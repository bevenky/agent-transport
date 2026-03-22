use crate::transport::SipConnection;
use rsip::{headers::*, prelude::HeadersExt, SipMessage, Transport};
use std::net::SocketAddr;

/// Test Via received parameter handling for different transport protocols
#[test]
fn test_via_received_udp() {
    let register_req = create_test_request("SIP/2.0/UDP");
    let addr: SocketAddr = "192.168.1.100:5060".parse().unwrap();

    let msg = SipConnection::update_msg_received(register_req.into(), addr, Transport::Udp)
        .expect("update_msg_received for UDP");

    match msg {
        SipMessage::Request(req) => {
            let via_header = req.via_header().expect("via header");
            let typed_via = via_header.typed().expect("typed via");

            // UDP should always add received parameter
            assert!(
                typed_via
                    .params
                    .iter()
                    .any(|p| matches!(p, rsip::Param::Received(_))),
                "UDP should add received parameter"
            );
            assert!(
                typed_via.params.iter().any(|p| matches!(
                    p, rsip::Param::Other(key, Some(_)) if key.value().eq_ignore_ascii_case("rport")
                )),
                "UDP should add rport parameter"
            );
        }
        _ => panic!("Expected request message"),
    }
}

#[test]
fn test_via_received_tcp() {
    let register_req = create_test_request("SIP/2.0/TCP");
    let addr: SocketAddr = "127.0.0.1:5060".parse().unwrap(); // Same as Via header

    let msg = SipConnection::update_msg_received(register_req.into(), addr, Transport::Tcp)
        .expect("update_msg_received for TCP");

    match msg {
        SipMessage::Request(req) => {
            let via_header = req.via_header().expect("via header");
            let typed_via = via_header.typed().expect("typed via");

            // TCP should not add received parameter if source matches Via
            assert!(
                !typed_via
                    .params
                    .iter()
                    .any(|p| matches!(p, rsip::Param::Received(_))),
                "TCP should not add received parameter when addresses match"
            );
        }
        _ => panic!("Expected request message"),
    }
}

#[test]
fn test_via_received_tcp_different_addr() {
    let register_req = create_test_request("SIP/2.0/TCP");
    let addr: SocketAddr = "192.168.1.100:5060".parse().unwrap(); // Different from Via header

    let msg = SipConnection::update_msg_received(register_req.into(), addr, Transport::Tcp)
        .expect("update_msg_received for TCP");

    match msg {
        SipMessage::Request(req) => {
            let via_header = req.via_header().expect("via header");
            let typed_via = via_header.typed().expect("typed via");

            // TCP should add received parameter if source differs from Via
            assert!(
                typed_via
                    .params
                    .iter()
                    .any(|p| matches!(p, rsip::Param::Received(_))),
                "TCP should add received parameter when addresses differ"
            );
        }
        _ => panic!("Expected request message"),
    }
}

#[test]
fn test_via_received_tls() {
    let register_req = create_test_request("SIP/2.0/TLS");
    let addr: SocketAddr = "192.168.1.100:5061".parse().unwrap();

    let msg = SipConnection::update_msg_received(register_req.into(), addr, Transport::Tls)
        .expect("update_msg_received for TLS");

    match msg {
        SipMessage::Request(req) => {
            let via_header = req.via_header().expect("via header");
            let typed_via = via_header.typed().expect("typed via");

            // TLS should add received parameter only if host differs
            assert!(
                typed_via
                    .params
                    .iter()
                    .any(|p| matches!(p, rsip::Param::Received(_))),
                "TLS should add received parameter when host differs"
            );
        }
        _ => panic!("Expected request message"),
    }
}

#[test]
fn test_via_received_ws() {
    let register_req = create_test_request("SIP/2.0/WS");
    let addr: SocketAddr = "192.168.1.100:80".parse().unwrap();

    let msg = SipConnection::update_msg_received(register_req.into(), addr, Transport::Ws)
        .expect("update_msg_received for WS");

    match msg {
        SipMessage::Request(req) => {
            let via_header = req.via_header().expect("via header");
            let typed_via = via_header.typed().expect("typed via");

            // WS should handle received parameter like other connection-oriented protocols
            assert!(
                typed_via
                    .params
                    .iter()
                    .any(|p| matches!(p, rsip::Param::Received(_))),
                "WS should add received parameter when host differs"
            );
        }
        _ => panic!("Expected request message"),
    }
}

#[test]
fn test_via_response_not_modified() {
    let response = rsip::message::Response {
        status_code: rsip::StatusCode::try_from(200).unwrap(),
        headers: vec![Via::new("SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bK-test").into()].into(),
        version: rsip::Version::V2,
        body: Default::default(),
    };

    let addr: SocketAddr = "192.168.1.100:5060".parse().unwrap();

    let msg = SipConnection::update_msg_received(response.into(), addr, Transport::Udp)
        .expect("update_msg_received for response");

    // Response messages should not be modified
    match msg {
        SipMessage::Response(_) => {
            // This is expected - responses are not modified
        }
        _ => panic!("Expected response message"),
    }
}

fn create_test_request(via_proto: &str) -> rsip::message::Request {
    rsip::message::Request {
        method: rsip::method::Method::Register,
        uri: rsip::Uri {
            scheme: Some(rsip::Scheme::Sip),
            host_with_port: rsip::HostWithPort::try_from("example.com:5060")
                .expect("host_port parse"),
            ..Default::default()
        },
        headers: vec![
            Via::new(&format!("{} 127.0.0.1:5060;branch=z9hG4bK-test", via_proto)).into(),
        ]
        .into(),
        version: rsip::Version::V2,
        body: Default::default(),
    }
}
