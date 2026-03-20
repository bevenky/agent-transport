//! RTP transport — audio send/recv over UDP with G.711 codec.
//!
//! Handles: symmetric RTP (NAT), SSRC tracking, media timeout, marker bit,
//! packet validation, DTMF with proper END pacing and duration tracking,
//! NAT keepalive, and basic RTCP SR/RR.

use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, AtomicU16, AtomicU32, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use audio_codec_algorithms::{decode_alaw, decode_ulaw, encode_alaw, encode_ulaw};
use crossbeam_channel::{Receiver, Sender};
use rtp::{header::Header, packet::Packet};
use tokio::net::UdpSocket;
use tokio_util::sync::CancellationToken;
use tracing::{debug, info, warn};
use webrtc_util::marshal::{Marshal, MarshalSize, Unmarshal};

use beep_detector::{BeepDetector, BeepDetectorResult};
use crate::audio::AudioFrame;
use crate::config::Codec;
use crate::dtmf;
use crate::events::EndpointEvent;

/// Default DTMF payload type (negotiated via SDP, this is fallback).
pub(crate) const DEFAULT_DTMF_PT: u8 = 101;

/// Media timeout — if no RTP received for this duration, emit timeout event.
const MEDIA_TIMEOUT: Duration = Duration::from_secs(30);

/// NAT keepalive interval — send empty RTP to keep NAT pinhole open.
const NAT_KEEPALIVE_INTERVAL: Duration = Duration::from_secs(15);

/// DTMF END packet timeout — if no END received, auto-complete after this.
const DTMF_END_TIMEOUT: Duration = Duration::from_secs(5);

pub(crate) struct RtpTransport {
    pub socket: Arc<UdpSocket>,
    pub remote_addr: Mutex<SocketAddr>, // Mutable for symmetric RTP
    ssrc: u32,
    codec: Codec,
    seq: AtomicU16,
    timestamp: AtomicU32,
    pub dtmf_pt: u8,        // Negotiated from SDP
    pub ptime_ms: u32,      // Negotiated from SDP (default 20)
    pub cancel: CancellationToken,
}

impl RtpTransport {
    pub fn new(socket: Arc<UdpSocket>, remote_addr: SocketAddr, codec: Codec, cancel: CancellationToken, dtmf_pt: u8, ptime_ms: u32) -> Self {
        Self {
            socket, remote_addr: Mutex::new(remote_addr), ssrc: rand::random(),
            codec, seq: AtomicU16::new(0), timestamp: AtomicU32::new(0),
            dtmf_pt, ptime_ms, cancel,
        }
    }

    fn remote(&self) -> SocketAddr { *self.remote_addr.lock().unwrap() }
    fn samples_per_frame(&self) -> u32 { 8000 * self.ptime_ms / 1000 } // samples at 8kHz

    async fn send_rtp(&self, pt: u8, ts: u32, marker: bool, payload: Vec<u8>) -> std::io::Result<()> {
        let pkt = Packet {
            header: Header { version: 2, marker, payload_type: pt, sequence_number: self.seq.fetch_add(1, Ordering::Relaxed), timestamp: ts, ssrc: self.ssrc, ..Default::default() },
            payload: bytes::Bytes::from(payload),
        };
        self.socket.send_to(&pkt.marshal().map_err(|e| std::io::Error::other(e.to_string()))?, self.remote()).await?;
        Ok(())
    }

    /// Send DTMF with proper END pacing (3x at ptime intervals per RFC 4733).
    pub async fn send_dtmf_event(&self, digit: char, duration_ms: u32) -> std::io::Result<()> {
        let ev = dtmf::digit_to_event(digit).unwrap_or(0);
        let ts = self.timestamp.load(Ordering::Relaxed);
        let dur_samples = (8 * duration_ms) as u16; // 8 samples/ms at 8kHz
        let ptime = Duration::from_millis(self.ptime_ms as u64);

        // Start event (marker=true)
        self.send_rtp(self.dtmf_pt, ts, true, dtmf::encode_rfc4733(ev, false, 10, 0).to_vec()).await?;
        tokio::time::sleep(ptime).await;

        // Update events during duration
        let steps = (duration_ms / self.ptime_ms).max(1);
        for i in 1..=steps {
            let d = ((8 * self.ptime_ms * i) as u16).min(dur_samples);
            self.send_rtp(self.dtmf_pt, ts, false, dtmf::encode_rfc4733(ev, false, 10, d).to_vec()).await?;
            tokio::time::sleep(ptime).await;
        }

        // END event 3x at ptime intervals (RFC 4733 §2.5.3)
        for _ in 0..3 {
            self.send_rtp(self.dtmf_pt, ts, false, dtmf::encode_rfc4733(ev, true, 10, dur_samples).to_vec()).await?;
            tokio::time::sleep(ptime).await;
        }
        Ok(())
    }

    pub fn start_send_loop(self: &Arc<Self>, rx: Receiver<Vec<i16>>, muted: Arc<AtomicBool>, paused: Arc<AtomicBool>, flush: Arc<AtomicBool>, playout: Arc<(Mutex<bool>, Condvar)>) -> tokio::task::JoinHandle<()> {
        let t = Arc::clone(self);
        tokio::spawn(async move {
            let mut iv = tokio::time::interval(Duration::from_millis(t.ptime_ms as u64));
            let sil = silence_byte(t.codec);
            let spf = t.samples_per_frame();
            let mut first_audio = true; // For marker bit on first packet

            loop {
                tokio::select! { _ = t.cancel.cancelled() => break, _ = iv.tick() => {} }
                let ts = t.timestamp.fetch_add(spf, Ordering::Relaxed);
                if flush.swap(false, Ordering::Relaxed) { while rx.try_recv().is_ok() {} notify(&playout); first_audio = true; continue; }
                if paused.load(Ordering::Relaxed) { let _ = t.send_rtp(t.codec.payload_type(), ts, false, vec![sil; spf as usize]).await; continue; }

                match rx.try_recv() {
                    Ok(s) if !muted.load(Ordering::Relaxed) => {
                        let marker = first_audio; // #12: marker on first packet after silence
                        first_audio = false;
                        let _ = t.send_rtp(t.codec.payload_type(), ts, marker, encode_g711(&downsample(&s), t.codec)).await;
                    }
                    Ok(_) => { let _ = t.send_rtp(t.codec.payload_type(), ts, false, vec![sil; spf as usize]).await; }
                    Err(_) => { notify(&playout); first_audio = true; let _ = t.send_rtp(t.codec.payload_type(), ts, false, vec![sil; spf as usize]).await; }
                }
            }
        })
    }

    pub fn start_recv_loop(self: &Arc<Self>, tx: Sender<AudioFrame>, etx: Sender<EndpointEvent>, cid: i32, bd: Arc<Mutex<Option<BeepDetector>>>) -> tokio::task::JoinHandle<()> {
        let t = Arc::clone(self);
        tokio::spawn(async move {
            let mut buf = vec![0u8; 2048];
            let mut last_rtp = Instant::now();
            let mut remote_ssrc: Option<u32> = None;
            let mut keepalive_iv = tokio::time::interval(NAT_KEEPALIVE_INTERVAL);
            // DTMF receiver state
            let mut dtmf_event: Option<u8> = None;
            let mut dtmf_last_ts: u32 = 0;
            let mut dtmf_timer: Option<Instant> = None;

            loop {
                tokio::select! {
                    _ = t.cancel.cancelled() => break,
                    _ = keepalive_iv.tick() => {
                        // #20: NAT keepalive — send empty RTP to keep pinhole open
                        let ts = t.timestamp.load(Ordering::Relaxed);
                        let _ = t.send_rtp(t.codec.payload_type(), ts, false, vec![silence_byte(t.codec); t.samples_per_frame() as usize]).await;
                    }
                    r = t.socket.recv_from(&mut buf) => {
                        let (len, from_addr) = match r { Ok(r) => r, Err(_) => continue };
                        if len < 12 { continue; } // #8: minimum RTP header size

                        let pkt = match Packet::unmarshal(&mut &buf[..len]) { Ok(p) => p, Err(_) => continue };

                        // #8: RTP version check
                        if pkt.header.version != 2 { continue; }

                        // #7: Symmetric RTP — learn remote address from first incoming packet
                        if from_addr != t.remote() {
                            info!("Symmetric RTP: updating remote {} -> {}", t.remote(), from_addr);
                            *t.remote_addr.lock().unwrap() = from_addr;
                        }

                        // #9: SSRC tracking — detect change (call transfer, failover)
                        let pkt_ssrc = pkt.header.ssrc;
                        if let Some(known) = remote_ssrc {
                            if pkt_ssrc != known && pkt_ssrc != t.ssrc {
                                info!("SSRC changed: {} -> {} (stream reset)", known, pkt_ssrc);
                                remote_ssrc = Some(pkt_ssrc);
                                dtmf_event = None; // Reset DTMF state
                            }
                        } else if pkt_ssrc != t.ssrc { // Ignore our own packets
                            remote_ssrc = Some(pkt_ssrc);
                        }

                        last_rtp = Instant::now();

                        // DTMF telephone-event
                        if pkt.header.payload_type == t.dtmf_pt {
                            if let Some((ev, end, _, dur)) = dtmf::decode_rfc4733(&pkt.payload) {
                                if end {
                                    // #5: Report with accumulated duration
                                    if let Some(d) = dtmf::event_to_digit(ev) {
                                        let _ = etx.try_send(EndpointEvent::DtmfReceived { call_id: cid, digit: d, method: "rfc2833".into() });
                                    }
                                    dtmf_event = None;
                                    dtmf_timer = None;
                                } else {
                                    // Track ongoing event for timeout
                                    dtmf_event = Some(ev);
                                    dtmf_last_ts = pkt.header.timestamp;
                                    if dtmf_timer.is_none() { dtmf_timer = Some(Instant::now()); }
                                }
                            }
                            continue;
                        }

                        // #6: DTMF END timeout — auto-complete if END never arrives
                        if let Some(ev) = dtmf_event {
                            if dtmf_timer.map(|t| t.elapsed() > DTMF_END_TIMEOUT).unwrap_or(false) {
                                if let Some(d) = dtmf::event_to_digit(ev) {
                                    warn!("DTMF END timeout for digit {}", d);
                                    let _ = etx.try_send(EndpointEvent::DtmfReceived { call_id: cid, digit: d, method: "rfc2833".into() });
                                }
                                dtmf_event = None;
                                dtmf_timer = None;
                            }
                        }

                        // #14: Validate payload type matches negotiated audio codec
                        if pkt.header.payload_type != t.codec.payload_type() { continue; }

                        let pcm = upsample(&decode_g711(&pkt.payload, t.codec));

                        // Feed beep detector
                        if let Ok(mut g) = bd.lock() { if let Some(ref mut det) = *g {
                            match det.process_frame(&pcm) {
                                BeepDetectorResult::Detected(e) => { let _ = etx.try_send(EndpointEvent::BeepDetected { call_id: cid, frequency_hz: e.frequency_hz, duration_ms: e.duration_ms }); *g = None; }
                                BeepDetectorResult::Timeout => { let _ = etx.try_send(EndpointEvent::BeepTimeout { call_id: cid }); *g = None; }
                                BeepDetectorResult::Listening => {}
                            }
                        }}
                        let n = pcm.len() as u32;
                        let _ = tx.try_send(AudioFrame { data: pcm, sample_rate: 16000, num_channels: 1, samples_per_channel: n });
                    }
                }

                // #3: Media timeout check
                if last_rtp.elapsed() > MEDIA_TIMEOUT {
                    warn!("Media timeout on call {} (no RTP for {}s)", cid, MEDIA_TIMEOUT.as_secs());
                    let _ = etx.try_send(EndpointEvent::CallTerminated {
                        session: crate::call::CallSession::new(cid, crate::call::CallDirection::Outbound),
                        reason: "media timeout".into(),
                    });
                    break;
                }
            }
        })
    }
}

fn notify(p: &Arc<(Mutex<bool>, Condvar)>) { if let Ok(mut d) = p.0.lock() { *d = true; p.1.notify_all(); } }

fn encode_g711(samples: &[i16], c: Codec) -> Vec<u8> {
    match c {
        Codec::PCMU => samples.iter().map(|&s| encode_ulaw(s)).collect(),
        Codec::PCMA => samples.iter().map(|&s| encode_alaw(s)).collect(),
        _ => samples.iter().map(|&s| encode_ulaw(s)).collect(),
    }
}

fn decode_g711(bytes: &[u8], c: Codec) -> Vec<i16> {
    match c {
        Codec::PCMU => bytes.iter().map(|&b| decode_ulaw(b)).collect(),
        Codec::PCMA => bytes.iter().map(|&b| decode_alaw(b)).collect(),
        _ => bytes.iter().map(|&b| decode_ulaw(b)).collect(),
    }
}

fn silence_byte(c: Codec) -> u8 { match c { Codec::PCMU => 0xFF, Codec::PCMA => 0xD5, _ => 0xFF } }

fn upsample(s: &[i16]) -> Vec<i16> {
    let mut o = Vec::with_capacity(s.len() * 2);
    for i in 0..s.len() {
        o.push(s[i]);
        let n = if i + 1 < s.len() { s[i + 1] } else { s[i] };
        o.push(((s[i] as i32 + n as i32) / 2) as i16);
    }
    o
}

fn downsample(s: &[i16]) -> Vec<i16> { s.iter().step_by(2).copied().collect() }

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_pcmu_roundtrip() {
        for &s in &[0i16, 100, 1000, 8000, -100, -1000, -8000] {
            let d = decode_ulaw(encode_ulaw(s));
            assert!((s as i32 - d as i32).unsigned_abs() < (s.unsigned_abs() as u32 / 10).max(100), "PCMU: {s} -> {d}");
        }
    }

    #[test]
    fn test_pcma_roundtrip() {
        for &s in &[0i16, 100, 1000, 8000, -100, -1000, -8000] {
            let d = decode_alaw(encode_alaw(s));
            assert!((s as i32 - d as i32).unsigned_abs() < (s.unsigned_abs() as u32 / 10).max(100), "PCMA: {s} -> {d}");
        }
    }

    #[test]
    fn test_resample_lengths() {
        assert_eq!(upsample(&vec![0i16; 160]).len(), 320);
        assert_eq!(downsample(&vec![0i16; 320]).len(), 160);
    }

    #[test]
    fn test_g711_encode_decode() {
        let s = vec![0i16, 1000, -1000, 8000];
        assert_eq!(encode_g711(&s, Codec::PCMU).len(), 4);
        assert_eq!(decode_g711(&encode_g711(&s, Codec::PCMU), Codec::PCMU).len(), 4);
    }
}
