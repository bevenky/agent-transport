/**
 * SipAudioOutput — extends LiveKit's AudioOutput base class for SIP/AudioStream.
 *
 * Matches WebRTC's ParticipantAudioOutput exactly:
 * - captureFrame sends to Rust with backpressure (sendAudioNotify callback)
 * - waitForPlayout uses Rust callback (fires when buffer drains to empty)
 * - queuedDuration reads real Rust buffer state
 * - clearBuffer signals interruption
 * - pause/resume controls Rust RTP output directly
 *
 * No timer heuristics — all playout tracking comes from Rust.
 */

import { AudioFrame } from '@livekit/rtc-node';
import { createRequire } from 'node:module';
import { Future, Task } from '@livekit/agents';
import type { SipEndpoint, AudioStreamEndpoint } from 'agent-transport';

// AudioOutput is not publicly exported from @livekit/agents — resolve internal path
const _require = createRequire(import.meta.url);
const _agentsPath = _require.resolve('@livekit/agents');
const _ioPath = _agentsPath.replace(/dist\/index\.(c?)js$/, 'dist/voice/io.$1js');
const { AudioOutput: _AudioOutputBase } = _require(_ioPath);

// Log via fd 2 (stderr) — pino-pretty takes over stdout/fd 1 completely
import { writeSync } from 'node:fs';
const _log = (msg: string) => { try { writeSync(2, msg + '\n'); } catch {} };

export class SipAudioOutput extends _AudioOutputBase {
  private endpoint: SipEndpoint | AudioStreamEndpoint;
  private sessionId: string;

  private flushTask: Task<void> | null = null;
  private interruptedFuture = new Future<void>();
  private firstFrameEmitted = false;
  private pushedDuration = 0;
  private rustPaused = false;

  constructor(
    endpoint: SipEndpoint | AudioStreamEndpoint,
    sessionId: string,
    sampleRate?: number,
    nextInChain?: any,
  ) {
    const _sampleRate = sampleRate ?? endpoint.outputSampleRate;
    super(_sampleRate, nextInChain, { pause: true });
    _log(`SipAudioOutput constructor: sr=${_sampleRate} session=${sessionId}`);
    this.endpoint = endpoint;
    this.sessionId = sessionId;
  }

  // -- captureFrame: matches WebRTC's ParticipantAudioOutput.captureFrame --

  async captureFrame(frame: AudioFrame): Promise<void> {
    // Segment tracking (WebRTC's super.captureFrame is sync void)
    super.captureFrame(frame);

    // Track pushed duration before the await so a sync caller's
    // pushedDuration accounting matches what the upstream sync
    // super.captureFrame would have done.
    this.pushedDuration += frame.samplesPerChannel / frame.sampleRate;

    // Push to Rust with backpressure — callback fires when buffer has space.
    // Matches WebRTC's `await audioSource.captureFrame(frame)`.
    const frameData = Buffer.from(frame.data.buffer, frame.data.byteOffset, frame.data.byteLength);
    const isFirstFrame = !this.firstFrameEmitted;
    await new Promise<void>((resolve) => {
      try {
        this.endpoint.sendAudioNotify(
          this.sessionId,
          frameData,
          frame.sampleRate,
          frame.channels,
          () => resolve(),
        );
      } catch {
        // Buffer full or session gone — drop frame silently (matches WebRTC
        // behavior where captureFrame returns false on buffer full without
        // throwing).
        resolve();
      }
    });

    // Emit playback-started AFTER the first frame has actually been
    // accepted by Rust (the napi callback fired). Upstream LiveKit fires
    // it after `await audioSource.captureFrame(frame)` returns, so the
    // TTFB metric reflects "first frame queued for playback" rather than
    // "first frame received from TTS". Firing it before the await would
    // overreport TTFB by ~50-100 ms.
    if (isFirstFrame) {
      this.firstFrameEmitted = true;
      this.onPlaybackStarted(Date.now());
    }
  }

  // -- flush: matches WebRTC's ParticipantAudioOutput.flush --

  flush(): void {
    super.flush();

    if (!this.pushedDuration) {
      _log('SipAudioOutput.flush: no pushed_duration, skipping');
      return;
    }

    if (this.flushTask && !this.flushTask.done) {
      _log('SipAudioOutput.flush: called while playback in progress');
      this.flushTask.cancel();
    }

    _log(`SipAudioOutput.flush: pushed_dur=${this.pushedDuration.toFixed(3)}s, creating waitForPlayout task`);
    this.flushTask = Task.from((controller: any) => this.waitForPlayoutTask(controller));
  }

  // -- clearBuffer: matches WebRTC's ParticipantAudioOutput.clearBuffer --

  clearBuffer(): void {
    if (!this.pushedDuration) {
      _log('SipAudioOutput.clearBuffer: no pushed_duration, skipping');
      return;
    }
    _log(`SipAudioOutput.clearBuffer: setting interrupted, pushed_dur=${this.pushedDuration.toFixed(3)}s`);
    this.interruptedFuture.resolve();
  }

  // -- pause/resume: call Rust endpoint directly for immediate RTP effect --

  pause(): void {
    super.pause();
    if (!this.rustPaused) {
      // Update the flag AFTER the FFI call succeeds. If endpoint.pause()
      // throws (e.g., session already closed), the flag must stay false
      // so a subsequent pause() retries instead of being short-circuited
      // by the guard — otherwise Rust keeps sending audio while the TS
      // layer thinks it's paused.
      try {
        this.endpoint.pause(this.sessionId);
        this.rustPaused = true;
        _log('SipAudioOutput.pause: Rust paused');
      } catch (e) {
        _log(`SipAudioOutput.pause failed: ${(e as Error)?.message ?? e}`);
      }
    }
  }

  resume(): void {
    super.resume();
    if (this.rustPaused) {
      try {
        this.endpoint.resume(this.sessionId);
        this.rustPaused = false;
        _log('SipAudioOutput.resume: Rust resumed');
      } catch (e) {
        _log(`SipAudioOutput.resume failed: ${(e as Error)?.message ?? e}`);
      }
    }
  }

  // -- waitForPlayoutTask: matches WebRTC's waitForPlayoutTask exactly --

  private async waitForPlayoutTask(abortController?: any): Promise<void> {
    _log(`SipAudioOutput._waitForPlayout: starting (pushed=${this.pushedDuration.toFixed(3)}s)`);
    const abortFuture = new Future<boolean>();
    const resolveAbort = () => {
      if (!abortFuture.done) abortFuture.resolve(true);
    };
    if (abortController?.signal) {
      abortController.signal.addEventListener('abort', resolveAbort);
    }

    // Wait for Rust playout — callback fires when buffer drains to empty.
    // Pause-aware: won't fire while paused (RTP loop doesn't drain).
    // Matches WebRTC's audioSource.waitForPlayout().
    this.waitForSourcePlayout().finally(() => {
      if (abortController?.signal) {
        abortController.signal.removeEventListener('abort', resolveAbort);
      }
      if (!abortFuture.done) abortFuture.resolve(false);
    });

    const interrupted = await Promise.race([
      abortFuture.await,
      this.interruptedFuture.await.then(() => true),
    ]);

    let pushedDuration = this.pushedDuration;

    if (interrupted) {
      // Real Rust buffer state — matches WebRTC's audioSource.queuedDuration.
      // Always exposed by both SipEndpoint and AudioStreamEndpoint via napi.
      const queuedMs = this.endpoint.queuedDurationMs(this.sessionId);
      pushedDuration = Math.max(this.pushedDuration - queuedMs / 1000, 0);
      this.clearSourceQueue();
      _log(`SipAudioOutput._waitForPlayout: interrupted, played=${pushedDuration.toFixed(3)}s`);
    } else {
      _log(`SipAudioOutput._waitForPlayout: completed, played=${pushedDuration.toFixed(3)}s`);
    }

    this.pushedDuration = 0;
    this.interruptedFuture = new Future();
    this.firstFrameEmitted = false;
    this.onPlaybackFinished({
      playbackPosition: pushedDuration,
      interrupted,
    });
  }

  // -- Source helpers (using Rust APIs, matching WebRTC's audioSource) --

  /** Wait for playout via Rust callback — no timer, no thread pool. */
  private waitForSourcePlayout(): Promise<void> {
    return new Promise<void>((resolve) => {
      try {
        (this.endpoint as any).waitForPlayoutNotify(this.sessionId, () => resolve());
      } catch {
        resolve(); // Session gone
      }
    });
  }

  /** Clear Rust buffer immediately. */
  private clearSourceQueue(): void {
    try {
      this.endpoint.clearBuffer(this.sessionId);
    } catch { /* ignore */ }
  }

  // -- lifecycle --

  async close(): Promise<void> {
    if (this.flushTask) this.flushTask.cancel();
  }

  onAttached(): void {
    if (this.nextInChain) this.nextInChain.onAttached();
  }

  onDetached(): void {
    if (this.nextInChain) this.nextInChain.onDetached();
  }
}
