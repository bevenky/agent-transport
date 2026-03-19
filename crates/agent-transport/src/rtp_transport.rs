//! RTP transport — sends and receives audio over UDP with G.711 codec.
//!
//! Handles RTP packet framing (via rtc-rtp), G.711 PCMU/PCMA encode/decode,
//! and 8kHz ↔ 16kHz resampling.

use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, AtomicU16, AtomicU32};
use std::sync::Arc;

use crossbeam_channel::{Receiver, Sender};
use tokio::net::UdpSocket;
use tokio_util::sync::CancellationToken;

use crate::audio::AudioFrame;
use crate::config::Codec;
use crate::events::EndpointEvent;

/// RTP transport for a single call.
pub(crate) struct RtpTransport {
    pub socket: Arc<UdpSocket>,
    pub remote_addr: SocketAddr,
    pub ssrc: u32,
    pub codec: Codec,
    pub seq: AtomicU16,
    pub timestamp: AtomicU32,
    pub cancel: CancellationToken,
}

impl RtpTransport {
    pub fn new(
        _socket: Arc<UdpSocket>,
        _remote_addr: SocketAddr,
        _codec: Codec,
    ) -> Self {
        todo!("RTP transport constructor")
    }

    /// Start the RTP send loop (reads from outgoing channel, encodes, sends).
    pub fn start_send_loop(
        &self,
        _outgoing_rx: Receiver<Vec<i16>>,
        _muted: Arc<AtomicBool>,
        _paused: Arc<AtomicBool>,
        _flush_flag: Arc<AtomicBool>,
        _playout_notify: Arc<(std::sync::Mutex<bool>, std::sync::Condvar)>,
    ) -> tokio::task::JoinHandle<()> {
        todo!("RTP send loop")
    }

    /// Start the RTP recv loop (receives, decodes, pushes to incoming channel).
    pub fn start_recv_loop(
        &self,
        _incoming_tx: Sender<AudioFrame>,
        _event_tx: Sender<EndpointEvent>,
        _call_id: i32,
    ) -> tokio::task::JoinHandle<()> {
        todo!("RTP recv loop")
    }
}

// ─── G.711 Codec ─────────────────────────────────────────────────────────────

/// Encode a 16-bit linear PCM sample to G.711 mu-law (PCMU).
pub(crate) fn pcmu_encode(_sample: i16) -> u8 {
    todo!("PCMU encode")
}

/// Decode a G.711 mu-law (PCMU) byte to 16-bit linear PCM.
pub(crate) fn pcmu_decode(_byte: u8) -> i16 {
    todo!("PCMU decode")
}

/// Encode a 16-bit linear PCM sample to G.711 A-law (PCMA).
pub(crate) fn pcma_encode(_sample: i16) -> u8 {
    todo!("PCMA encode")
}

/// Decode a G.711 A-law (PCMA) byte to 16-bit linear PCM.
pub(crate) fn pcma_decode(_byte: u8) -> i16 {
    todo!("PCMA decode")
}

// ─── Resampling ──────────────────────────────────────────────────────────────

/// Resample from 8kHz to 16kHz (linear interpolation).
pub(crate) fn resample_8k_to_16k(_samples: &[i16]) -> Vec<i16> {
    todo!("upsample 8k→16k")
}

/// Resample from 16kHz to 8kHz (decimation).
pub(crate) fn resample_16k_to_8k(_samples: &[i16]) -> Vec<i16> {
    todo!("downsample 16k→8k")
}
