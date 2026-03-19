//! SDP offer/answer construction and STUN binding for public IP discovery.

use std::net::{IpAddr, SocketAddr};

use crate::config::Codec;
use crate::error::Result;

/// Parsed SDP answer from remote party.
#[derive(Debug, Clone)]
pub(crate) struct SdpAnswer {
    pub remote_ip: IpAddr,
    pub remote_port: u16,
    pub codec: Codec,
    pub payload_type: u8,
    pub dtmf_payload_type: Option<u8>,
}

/// Build an SDP offer for an audio-only call.
pub(crate) fn build_offer(
    _local_ip: IpAddr,
    _rtp_port: u16,
    _codecs: &[Codec],
) -> String {
    todo!("SDP offer construction")
}

/// Parse an SDP answer to extract remote RTP address and codec.
pub(crate) fn parse_answer(_sdp_bytes: &[u8]) -> Result<SdpAnswer> {
    todo!("SDP answer parsing")
}

/// Discover our public IP via STUN binding request.
pub(crate) async fn stun_binding(_stun_server: &str) -> Result<SocketAddr> {
    todo!("STUN binding request")
}
