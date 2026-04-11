//! Shared audio buffer matching WebRTC C++ AudioSource's internal buffer.
//!
//! Architecture:
//! - `send_audio` pushes samples under mutex, checks threshold
//! - If below threshold: signals completion immediately
//! - If above threshold: stores completion signal for deferred firing
//! - RTP send loop drains samples under same mutex every 20ms
//! - After draining, fires deferred completion if buffer dropped below threshold
//!
//! This matches WebRTC's C++ InternalSource::capture_frame + audio_task_ exactly:
//! - One buffer, one mutex
//! - Immediate vs deferred on_complete callback
//! - Only one deferred callback at a time

use std::collections::VecDeque;
use std::sync::Mutex;
use tracing::{debug, info};

use crate::sync::LockExt;

/// Default queue_size_ms matching _ParticipantAudioOutput production usage (200ms).
/// rtc.AudioSource class default is 1000ms, but LiveKit voice agents override to 200ms
/// for tighter backpressure and faster interrupt response.
const DEFAULT_QUEUE_SIZE_MS: u32 = 200;

/// Completion callback — called from RTP send loop thread to signal Python.
/// This is the equivalent of WebRTC's on_complete(ctx) callback.
pub(crate) type CompletionCallback = Box<dyn FnOnce() + Send>;

/// Inner state protected by mutex (matches WebRTC's mutex_-guarded fields).
struct Inner {
    /// PCM samples buffer (matches WebRTC's buffer_).
    /// VecDeque for O(1) drain from front (WebRTC C++ uses deque-like circular buffer).
    pcm: VecDeque<i16>,
    /// Deferred completion callback (matches WebRTC's on_complete_ + capture_userdata_)
    /// Only one at a time — WebRTC rejects capture_frame if one is already pending.
    pending_complete: Option<CompletionCallback>,
    /// Flush flag — when set, pcm is cleared on next drain
    flush: bool,
    /// Playout callback — fires when buffer drains to empty after flush.
    /// Matches WebRTC's waitForPlayout() which resolves when all queued audio is consumed.
    /// Per-flush: each flush() stores a new callback, replacing any previous one.
    playout_callback: Option<CompletionCallback>,
}

/// Shared audio buffer for outbound audio.
/// Thread-safe: locked by both send_audio (Python thread) and RTP send loop (tokio thread).
pub(crate) struct AudioBuffer {
    inner: Mutex<Inner>,
    /// notify_threshold = queue_size_samples (matches WebRTC C++)
    notify_threshold: usize,
    /// capacity = 2 * queue_size_samples (matches WebRTC C++)
    capacity: usize,
    /// Sample rate used for debug logging
    sample_rate: u32,
}

impl AudioBuffer {
    /// Create with default queue_size_ms (200ms, matching _ParticipantAudioOutput production).
    pub fn new() -> Self {
        Self::with_queue_size(DEFAULT_QUEUE_SIZE_MS, 8000)
    }

    /// Create with configurable queue_size_ms and sample_rate
    /// (matches WebRTC C++ InternalSource constructor).
    /// - notify_threshold = queue_size_ms * sample_rate / 1000
    /// - capacity = 2 * notify_threshold
    pub fn with_queue_size(queue_size_ms: u32, sample_rate: u32) -> Self {
        let queue_size_samples = (queue_size_ms as u64 * sample_rate as u64 / 1000) as usize;
        let notify_threshold = queue_size_samples;
        let capacity = queue_size_samples + notify_threshold; // 2x, same as WebRTC C++
        info!(
            "AudioBuffer: queue_size_ms={} sample_rate={} threshold={} capacity={}",
            queue_size_ms, sample_rate, notify_threshold, capacity
        );
        Self {
            inner: Mutex::new(Inner {
                pcm: VecDeque::with_capacity(capacity),
                pending_complete: None,
                flush: false,
                playout_callback: None,
            }),
            notify_threshold,
            capacity,
            sample_rate,
        }
    }

    /// Push samples into the buffer (called from send_audio on Python thread).
    ///
    /// Matches WebRTC C++ InternalSource::capture_frame exactly:
    /// 1. Check capacity → reject if full
    /// 2. Append samples
    /// 3. If below threshold → fire on_complete immediately
    /// 4. If above threshold → store on_complete for deferred firing
    pub fn push(&self, samples: &[i16], on_complete: CompletionCallback) -> Result<(), &'static str> {
        let mut inner = self.inner.lock_or_recover();

        // Check capacity (matches WebRTC: available = capacity - buffer_.size())
        let available = self.capacity.saturating_sub(inner.pcm.len());
        if available < samples.len() {
            return Err("buffer full");
        }

        // Reject if a deferred callback is already pending
        // (matches WebRTC: if (on_complete_ || capture_userdata_) return false)
        if inner.pending_complete.is_some() {
            return Err("previous capture still pending");
        }

        // Append samples
        inner.pcm.extend(samples.iter().copied());

        // Decision: immediate vs deferred (matches WebRTC threshold check)
        let buf_len = inner.pcm.len();
        if buf_len <= self.notify_threshold {
            // Below threshold → fire immediately (matches WebRTC immediate on_complete)
            drop(inner);
            on_complete();
            Ok(())
        } else {
            // Above threshold → store for deferred firing by RTP loop
            debug!("AudioBuffer: deferred callback, buf={} samples ({}ms)", buf_len, buf_len * 1000 / self.sample_rate as usize);
            inner.pending_complete = Some(on_complete);
            Ok(())
        }
    }

    /// Drain up to `count` samples from the front of the buffer.
    /// Called by RTP send loop every 20ms.
    ///
    /// After draining, checks if a deferred completion should fire
    /// (matches WebRTC's audio_task_ firing on_complete_ when buffer <= threshold).
    pub fn drain(&self, count: usize) -> Vec<i16> {
        let mut inner = self.inner.lock_or_recover();

        // Check flush flag first
        if inner.flush {
            let flushed = inner.pcm.len();
            inner.pcm.clear();
            inner.flush = false;
            // Fire any pending completion immediately
            let cb = inner.pending_complete.take();
            drop(inner);
            if flushed > 0 {
                info!("AudioBuffer flush: cleared {} samples", flushed);
            }
            if let Some(cb) = cb { cb(); }
            return Vec::new();
        }

        let n = count.min(inner.pcm.len());
        let samples: Vec<i16> = if n > 0 {
            inner.pcm.drain(..n).collect()
        } else {
            Vec::new()
        };

        // Fire deferred completion if buffer dropped below threshold
        // (matches WebRTC: if on_complete_ && buffer_.size() <= notify_threshold_samples_)
        let pending_cb = if inner.pending_complete.is_some() && inner.pcm.len() <= self.notify_threshold {
            let remaining = inner.pcm.len();
            let cb = inner.pending_complete.take();
            debug!("AudioBuffer: firing deferred callback, buf={} samples ({}ms)", remaining, remaining * 1000 / self.sample_rate as usize);
            cb
        } else { None };

        // Fire playout callback when buffer drains to empty
        // (matches WebRTC's waitForPlayout resolving when last frame consumed)
        let playout_cb = if inner.pcm.is_empty() && inner.playout_callback.is_some() {
            inner.playout_callback.take()
        } else { None };

        drop(inner);
        if let Some(cb) = pending_cb { cb(); }
        if let Some(cb) = playout_cb { cb(); }

        samples
    }

    /// Get current buffer length in samples.
    pub fn len(&self) -> usize {
        self.inner.lock_or_recover().pcm.len()
    }

    /// Check if buffer is empty.
    pub fn is_empty(&self) -> bool {
        self.inner.lock_or_recover().pcm.is_empty()
    }

    /// Set flush flag — buffer will be cleared on next drain tick.
    pub fn set_flush(&self) {
        let mut inner = self.inner.lock_or_recover();
        inner.flush = true;
        // Also fire any pending completion immediately
        let cb = inner.pending_complete.take();
        drop(inner);
        if let Some(cb) = cb { cb(); }
    }

    /// Push samples without backpressure — drop if buffer full.
    /// Used for background audio which is continuous and low priority.
    ///
    /// Drops are logged at DEBUG level so we can diagnose bg audio gaps
    /// (silent-ambient runs in recordings) as either drop-caused or
    /// upstream-starvation-caused. Upstream path is Python-paced via the
    /// rtc.AudioStream loopback reader; stalls on the Python event loop
    /// (transformer inference etc.) can cause bursts that overflow here.
    pub fn push_no_backpressure(&self, samples: &[i16]) {
        let mut inner = self.inner.lock_or_recover();
        let buf_len = inner.pcm.len();
        let available = self.capacity.saturating_sub(buf_len);
        if available >= samples.len() {
            inner.pcm.extend(samples.iter().copied());
        } else {
            let dropped = samples.len();
            let sr = self.sample_rate;
            drop(inner);
            debug!(
                "AudioBuffer: no-backpressure drop: dropped={} samples ({}ms), buf={} ({}ms), cap={} ({}ms)",
                dropped,
                dropped * 1000 / sr as usize,
                buf_len,
                buf_len * 1000 / sr as usize,
                self.capacity,
                self.capacity * 1000 / sr as usize,
            );
        }
    }

    /// Clear buffer immediately and fire pending completion.
    /// Called from clear_queue for immediate interrupt.
    /// Does NOT fire playout_callback — interruption is handled by the
    /// interrupted_event/interruptedFuture in the Python/TS layer.
    /// Matches WebRTC: clearQueue() clears buffer but doesn't resolve waitForPlayout().
    pub fn clear(&self) {
        let mut inner = self.inner.lock_or_recover();
        let cleared = inner.pcm.len();
        inner.pcm.clear();
        inner.flush = false;
        let cb = inner.pending_complete.take();
        let _playout_cb = inner.playout_callback.take(); // Drop without firing
        drop(inner);
        if cleared > 0 {
            info!("AudioBuffer clear: cleared {} samples", cleared);
        }
        if let Some(cb) = cb { cb(); }
    }

    /// Get queued audio duration in milliseconds (real buffer state).
    /// Matches WebRTC's audioSource.queuedDuration.
    pub fn queued_duration_ms(&self, sample_rate: u32) -> f64 {
        let len = self.inner.lock_or_recover().pcm.len();
        (len as f64 / sample_rate as f64) * 1000.0
    }

    /// Set a callback to fire when buffer drains to empty after flush.
    /// Matches WebRTC's audioSource.waitForPlayout() — resolves when all
    /// queued audio is consumed by the RTP send loop.
    /// Per-flush: fires any existing playout callback first (so previous
    /// waiter isn't orphaned), then stores the new one.
    /// Pause-aware: callback won't fire while paused (RTP loop doesn't drain).
    pub fn set_playout_callback(&self, cb: CompletionCallback) {
        let mut inner = self.inner.lock_or_recover();
        // Fire any previously stored callback so its waiter isn't orphaned.
        // This handles concurrent flush() calls where a new wait_for_playout
        // replaces an in-flight one before the RTP loop has fired it.
        let prev = inner.playout_callback.take();
        // If buffer is already empty, fire the new callback immediately
        if inner.pcm.is_empty() {
            drop(inner);
            if let Some(prev_cb) = prev { prev_cb(); }
            cb();
        } else {
            inner.playout_callback = Some(cb);
            drop(inner);
            if let Some(prev_cb) = prev { prev_cb(); }
        }
    }
}

impl Drop for AudioBuffer {
    fn drop(&mut self) {
        // Fire any pending callbacks to unblock callers waiting on
        // capture_frame or wait_for_playout. Without this, Python/TS callers
        // awaiting these callbacks hang forever on session teardown.
        // Recover from poisoning so even a panicked producer releases its
        // waiters on drop.
        let mut inner = self.inner.lock_or_recover();
        if let Some(cb) = inner.pending_complete.take() { cb(); }
        if let Some(cb) = inner.playout_callback.take() { cb(); }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    use std::sync::Arc;

    // Small helper: 100 samples per push, threshold default = 200ms*8000/1000 = 1600
    fn new_buf() -> AudioBuffer {
        AudioBuffer::with_queue_size(200, 8000)
    }

    // ─── push / drain basics ─────────────────────────────────────────────

    #[test]
    fn test_push_below_threshold_fires_complete_immediately() {
        let buf = new_buf();
        let fired = Arc::new(AtomicBool::new(false));
        let f = fired.clone();
        buf.push(&vec![0i16; 100], Box::new(move || f.store(true, Ordering::SeqCst))).unwrap();
        assert!(fired.load(Ordering::SeqCst), "below threshold → immediate fire");
    }

    #[test]
    fn test_push_above_threshold_defers_complete() {
        let buf = new_buf();
        let fired = Arc::new(AtomicBool::new(false));
        let f = fired.clone();
        // 2000 > threshold 1600, should defer
        buf.push(&vec![0i16; 2000], Box::new(move || f.store(true, Ordering::SeqCst))).unwrap();
        assert!(!fired.load(Ordering::SeqCst), "above threshold → deferred, not yet fired");
        // Drain enough to drop below threshold, then it should fire
        let _ = buf.drain(500);
        assert!(fired.load(Ordering::SeqCst), "after drain below threshold → deferred callback fires");
    }

    #[test]
    fn test_push_rejects_when_full() {
        let buf = new_buf();
        // capacity = 2 * 1600 = 3200
        let _ = buf.push(&vec![0i16; 3200], Box::new(|| {}));
        // Already at capacity AND pending_complete pending → second push must fail
        let r = buf.push(&vec![0i16; 100], Box::new(|| {}));
        assert!(r.is_err(), "buffer full should reject");
    }

    #[test]
    fn test_push_rejects_second_pending_complete() {
        let buf = new_buf();
        // 2000 > 1600 → pending_complete stored
        buf.push(&vec![0i16; 2000], Box::new(|| {})).unwrap();
        // Second deferred push while one is pending → must reject
        let r = buf.push(&vec![0i16; 100], Box::new(|| {}));
        assert!(r.is_err(), "only one pending_complete allowed at a time");
    }

    #[test]
    fn test_drain_returns_samples_in_order() {
        let buf = new_buf();
        let input: Vec<i16> = (0..500).map(|i| i as i16).collect();
        buf.push(&input, Box::new(|| {})).unwrap();
        let drained = buf.drain(500);
        assert_eq!(drained, input);
        assert!(buf.is_empty());
    }

    #[test]
    fn test_drain_empty_returns_empty() {
        let buf = new_buf();
        let d = buf.drain(100);
        assert!(d.is_empty());
    }

    #[test]
    fn test_len_and_is_empty() {
        let buf = new_buf();
        assert!(buf.is_empty());
        assert_eq!(buf.len(), 0);
        buf.push(&vec![0i16; 100], Box::new(|| {})).unwrap();
        assert_eq!(buf.len(), 100);
        assert!(!buf.is_empty());
    }

    // ─── playout_callback semantics ──────────────────────────────────────

    #[test]
    fn test_set_playout_callback_on_empty_fires_immediately() {
        let buf = new_buf();
        let fired = Arc::new(AtomicBool::new(false));
        let f = fired.clone();
        buf.set_playout_callback(Box::new(move || f.store(true, Ordering::SeqCst)));
        assert!(fired.load(Ordering::SeqCst), "empty buffer → callback fires immediately");
    }

    #[test]
    fn test_playout_callback_fires_on_drain_to_empty() {
        let buf = new_buf();
        buf.push(&vec![0i16; 100], Box::new(|| {})).unwrap();
        let fired = Arc::new(AtomicBool::new(false));
        let f = fired.clone();
        buf.set_playout_callback(Box::new(move || f.store(true, Ordering::SeqCst)));
        assert!(!fired.load(Ordering::SeqCst), "not yet — buffer still has samples");
        let _ = buf.drain(100);
        assert!(fired.load(Ordering::SeqCst), "drained to empty → playout fires");
    }

    #[test]
    fn test_playout_callback_not_fired_while_samples_remain() {
        let buf = new_buf();
        buf.push(&vec![0i16; 500], Box::new(|| {})).unwrap();
        let fired = Arc::new(AtomicBool::new(false));
        let f = fired.clone();
        buf.set_playout_callback(Box::new(move || f.store(true, Ordering::SeqCst)));
        // Drain partial — still has samples
        let _ = buf.drain(200);
        assert!(!fired.load(Ordering::SeqCst));
        let _ = buf.drain(200);
        assert!(!fired.load(Ordering::SeqCst));
        let _ = buf.drain(200); // now empty
        assert!(fired.load(Ordering::SeqCst), "fires when last drain empties the buffer");
    }

    #[test]
    fn test_set_playout_callback_fires_previous_on_replace() {
        // Regression: set_playout_callback used to silently drop the existing
        // callback, orphaning concurrent flush waiters.
        let buf = new_buf();
        buf.push(&vec![0i16; 500], Box::new(|| {})).unwrap();

        let first_fired = Arc::new(AtomicBool::new(false));
        let f1 = first_fired.clone();
        buf.set_playout_callback(Box::new(move || f1.store(true, Ordering::SeqCst)));

        let second_fired = Arc::new(AtomicBool::new(false));
        let f2 = second_fired.clone();
        buf.set_playout_callback(Box::new(move || f2.store(true, Ordering::SeqCst)));

        assert!(first_fired.load(Ordering::SeqCst), "old callback must fire when replaced");
        assert!(!second_fired.load(Ordering::SeqCst), "new callback waits for drain");

        let _ = buf.drain(500);
        assert!(second_fired.load(Ordering::SeqCst), "new callback fires on drain to empty");
    }

    #[test]
    fn test_set_playout_callback_replace_on_empty_fires_both() {
        let buf = new_buf();
        let first_fired = Arc::new(AtomicBool::new(false));
        let f1 = first_fired.clone();
        buf.set_playout_callback(Box::new(move || f1.store(true, Ordering::SeqCst)));
        assert!(first_fired.load(Ordering::SeqCst), "empty → first fires immediately");

        // Now push some and call set again — old one is already fired, new one should defer
        buf.push(&vec![0i16; 100], Box::new(|| {})).unwrap();
        let second_fired = Arc::new(AtomicBool::new(false));
        let f2 = second_fired.clone();
        buf.set_playout_callback(Box::new(move || f2.store(true, Ordering::SeqCst)));
        assert!(!second_fired.load(Ordering::SeqCst));
        let _ = buf.drain(100);
        assert!(second_fired.load(Ordering::SeqCst));
    }

    // ─── Drop behavior ───────────────────────────────────────────────────

    #[test]
    fn test_drop_fires_pending_complete() {
        let fired = Arc::new(AtomicBool::new(false));
        {
            let buf = new_buf();
            let f = fired.clone();
            // Above threshold → stored as pending
            buf.push(&vec![0i16; 2000], Box::new(move || f.store(true, Ordering::SeqCst))).unwrap();
            assert!(!fired.load(Ordering::SeqCst));
            // buf drops here
        }
        assert!(fired.load(Ordering::SeqCst), "Drop must fire pending_complete");
    }

    #[test]
    fn test_drop_fires_playout_callback() {
        // Regression: Drop used to silently drop playout_callback, hanging
        // wait_for_playout_notify callers on session teardown.
        let fired = Arc::new(AtomicBool::new(false));
        {
            let buf = new_buf();
            buf.push(&vec![0i16; 500], Box::new(|| {})).unwrap();
            let f = fired.clone();
            buf.set_playout_callback(Box::new(move || f.store(true, Ordering::SeqCst)));
            assert!(!fired.load(Ordering::SeqCst), "still samples queued");
            // buf drops here
        }
        assert!(fired.load(Ordering::SeqCst), "Drop must fire playout_callback");
    }

    #[test]
    fn test_drop_fires_both_callbacks() {
        let pending_fired = Arc::new(AtomicBool::new(false));
        let playout_fired = Arc::new(AtomicBool::new(false));
        {
            let buf = new_buf();
            let p = pending_fired.clone();
            buf.push(&vec![0i16; 2000], Box::new(move || p.store(true, Ordering::SeqCst))).unwrap();
            let po = playout_fired.clone();
            buf.set_playout_callback(Box::new(move || po.store(true, Ordering::SeqCst)));
        }
        assert!(pending_fired.load(Ordering::SeqCst));
        assert!(playout_fired.load(Ordering::SeqCst));
    }

    // ─── clear semantics ─────────────────────────────────────────────────

    #[test]
    fn test_clear_fires_pending_complete() {
        let buf = new_buf();
        let fired = Arc::new(AtomicBool::new(false));
        let f = fired.clone();
        buf.push(&vec![0i16; 2000], Box::new(move || f.store(true, Ordering::SeqCst))).unwrap();
        assert!(!fired.load(Ordering::SeqCst));
        buf.clear();
        assert!(fired.load(Ordering::SeqCst), "clear fires pending_complete");
        assert!(buf.is_empty());
    }

    #[test]
    fn test_clear_drops_playout_callback_without_firing() {
        // Documented behavior: clear() drops playout_callback without firing.
        // Interruption is signalled via a separate path (interrupted_event /
        // interruptedFuture) in the Python/TS layers.
        let buf = new_buf();
        buf.push(&vec![0i16; 500], Box::new(|| {})).unwrap();
        let fired = Arc::new(AtomicBool::new(false));
        let f = fired.clone();
        buf.set_playout_callback(Box::new(move || f.store(true, Ordering::SeqCst)));
        buf.clear();
        assert!(!fired.load(Ordering::SeqCst), "clear must NOT fire playout_callback");
        assert!(buf.is_empty());
    }

    // ─── push_no_backpressure (for background audio) ─────────────────────

    #[test]
    fn test_push_no_backpressure_drops_silently_when_full() {
        let buf = new_buf();
        // Fill to capacity
        buf.push_no_backpressure(&vec![0i16; 3200]);
        assert_eq!(buf.len(), 3200);
        // Additional push should silently drop
        buf.push_no_backpressure(&vec![0i16; 100]);
        assert_eq!(buf.len(), 3200, "push_no_backpressure drops when full");
    }

    #[test]
    fn test_push_no_backpressure_no_callbacks() {
        let buf = new_buf();
        // Should just append without triggering any callback path
        buf.push_no_backpressure(&vec![0i16; 500]);
        assert_eq!(buf.len(), 500);
    }

    // ─── queued_duration_ms ───────────────────────────────────────────────

    #[test]
    fn test_queued_duration_ms() {
        let buf = new_buf();
        buf.push(&vec![0i16; 800], Box::new(|| {})).unwrap();
        // 800 samples at 8000Hz = 100ms
        let dur = buf.queued_duration_ms(8000);
        assert!((dur - 100.0).abs() < 0.01, "expected ~100ms, got {}", dur);
    }

    // ─── Repeated drain with callback counts ─────────────────────────────

    #[test]
    fn test_callback_fires_once_per_registration() {
        let buf = new_buf();
        buf.push(&vec![0i16; 500], Box::new(|| {})).unwrap();
        let count = Arc::new(AtomicUsize::new(0));
        let c = count.clone();
        buf.set_playout_callback(Box::new(move || { c.fetch_add(1, Ordering::SeqCst); }));
        let _ = buf.drain(500);
        assert_eq!(count.load(Ordering::SeqCst), 1);
        // Subsequent drains of an empty buffer should not refire
        let _ = buf.drain(100);
        let _ = buf.drain(100);
        assert_eq!(count.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn test_pending_complete_callback_fires_once() {
        let buf = new_buf();
        let count = Arc::new(AtomicUsize::new(0));
        let c = count.clone();
        buf.push(&vec![0i16; 2000], Box::new(move || { c.fetch_add(1, Ordering::SeqCst); })).unwrap();
        let _ = buf.drain(500); // drops below threshold
        assert_eq!(count.load(Ordering::SeqCst), 1);
        let _ = buf.drain(500);
        let _ = buf.drain(500);
        assert_eq!(count.load(Ordering::SeqCst), 1, "pending_complete only fires once");
    }
}
