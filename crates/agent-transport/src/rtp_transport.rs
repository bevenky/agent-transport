//! RTP transport — audio send/recv over UDP with G.711 codec.

use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, AtomicU16, AtomicU32, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

use audio_codec_algorithms::{decode_alaw, decode_ulaw, encode_alaw, encode_ulaw};
use crossbeam_channel::{Receiver, Sender};
use rtp::{header::Header, packet::Packet};
use tokio::net::UdpSocket;
use tokio_util::sync::CancellationToken;
use tracing::warn;
use webrtc_util::marshal::{Marshal, MarshalSize, Unmarshal};

use beep_detector::{BeepDetector, BeepDetectorResult};
use crate::audio::AudioFrame;
use crate::config::Codec;
use crate::dtmf;
use crate::events::EndpointEvent;

pub(crate) const DTMF_PAYLOAD_TYPE: u8 = 101;

pub(crate) struct RtpTransport {
    pub socket: Arc<UdpSocket>,
    pub remote_addr: SocketAddr,
    ssrc: u32,
    codec: Codec,
    seq: AtomicU16,
    timestamp: AtomicU32,
    pub cancel: CancellationToken,
}

impl RtpTransport {
    pub fn new(socket: Arc<UdpSocket>, remote_addr: SocketAddr, codec: Codec, cancel: CancellationToken) -> Self {
        Self { socket, remote_addr, ssrc: rand::random(), codec, seq: AtomicU16::new(0), timestamp: AtomicU32::new(0), cancel }
    }

    async fn send_rtp(&self, pt: u8, ts: u32, marker: bool, payload: Vec<u8>) -> std::io::Result<()> {
        let pkt = Packet {
            header: Header { version: 2, marker, payload_type: pt, sequence_number: self.seq.fetch_add(1, Ordering::Relaxed), timestamp: ts, ssrc: self.ssrc, ..Default::default() },
            payload: bytes::Bytes::from(payload),
        };
        self.socket.send_to(&pkt.marshal().map_err(|e| std::io::Error::other(e.to_string()))?, self.remote_addr).await?;
        Ok(())
    }

    pub async fn send_dtmf_event(&self, digit: char, dur: u16) -> std::io::Result<()> {
        let ev = dtmf::digit_to_event(digit).unwrap_or(0);
        let ts = self.timestamp.load(Ordering::Relaxed);
        self.send_rtp(DTMF_PAYLOAD_TYPE, ts, true, dtmf::encode_rfc4733(ev, false, 10, 0).to_vec()).await?;
        for _ in 0..3 { self.send_rtp(DTMF_PAYLOAD_TYPE, ts, false, dtmf::encode_rfc4733(ev, true, 10, dur).to_vec()).await?; }
        Ok(())
    }

    pub fn start_send_loop(self: &Arc<Self>, rx: Receiver<Vec<i16>>, muted: Arc<AtomicBool>, paused: Arc<AtomicBool>, flush: Arc<AtomicBool>, playout: Arc<(Mutex<bool>, Condvar)>) -> tokio::task::JoinHandle<()> {
        let t = Arc::clone(self);
        tokio::spawn(async move {
            let mut iv = tokio::time::interval(Duration::from_millis(20));
            let sil = silence_byte(t.codec);
            loop {
                tokio::select! { _ = t.cancel.cancelled() => break, _ = iv.tick() => {} }
                let ts = t.timestamp.fetch_add(160, Ordering::Relaxed);
                if flush.swap(false, Ordering::Relaxed) { while rx.try_recv().is_ok() {} notify(&playout); continue; }
                if paused.load(Ordering::Relaxed) { let _ = t.send_rtp(t.codec.payload_type(), ts, false, vec![sil; 160]).await; continue; }
                let payload = match rx.try_recv() {
                    Ok(s) if !muted.load(Ordering::Relaxed) => encode_g711(&downsample(&s), t.codec),
                    Ok(_) => vec![sil; 160],
                    Err(_) => { notify(&playout); vec![sil; 160] }
                };
                let _ = t.send_rtp(t.codec.payload_type(), ts, false, payload).await;
            }
        })
    }

    pub fn start_recv_loop(self: &Arc<Self>, tx: Sender<AudioFrame>, etx: Sender<EndpointEvent>, cid: i32, bd: Arc<Mutex<Option<BeepDetector>>>) -> tokio::task::JoinHandle<()> {
        let t = Arc::clone(self);
        tokio::spawn(async move {
            let mut buf = vec![0u8; 2048];
            loop {
                tokio::select! {
                    _ = t.cancel.cancelled() => break,
                    r = t.socket.recv_from(&mut buf) => {
                        let (len, _) = match r { Ok(r) => r, Err(_) => continue };
                        let pkt = match Packet::unmarshal(&mut &buf[..len]) { Ok(p) => p, Err(_) => continue };
                        if pkt.header.payload_type == DTMF_PAYLOAD_TYPE {
                            if let Some((ev, end, _, _)) = dtmf::decode_rfc4733(&pkt.payload) {
                                if end { if let Some(d) = dtmf::event_to_digit(ev) { let _ = etx.try_send(EndpointEvent::DtmfReceived { call_id: cid, digit: d, method: "rfc2833".into() }); } }
                            }
                            continue;
                        }
                        let pcm = upsample(&decode_g711(&pkt.payload, t.codec));
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

/// 8kHz → 16kHz linear interpolation
fn upsample(s: &[i16]) -> Vec<i16> {
    let mut o = Vec::with_capacity(s.len() * 2);
    for i in 0..s.len() {
        o.push(s[i]);
        let n = if i + 1 < s.len() { s[i + 1] } else { s[i] };
        o.push(((s[i] as i32 + n as i32) / 2) as i16);
    }
    o
}

/// 16kHz → 8kHz decimation
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
