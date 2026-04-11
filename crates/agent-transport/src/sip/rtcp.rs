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

/// Detect whether a packet received on an RTP-muxed socket is RTCP or RTP.
///
/// Per RFC 5761 §4, when RTP and RTCP share a single transport (a=rtcp-mux),
/// peers distinguish them by looking at the payload-type field (byte 1 of
/// the packet, with the marker bit masked off):
///
/// * `PT & 0x7F` in `64..=95` → RTCP
/// * otherwise (0..=33 or 96..=127) → RTP
///
/// Returns `true` if the buffer looks like an RTCP packet and should be
/// handed to an RTCP handler (or skipped) rather than parsed as RTP.
///
/// The 72..=76 values we saw in debug logs correspond to the seven-bit
/// view of RTCP PTs 200 (SR), 201 (RR), 202 (SDES), 203 (BYE), 204 (APP):
/// `200 & 0x7F = 72`, `201 & 0x7F = 73`, etc.
pub fn is_rtcp(buf: &[u8]) -> bool {
    if buf.len() < 2 { return false; }
    let pt = buf[1] & 0x7F;
    (64..=95).contains(&pt)
}

#[cfg(test)]
mod tests {
    use super::*;

    // ─── is_rtcp (RFC 5761 RTP/RTCP de-mux) ──────────────────────────────

    #[test]
    fn rtp_pcmu_is_not_rtcp() {
        // M=0, PT=0 (PCMU)
        let buf = [0x80u8, 0x00, 0, 0];
        assert!(!is_rtcp(&buf));
    }

    #[test]
    fn rtp_dynamic_is_not_rtcp() {
        // M=1, PT=101 (DTMF telephone-event)
        let buf = [0x80u8, 0x80 | 101, 0, 0];
        assert!(!is_rtcp(&buf));
    }

    #[test]
    fn rtcp_sr_is_rtcp() {
        // RTCP Sender Report: PT=200 (0xC8)
        let buf = [0x80u8, 0xC8, 0, 0];
        assert!(is_rtcp(&buf));
    }

    #[test]
    fn rtcp_rr_is_rtcp() {
        // RTCP Receiver Report: PT=201 (0xC9)
        let buf = [0x81u8, 0xC9, 0, 0];
        assert!(is_rtcp(&buf));
    }

    #[test]
    fn rtcp_sdes_bye_app_are_rtcp() {
        for pt in [202u8, 203, 204] {
            let buf = [0x80u8, pt, 0, 0];
            assert!(is_rtcp(&buf), "PT {} should be RTCP", pt);
        }
    }

    #[test]
    fn short_buffer_is_not_rtcp() {
        // Too short to classify — treat as non-RTCP (caller will drop below 12 bytes).
        assert!(!is_rtcp(&[]));
        assert!(!is_rtcp(&[0x80]));
    }

    #[test]
    fn rtp_pts_near_boundary_are_not_rtcp() {
        // PT=33 is the last static RTP type; not RTCP.
        let buf = [0x80u8, 33, 0, 0];
        assert!(!is_rtcp(&buf));
        // PT=96 is the first dynamic RTP type; not RTCP.
        let buf = [0x80u8, 96, 0, 0];
        assert!(!is_rtcp(&buf));
    }

    #[test]
    fn rtcp_boundaries_are_rtcp() {
        // 64 and 95 are the RFC 5761 RTCP boundary values.
        let buf = [0x80u8, 64, 0, 0];
        assert!(is_rtcp(&buf));
        let buf = [0x80u8, 95, 0, 0];
        assert!(is_rtcp(&buf));
    }

    #[test]
    fn marker_bit_is_ignored() {
        // M=1 on top of an RTCP PT (via 0x80 flag). Still RTCP.
        let buf = [0x80u8, 0x80 | 72, 0, 0];
        assert!(is_rtcp(&buf));
    }

    // ─── build_sender_report / build_receiver_report ─────────────────────

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
