//! Regression tests for issue #50: stale Contact after public IP change.
//!
//! Scenario:
//! 1. Agent registers with Plivo using STUN-discovered public IP A
//! 2. WiFi switches → NAT mapping changes → public IP becomes B
//! 3. Re-registration cycle fires
//! 4. BEFORE THE FIX: reg.contact still points to IP A → Plivo routes
//!    incoming INVITEs to the dead IP → caller gets 486 Busy Here
//! 5. AFTER THE FIX: detect_public_ip_change() re-runs STUN on every
//!    re-reg cycle and rebuilds the Contact when the IP has moved
//!
//! These tests drive `detect_public_ip_change` directly against a local
//! mock STUN server so we can simulate the IP change without real
//! network instability.

#![cfg(test)]

use std::net::SocketAddr;

use crate::sip::endpoint::{detect_public_ip_change, PublicAddrRefresh};
use crate::sip::test_stun::MockStunServer;

fn parse_sa(s: &str) -> SocketAddr {
    s.parse().expect("valid SocketAddr")
}

/// Helper: extract the "ip:port" string from an `rsip::HostWithPort`
/// produced by `detect_public_ip_change`.
fn hp_to_string(hp: &rsip::HostWithPort) -> String {
    let port = hp.port.as_ref().map(|p| u16::from(*p)).unwrap_or(0);
    format!("{}:{}", hp.host, port)
}

/// First call — no prior public address cached. Should always return
/// `Changed` with whatever STUN reports, so the initial registration has
/// a Contact to work with.
#[tokio::test]
async fn first_call_returns_changed_with_initial_stun_result() {
    let mock = MockStunServer::start(parse_sa("198.51.100.10:5060")).unwrap();

    let refresh = detect_public_ip_change(&mock.addr_str(), None, "alice").await;
    match refresh {
        PublicAddrRefresh::Changed { new_hp, new_sa, new_contact } => {
            assert_eq!(hp_to_string(&new_hp), "198.51.100.10:5060");
            assert_eq!(new_sa.ip().to_string(), "198.51.100.10");
            // Contact URI carries the new IP and the right user.
            let uri_str = new_contact.uri.to_string();
            assert!(
                uri_str.contains("alice@198.51.100.10:5060"),
                "expected contact to contain alice@198.51.100.10:5060, got: {}",
                uri_str
            );
            // `transport=tcp` is parsed into a URI parameter by rsip so
            // it doesn't always round-trip in `to_string()`. Verify the
            // param is present structurally instead.
            let has_tcp_param = new_contact.uri.params.iter().any(|p| {
                matches!(p, rsip::uri::Param::Transport(rsip::Transport::Tcp))
            });
            assert!(
                has_tcp_param,
                "contact URI should carry transport=tcp param, got params: {:?}",
                new_contact.uri.params
            );
        }
        other => panic!("expected Changed, got {:?}", other),
    }
}

/// STUN returns the same address we already have → no-op. This is the
/// common case every time the re-reg timer fires and nothing has changed.
#[tokio::test]
async fn unchanged_ip_returns_unchanged() {
    let mock = MockStunServer::start(parse_sa("198.51.100.10:5060")).unwrap();

    // First call populates the cache.
    let first = detect_public_ip_change(&mock.addr_str(), None, "alice").await;
    let cached_hp = match first {
        PublicAddrRefresh::Changed { new_hp, .. } => Some(new_hp),
        _ => panic!("first call should return Changed"),
    };

    // Second call with STUN still reporting the same address.
    let refresh = detect_public_ip_change(&mock.addr_str(), cached_hp, "alice").await;
    match refresh {
        PublicAddrRefresh::Unchanged => {} // expected
        other => panic!("expected Unchanged, got {:?}", other),
    }

    // And the server did see both requests — we didn't short-circuit
    // STUN on the second call.
    assert!(mock.request_count() >= 2);
}

/// The core reproduction: simulate a WiFi/ISP switch by changing what
/// the mock STUN server reports between two calls. The helper must
/// detect the change and return a new Contact carrying IP B.
#[tokio::test]
async fn ip_change_rebuilds_contact() {
    let mock = MockStunServer::start(parse_sa("198.51.100.10:5060")).unwrap();

    // Initial registration phase: cache IP A.
    let first = detect_public_ip_change(&mock.addr_str(), None, "alice").await;
    let cached_hp = match first {
        PublicAddrRefresh::Changed { new_hp, .. } => Some(new_hp),
        _ => panic!("first call should return Changed"),
    };

    // Simulate the WiFi switch → NAT rebinds → different public address.
    mock.set_mapped_address(parse_sa("203.0.113.99:5070"));

    // Next re-registration cycle fires → helper re-runs STUN.
    let refresh = detect_public_ip_change(&mock.addr_str(), cached_hp, "alice").await;
    match refresh {
        PublicAddrRefresh::Changed { new_hp, new_sa, new_contact } => {
            assert_eq!(hp_to_string(&new_hp), "203.0.113.99:5070");
            assert_eq!(new_sa.ip().to_string(), "203.0.113.99");
            assert_eq!(new_sa.port(), 5070);

            // The rebuilt Contact carries the NEW IP, not the old one.
            let uri_str = new_contact.uri.to_string();
            assert!(
                uri_str.contains("alice@203.0.113.99:5070"),
                "expected new contact to contain 203.0.113.99:5070, got: {}",
                uri_str
            );
            assert!(
                !uri_str.contains("198.51.100.10"),
                "contact must not carry the stale IP: {}",
                uri_str
            );
        }
        other => panic!("expected Changed after IP switch, got {:?}", other),
    }
}

/// NAT rebind on the same uplink: public IP stays the same but the
/// mapped port changes. Must still be treated as a change so the new
/// port lands in the Contact header — otherwise Plivo's PINGs hit a
/// closed port.
#[tokio::test]
async fn port_only_change_rebuilds_contact() {
    let mock = MockStunServer::start(parse_sa("198.51.100.10:5060")).unwrap();

    let first = detect_public_ip_change(&mock.addr_str(), None, "alice").await;
    let cached_hp = match first {
        PublicAddrRefresh::Changed { new_hp, .. } => Some(new_hp),
        _ => unreachable!(),
    };

    // Same IP, different port (NAT rebind).
    mock.set_mapped_address(parse_sa("198.51.100.10:5099"));

    let refresh = detect_public_ip_change(&mock.addr_str(), cached_hp, "alice").await;
    match refresh {
        PublicAddrRefresh::Changed { new_hp, .. } => {
            assert_eq!(hp_to_string(&new_hp), "198.51.100.10:5099");
        }
        other => panic!("expected Changed on port change, got {:?}", other),
    }
}

/// STUN unreachable → helper must report `StunFailed` so the caller
/// keeps using the cached Contact. The re-registration cycle proceeds
/// with the stale Contact rather than crashing — worst case we hit the
/// original bug until the next cycle succeeds.
#[tokio::test]
async fn stun_unreachable_reports_stun_failed() {
    // Bind a UDP socket just to reserve the port, then immediately drop
    // it so the port is closed when the helper tries to hit it.
    let unused = std::net::UdpSocket::bind("127.0.0.1:0").unwrap();
    let closed_addr = unused.local_addr().unwrap();
    drop(unused);

    let refresh = detect_public_ip_change(&closed_addr.to_string(), None, "alice").await;
    match refresh {
        PublicAddrRefresh::StunFailed => {} // expected
        other => panic!("expected StunFailed, got {:?}", other),
    }
}

/// End-to-end sanity: two real roundtrips through the mock server
/// (proves `detect_public_ip_change` actually hits the server twice and
/// doesn't cache internally).
#[tokio::test]
async fn helper_queries_stun_on_every_call() {
    let mock = MockStunServer::start(parse_sa("198.51.100.10:5060")).unwrap();

    let _ = detect_public_ip_change(&mock.addr_str(), None, "alice").await;
    let count_after_first = mock.request_count();

    // Simulate a re-registration tick — even if nothing has changed we
    // still want to VERIFY (not assume) that the IP is still valid.
    let _ = detect_public_ip_change(
        &mock.addr_str(),
        Some(parse_sa("198.51.100.10:5060").into_hp()),
        "alice",
    )
    .await;
    let count_after_second = mock.request_count();

    assert!(
        count_after_second > count_after_first,
        "helper must hit STUN on every call, not short-circuit on cached state"
    );
}

/// Simulates several re-registration cycles in sequence, including a
/// WiFi switch in the middle. Mirrors the exact sequence the re-reg
/// loop runs: read cached public address → call `detect_public_ip_change`
/// → (if Changed) apply the new contact → repeat.
///
/// We don't actually call `reg.register()` — that needs a real SIP
/// server. The fix for issue #50 is the STUN refresh step that runs
/// before each register, and this test verifies that step works
/// correctly across a multi-cycle simulated run.
#[tokio::test]
async fn multi_cycle_reregistration_tracks_ip_changes() {
    let mock = MockStunServer::start(parse_sa("198.51.100.10:5060")).unwrap();

    // Local state mirrors what the re-reg loop keeps in `reg.public_address`
    // and `state.contact_uri`. Start with nothing — first cycle populates.
    let mut cached_hp: Option<rsip::HostWithPort> = None;
    let mut cached_contact_uri: Option<String> = None;

    // ── Cycle 0: initial registration — cache IP A ──────────────────
    let refresh = detect_public_ip_change(&mock.addr_str(), cached_hp.clone(), "alice").await;
    if let PublicAddrRefresh::Changed { new_contact, new_hp, .. } = refresh {
        cached_contact_uri = Some(new_contact.uri.to_string());
        cached_hp = Some(new_hp);
    } else {
        panic!("cycle 0: expected Changed");
    }
    assert!(cached_contact_uri.as_ref().unwrap().contains("198.51.100.10"));

    // ── Cycle 1: same IP — Unchanged, no state mutation ─────────────
    let prev_uri = cached_contact_uri.clone();
    let refresh = detect_public_ip_change(&mock.addr_str(), cached_hp.clone(), "alice").await;
    assert!(matches!(refresh, PublicAddrRefresh::Unchanged));
    assert_eq!(cached_contact_uri, prev_uri, "state must not churn on unchanged IP");

    // ── WiFi switch! Public IP moves to B ───────────────────────────
    mock.set_mapped_address(parse_sa("203.0.113.99:5070"));

    // ── Cycle 2: detect the change, update Contact + cached state ───
    let refresh = detect_public_ip_change(&mock.addr_str(), cached_hp.clone(), "alice").await;
    if let PublicAddrRefresh::Changed { new_contact, new_hp, new_sa } = refresh {
        assert_eq!(new_sa.ip().to_string(), "203.0.113.99");
        assert_eq!(new_sa.port(), 5070);
        cached_contact_uri = Some(new_contact.uri.to_string());
        cached_hp = Some(new_hp);
    } else {
        panic!("cycle 2: expected Changed after WiFi switch");
    }
    assert!(
        cached_contact_uri.as_ref().unwrap().contains("203.0.113.99:5070"),
        "contact must carry the new IP after WiFi switch"
    );
    assert!(
        !cached_contact_uri.as_ref().unwrap().contains("198.51.100.10"),
        "contact must NOT carry the stale IP after WiFi switch"
    );

    // ── Cycle 3: still on the new network, no more churn ───────────
    let refresh = detect_public_ip_change(&mock.addr_str(), cached_hp.clone(), "alice").await;
    assert!(matches!(refresh, PublicAddrRefresh::Unchanged));

    // ── WiFi switches BACK (rare but possible: LTE → WiFi → LTE) ───
    mock.set_mapped_address(parse_sa("198.51.100.10:5060"));

    // ── Cycle 4: detect the reverse change ──────────────────────────
    let refresh = detect_public_ip_change(&mock.addr_str(), cached_hp.clone(), "alice").await;
    if let PublicAddrRefresh::Changed { new_contact, .. } = refresh {
        let uri_str = new_contact.uri.to_string();
        assert!(uri_str.contains("198.51.100.10"));
        assert!(!uri_str.contains("203.0.113.99"));
    } else {
        panic!("cycle 4: expected Changed on reverse switch");
    }

    // Server saw a STUN request for every cycle (5 total). This is the
    // key property: no internal caching — we verify the current IP
    // every single re-reg tick.
    assert!(
        mock.request_count() >= 5,
        "expected ≥5 STUN requests for 5 cycles, got {}",
        mock.request_count()
    );
}

/// Small shim: lets the test build a `rsip::HostWithPort` from a
/// `SocketAddr` without cluttering every test with the parse ceremony.
trait IntoHp {
    fn into_hp(self) -> rsip::HostWithPort;
}
impl IntoHp for SocketAddr {
    fn into_hp(self) -> rsip::HostWithPort {
        format!("{}:{}", self.ip(), self.port())
            .try_into()
            .expect("valid host:port")
    }
}
