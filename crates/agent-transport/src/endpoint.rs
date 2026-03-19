use std::collections::HashMap;
use std::sync::atomic::AtomicBool;
use std::sync::{Arc, Mutex};

use crossbeam_channel::{Receiver, Sender};
use tokio::runtime::Runtime;
use tokio_util::sync::CancellationToken;

use beep_detector::{BeepDetector, BeepDetectorConfig};

use crate::audio::AudioFrame;
use crate::call::CallSession;
use crate::config::EndpointConfig;
use crate::error::{EndpointError, Result};
use crate::events::EndpointEvent;
use crate::rtp_transport::RtpTransport;

/// WAV file recorder for call audio.
struct WavRecorder {
    // TODO: file handle, sample count, finalize on drop
}

/// Per-call context holding RTP transport, audio channels, and control flags.
struct CallContext {
    session: CallSession,
    _rtp: Option<RtpTransport>,
    _outgoing_tx: Sender<Vec<i16>>,
    _incoming_rx: Receiver<AudioFrame>,
    _muted: Arc<AtomicBool>,
    _paused: Arc<AtomicBool>,
    _flush_flag: Arc<AtomicBool>,
    _playout_notify: Arc<(std::sync::Mutex<bool>, std::sync::Condvar)>,
    _beep_detector: Option<BeepDetector>,
    _recorder: Option<WavRecorder>,
    _cancel: CancellationToken,
}

/// Shared mutable state for the endpoint.
struct EndpointState {
    registered: bool,
    calls: HashMap<i32, CallContext>,
    next_call_id: i32,
    // rsipstack objects will be stored here when implemented
}

/// The SIP endpoint — provides call control and audio I/O.
///
/// Pure Rust implementation using rsipstack for SIP signaling
/// and rtc-rtp for RTP media transport. No C dependencies.
pub struct SipEndpoint {
    _config: EndpointConfig,
    _runtime: Runtime,
    _state: Arc<Mutex<EndpointState>>,
    event_tx: Sender<EndpointEvent>,
    event_rx: Receiver<EndpointEvent>,
    _cancel: CancellationToken,
}

impl SipEndpoint {
    /// Create and initialize a new SIP endpoint.
    pub fn new(config: EndpointConfig) -> Result<Self> {
        let runtime = Runtime::new()
            .map_err(|e| EndpointError::Other(format!("failed to create tokio runtime: {}", e)))?;

        let (event_tx, event_rx) = crossbeam_channel::unbounded();
        let cancel = CancellationToken::new();

        let state = Arc::new(Mutex::new(EndpointState {
            registered: false,
            calls: HashMap::new(),
            next_call_id: 0,
        }));

        // TODO: Create rsipstack Endpoint + UdpConnection + DialogLayer
        // using runtime.block_on(async { ... })

        Ok(Self {
            _config: config,
            _runtime: runtime,
            _state: state,
            event_tx,
            event_rx,
            _cancel: cancel,
        })
    }

    /// Register with the SIP server using digest authentication.
    pub fn register(&self, _username: &str, _password: &str) -> Result<()> {
        // TODO: rsipstack Registration::register()
        let _ = self.event_tx.try_send(EndpointEvent::Registered);
        todo!("SIP registration via rsipstack")
    }

    /// Unregister from the SIP server.
    pub fn unregister(&self) -> Result<()> {
        todo!("SIP unregistration")
    }

    /// Check if currently registered.
    pub fn is_registered(&self) -> bool {
        self._state.lock().unwrap().registered
    }

    /// Make an outbound call. Returns the call ID.
    pub fn call(
        &self,
        _dest_uri: &str,
        _headers: Option<HashMap<String, String>>,
    ) -> Result<i32> {
        todo!("outbound call via rsipstack do_invite")
    }

    /// Answer an incoming call.
    pub fn answer(&self, _call_id: i32, _code: u16) -> Result<()> {
        todo!("answer incoming call")
    }

    /// Reject an incoming call.
    pub fn reject(&self, _call_id: i32, _code: u16) -> Result<()> {
        todo!("reject incoming call")
    }

    /// Hang up an active call.
    pub fn hangup(&self, _call_id: i32) -> Result<()> {
        todo!("hangup via BYE")
    }

    /// Send DTMF digits (default: RFC 4733 / RFC 2833).
    pub fn send_dtmf(&self, call_id: i32, digits: &str) -> Result<()> {
        self.send_dtmf_with_method(call_id, digits, "rfc2833")
    }

    /// Send DTMF digits with explicit method selection.
    ///
    /// Methods: "rfc2833" (RTP telephone-event, default), "sip_info" (SIP INFO)
    pub fn send_dtmf_with_method(&self, _call_id: i32, _digits: &str, _method: &str) -> Result<()> {
        todo!("DTMF send (RFC 4733 or SIP INFO)")
    }

    /// Blind transfer via SIP REFER.
    pub fn transfer(&self, _call_id: i32, _dest_uri: &str) -> Result<()> {
        todo!("blind transfer via REFER")
    }

    /// Attended transfer (connect two active calls).
    pub fn transfer_attended(&self, _call_id: i32, _target_call_id: i32) -> Result<()> {
        Err(EndpointError::Other(
            "attended transfer not supported with pure-Rust backend".into(),
        ))
    }

    /// Mute outgoing audio on a call.
    pub fn mute(&self, _call_id: i32) -> Result<()> {
        todo!("mute")
    }

    /// Unmute outgoing audio on a call.
    pub fn unmute(&self, _call_id: i32) -> Result<()> {
        todo!("unmute")
    }

    /// Send an audio frame into the active call.
    pub fn send_audio(&self, _call_id: i32, _frame: &AudioFrame) -> Result<()> {
        todo!("send audio frame to RTP transport")
    }

    /// Receive the next audio frame from a call (non-blocking).
    pub fn recv_audio(&self, _call_id: i32) -> Result<Option<AudioFrame>> {
        todo!("receive audio frame from RTP transport")
    }

    /// Mark the current playback segment as complete.
    pub fn flush(&self, _call_id: i32) -> Result<()> {
        todo!("flush")
    }

    /// Clear all queued outgoing audio immediately (barge-in).
    pub fn clear_buffer(&self, _call_id: i32) -> Result<()> {
        todo!("clear buffer")
    }

    /// Block until all queued outgoing audio has finished playing.
    pub fn wait_for_playout(&self, _call_id: i32, _timeout_ms: u64) -> Result<bool> {
        todo!("wait for playout")
    }

    /// Pause audio playback. Queued frames are preserved.
    pub fn pause(&self, _call_id: i32) -> Result<()> {
        todo!("pause")
    }

    /// Resume audio playback after a pause.
    pub fn resume(&self, _call_id: i32) -> Result<()> {
        todo!("resume")
    }

    /// Start recording a call to a WAV file.
    pub fn start_recording(&self, _call_id: i32, _path: &str) -> Result<()> {
        todo!("start recording")
    }

    /// Stop recording a call and finalize the WAV file.
    pub fn stop_recording(&self, _call_id: i32) -> Result<()> {
        todo!("stop recording")
    }

    /// Start asynchronous beep detection on a call.
    pub fn detect_beep(&self, _call_id: i32, _config: BeepDetectorConfig) -> Result<()> {
        todo!("detect beep")
    }

    /// Cancel beep detection on a call.
    pub fn cancel_beep_detection(&self, _call_id: i32) -> Result<()> {
        todo!("cancel beep detection")
    }

    /// Get the event receiver channel.
    pub fn events(&self) -> Receiver<EndpointEvent> {
        self.event_rx.clone()
    }

    /// Shut down the endpoint.
    pub fn shutdown(&self) -> Result<()> {
        self._cancel.cancel();
        Ok(())
    }
}

impl Drop for SipEndpoint {
    fn drop(&mut self) {
        let _ = self.shutdown();
    }
}
