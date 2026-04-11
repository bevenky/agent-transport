//! Minimal STUN server for tests.
//!
//! Responds to any STUN Binding Request on a local UDP port with a
//! configurable XOR-MAPPED-ADDRESS. Exposes `set_mapped_address()` so
//! tests can simulate a public-IP change (WiFi/ISP failover, NAT rebind)
//! mid-session and observe how the SIP stack reacts.
//!
//! Implements just enough of RFC 5389 to satisfy `sdp::stun_binding()`'s
//! parser — not a conformant STUN server.

#![cfg(test)]

use std::net::{IpAddr, Ipv4Addr, SocketAddr, UdpSocket};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::Duration;

const STUN_MAGIC_COOKIE: u32 = 0x2112_A442;
const STUN_BINDING_REQUEST: u16 = 0x0001;
const STUN_BINDING_RESPONSE: u16 = 0x0101;
const ATTR_XOR_MAPPED_ADDRESS: u16 = 0x0020;
const FAMILY_IPV4: u8 = 0x01;

/// Mock STUN server bound to a local UDP port. Responds to Binding
/// Requests with whatever the current `mapped_address` says.
pub(crate) struct MockStunServer {
    local_addr: SocketAddr,
    mapped: Arc<Mutex<SocketAddr>>,
    shutdown: Arc<AtomicBool>,
    handle: Option<JoinHandle<()>>,
    request_count: Arc<std::sync::atomic::AtomicU32>,
}

impl MockStunServer {
    /// Start a new mock STUN server. Binds an ephemeral UDP port on
    /// 127.0.0.1 and begins responding to Binding Requests.
    pub fn start(initial_mapped: SocketAddr) -> std::io::Result<Self> {
        let sock = UdpSocket::bind("127.0.0.1:0")?;
        sock.set_read_timeout(Some(Duration::from_millis(100)))?;
        let local_addr = sock.local_addr()?;

        let mapped = Arc::new(Mutex::new(initial_mapped));
        let shutdown = Arc::new(AtomicBool::new(false));
        let request_count = Arc::new(std::sync::atomic::AtomicU32::new(0));

        let mapped_clone = mapped.clone();
        let shutdown_clone = shutdown.clone();
        let count_clone = request_count.clone();

        let handle = std::thread::spawn(move || {
            let mut buf = [0u8; 1024];
            while !shutdown_clone.load(Ordering::Relaxed) {
                let (len, from) = match sock.recv_from(&mut buf) {
                    Ok(v) => v,
                    Err(_) => continue, // timeout tick — check shutdown flag
                };

                if len < 20 {
                    continue;
                }

                let msg_type = u16::from_be_bytes([buf[0], buf[1]]);
                if msg_type != STUN_BINDING_REQUEST {
                    continue;
                }

                // Echo the transaction id.
                let mut txn = [0u8; 12];
                txn.copy_from_slice(&buf[8..20]);

                let current = *mapped_clone.lock().unwrap();
                count_clone.fetch_add(1, Ordering::Relaxed);

                let response = build_binding_response(&txn, current);
                let _ = sock.send_to(&response, from);
            }
        });

        Ok(Self {
            local_addr,
            mapped,
            shutdown,
            handle: Some(handle),
            request_count,
        })
    }

    /// Address the server is listening on. Use this as the `stun_server`
    /// config value when constructing a `SipEndpoint` in tests.
    pub fn addr_str(&self) -> String {
        self.local_addr.to_string()
    }

    /// Change the mapped address the server will report on subsequent
    /// Binding Requests. Simulates a public-IP change.
    pub fn set_mapped_address(&self, new_mapped: SocketAddr) {
        *self.mapped.lock().unwrap() = new_mapped;
    }

    /// Number of Binding Requests the server has answered so far. Tests
    /// use this to verify the client actually re-ran STUN rather than
    /// relying on a cached value.
    pub fn request_count(&self) -> u32 {
        self.request_count.load(Ordering::Relaxed)
    }
}

impl Drop for MockStunServer {
    fn drop(&mut self) {
        self.shutdown.store(true, Ordering::Relaxed);
        if let Some(h) = self.handle.take() {
            let _ = h.join();
        }
    }
}

/// Build a STUN Binding Success Response with XOR-MAPPED-ADDRESS (IPv4).
fn build_binding_response(txn_id: &[u8; 12], mapped: SocketAddr) -> Vec<u8> {
    let mut out = Vec::with_capacity(32);

    // STUN message header — type, placeholder length, magic cookie, txn id
    out.extend_from_slice(&STUN_BINDING_RESPONSE.to_be_bytes());
    out.extend_from_slice(&0u16.to_be_bytes()); // length placeholder
    out.extend_from_slice(&STUN_MAGIC_COOKIE.to_be_bytes());
    out.extend_from_slice(txn_id);

    // XOR-MAPPED-ADDRESS attribute (12 bytes total for IPv4: 4 TLV + 8 body)
    out.extend_from_slice(&ATTR_XOR_MAPPED_ADDRESS.to_be_bytes());
    out.extend_from_slice(&8u16.to_be_bytes()); // attribute length = 8

    // Body: reserved(1) + family(1) + xor-port(2) + xor-ip(4)
    out.push(0x00);
    out.push(FAMILY_IPV4);

    let xor_port = mapped.port() ^ ((STUN_MAGIC_COOKIE >> 16) as u16);
    out.extend_from_slice(&xor_port.to_be_bytes());

    let ip_v4 = match mapped.ip() {
        IpAddr::V4(v4) => v4,
        IpAddr::V6(_) => Ipv4Addr::UNSPECIFIED, // mock only does IPv4
    };
    let ip_u32 = u32::from_be_bytes(ip_v4.octets());
    let xor_ip = ip_u32 ^ STUN_MAGIC_COOKIE;
    out.extend_from_slice(&xor_ip.to_be_bytes());

    // Fix up message length (everything after the 20-byte header).
    let body_len = (out.len() - 20) as u16;
    out[2..4].copy_from_slice(&body_len.to_be_bytes());

    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mock_stun_roundtrips_through_real_parser() {
        // This exercises the full path: mock server → UDP → real
        // `sdp::stun_binding()` parser → SocketAddr. If the wire format
        // is wrong, stun_binding will fail or return garbage.
        use crate::sip::sdp;

        let mock = MockStunServer::start("203.0.113.99:5060".parse().unwrap()).unwrap();

        let addr = sdp::stun_binding(&mock.addr_str()).expect("stun_binding should succeed");
        assert_eq!(addr.ip().to_string(), "203.0.113.99");
        assert_eq!(addr.port(), 5060);
    }

    #[test]
    fn mock_stun_reflects_changes() {
        use crate::sip::sdp;

        let mock = MockStunServer::start("1.1.1.1:5060".parse().unwrap()).unwrap();

        let first = sdp::stun_binding(&mock.addr_str()).unwrap();
        assert_eq!(first.ip().to_string(), "1.1.1.1");

        // Simulate WiFi switch → public IP changes.
        mock.set_mapped_address("2.2.2.2:5061".parse().unwrap());

        let second = sdp::stun_binding(&mock.addr_str()).unwrap();
        assert_eq!(second.ip().to_string(), "2.2.2.2");
        assert_eq!(second.port(), 5061);

        // Server saw both requests.
        assert!(mock.request_count() >= 2);
    }
}
