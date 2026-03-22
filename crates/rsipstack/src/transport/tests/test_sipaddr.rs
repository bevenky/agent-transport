use crate::transport::{SipAddr, SipConnection};
use rsip::{headers::*, prelude::HeadersExt, HostWithPort, SipMessage};

#[test]
fn test_via_received() {
    let register_req = rsip::message::Request {
        method: rsip::method::Method::Register,
        uri: rsip::Uri {
            scheme: Some(rsip::Scheme::Sip),
            host_with_port: rsip::HostWithPort::try_from("127.0.0.1:2025")
                .expect("host_port parse")
                .into(),
            ..Default::default()
        },
        headers: vec![Via::new("SIP/2.0/TLS restsend.com:5061;branch=z9hG4bKnashd92").into()]
            .into(),
        version: rsip::Version::V2,
        body: Default::default(),
    };

    let (_, parse_addr) =
        SipConnection::parse_target_from_via(&register_req.via_header().expect("via_header"))
            .expect("get_target_socketaddr");

    let addr = HostWithPort {
        host: "restsend.com".parse().unwrap(),
        port: Some(5061.into()),
    };
    assert_eq!(parse_addr, addr);

    let addr = "127.0.0.1:1234".parse().unwrap();
    let msg = SipConnection::update_msg_received(
        register_req.into(),
        addr,
        rsip::transport::Transport::Udp,
    )
    .expect("update_msg_received");

    match msg {
        SipMessage::Request(req) => {
            let (_, parse_addr) =
                SipConnection::parse_target_from_via(&req.via_header().expect("via_header"))
                    .expect("get_target_socketaddr");
            assert_eq!(parse_addr, addr.into());
        }
        _ => {}
    }
}

#[test]
fn test_sipaddr() {
    let addr = "sip:proxy1.example.org:25060;transport=tcp";
    let uri = rsip::Uri::try_from(addr).expect("parse uri");
    let sipaddr = SipAddr::try_from(&uri).expect("SipAddr::try_from");
    assert_eq!(sipaddr.r#type, Some(rsip::transport::Transport::Tcp));
    assert_eq!(
        sipaddr.addr,
        rsip::HostWithPort {
            host: "proxy1.example.org".parse().unwrap(),
            port: Some(25060.into()),
        }
    );
}
