//! Basic RTCP Sender Report (SR) and Receiver Report (RR).
//!
//! Sends periodic RTCP SR to let Plivo monitor call quality.
//! Minimal implementation — enough for quality stats, not full RFC 3550.

use std::time::{SystemTime, UNIX_EPOCH};

/// RTCP packet type constants
const RTCP_SR: u8 = 200;
const RTCP_RR: u8 = 201;

/// NTP timestamp: seconds since 1900-01-01
// Note: NTP seconds wrap to 0 in year 2036 (u32 overflow). Acceptable for RTCP SR.
fn ntp_timestamp() -> (u32, u32) {
    let since_epoch = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default();
    // NTP epoch is 70 years before Unix epoch
    let ntp_secs = since_epoch.as_secs() + 2_208_988_800;
    let ntp_frac = ((since_epoch.subsec_nanos() as u64) << 32) / 1_000_000_000;
    (ntp_secs as u32, ntp_frac as u32)
}

/// Build an RTCP Sender Report packet.
///
/// Fields:
/// - ssrc: our SSRC
/// - rtp_timestamp: current RTP timestamp
/// - packet_count: total RTP packets sent
/// - octet_count: total RTP payload bytes sent
pub fn build_sender_report(ssrc: u32, rtp_timestamp: u32, packet_count: u32, octet_count: u32) -> Vec<u8> {
    let (ntp_sec, ntp_frac) = ntp_timestamp();
    let mut buf = Vec::with_capacity(28);

    // Header: V=2, P=0, RC=0, PT=200, length=6 (words - 1)
    buf.push(0x80); // V=2, P=0, RC=0
    buf.push(RTCP_SR);
    buf.extend_from_slice(&6u16.to_be_bytes()); // length in 32-bit words minus 1

    // SSRC
    buf.extend_from_slice(&ssrc.to_be_bytes());

    // NTP timestamp (8 bytes)
    buf.extend_from_slice(&ntp_sec.to_be_bytes());
    buf.extend_from_slice(&ntp_frac.to_be_bytes());

    // RTP timestamp
    buf.extend_from_slice(&rtp_timestamp.to_be_bytes());

    // Sender's packet count
    buf.extend_from_slice(&packet_count.to_be_bytes());

    // Sender's octet count
    buf.extend_from_slice(&octet_count.to_be_bytes());

    buf
}

/// Build an RTCP Receiver Report packet.
///
/// Minimal RR with one report block.
pub fn build_receiver_report(
    ssrc: u32,           // our SSRC
    remote_ssrc: u32,    // SSRC we're reporting on
    fraction_lost: u8,   // fraction of packets lost (0-255)
    packets_lost: u32,   // cumulative packets lost (24-bit)
    highest_seq: u32,    // extended highest sequence number
    jitter: u32,         // interarrival jitter
) -> Vec<u8> {
    let mut buf = Vec::with_capacity(32);

    // Header: V=2, P=0, RC=1, PT=201, length=7
    buf.push(0x81); // V=2, P=0, RC=1
    buf.push(RTCP_RR);
    buf.extend_from_slice(&7u16.to_be_bytes());

    // Reporter SSRC
    buf.extend_from_slice(&ssrc.to_be_bytes());

    // Report block
    buf.extend_from_slice(&remote_ssrc.to_be_bytes());

    // Fraction lost (8 bits) + cumulative lost (24 bits)
    let lost_word = ((fraction_lost as u32) << 24) | (packets_lost & 0x00FF_FFFF);
    buf.extend_from_slice(&lost_word.to_be_bytes());

    // Extended highest sequence number
    buf.extend_from_slice(&highest_seq.to_be_bytes());

    // Interarrival jitter
    buf.extend_from_slice(&jitter.to_be_bytes());

    // Last SR timestamp (LSR) — we don't track incoming SR, set to 0
    buf.extend_from_slice(&0u32.to_be_bytes());

    // Delay since last SR (DLSR) — 0
    buf.extend_from_slice(&0u32.to_be_bytes());

    buf
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_sender_report_size() {
        let sr = build_sender_report(0x12345678, 1000, 50, 8000);
        assert_eq!(sr.len(), 28); // 7 words × 4 bytes
        assert_eq!(sr[0], 0x80); // V=2, P=0, RC=0
        assert_eq!(sr[1], 200);  // PT=SR
    }

    #[test]
    fn test_receiver_report_size() {
        let rr = build_receiver_report(0x12345678, 0xABCDEF01, 0, 0, 100, 5);
        assert_eq!(rr.len(), 32); // 8 words × 4 bytes
        assert_eq!(rr[0], 0x81); // V=2, P=0, RC=1
        assert_eq!(rr[1], 201);  // PT=RR
    }

    #[test]
    fn test_sender_report_ssrc() {
        let sr = build_sender_report(0x12345678, 0, 0, 0);
        let ssrc = u32::from_be_bytes([sr[4], sr[5], sr[6], sr[7]]);
        assert_eq!(ssrc, 0x12345678);
    }

    #[test]
    fn test_receiver_report_fraction_lost() {
        let rr = build_receiver_report(0x11111111, 0x22222222, 128, 10, 500, 20);
        // Fraction lost is in byte 16 (first byte after report block SSRC)
        let lost_word = u32::from_be_bytes([rr[12], rr[13], rr[14], rr[15]]);
        assert_eq!(rr[12], 128); // fraction_lost
        assert_eq!(lost_word & 0x00FFFFFF, 10); // cumulative lost
    }
}
