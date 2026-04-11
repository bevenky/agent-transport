use crate::Result;
use rsip::{host_with_port, HostWithPort, Transport};
use std::{fmt, hash::Hash, net::SocketAddr};

/// SIP Address
///
/// `SipAddr` represents a SIP network address that combines a host/port
/// with an optional transport protocol. It provides a unified way to
/// handle SIP addressing across different transport types.
///
/// # Fields
///
/// * `r#type` - Optional transport protocol (UDP, TCP, TLS, WS, WSS)
/// * `addr` - Host and port information
///
/// # Transport Types
///
/// * `UDP` - User Datagram Protocol (unreliable)
/// * `TCP` - Transmission Control Protocol (reliable)
/// * `TLS` - Transport Layer Security over TCP (reliable, encrypted)
/// * `WS` - WebSocket (reliable)
/// * `WSS` - WebSocket Secure (reliable, encrypted)
///
/// # Examples
///
/// ```rust
/// use rsipstack::transport::SipAddr;
/// use rsip::transport::Transport;
/// use std::net::SocketAddr;
///
/// // Create from socket address
/// let socket_addr: SocketAddr = "192.168.1.100:5060".parse().unwrap();
/// let sip_addr = SipAddr::from(socket_addr);
///
/// // Create with specific transport
/// let sip_addr = SipAddr::new(
///     Transport::Tcp,
///     rsip::HostWithPort::try_from("example.com:5060").unwrap()
/// );
///
/// // Convert to socket address (for IP addresses)
/// if let Ok(socket_addr) = sip_addr.get_socketaddr() {
///     println!("Socket address: {}", socket_addr);
/// }
/// ```
///
/// # Usage in SIP
///
/// SipAddr is used throughout the stack for:
/// * Via header processing
/// * Contact header handling
/// * Route and Record-Route processing
/// * Transport layer addressing
/// * Connection management
///
/// # Conversion
///
/// SipAddr can be converted to/from:
/// * `SocketAddr` (for IP addresses only)
/// * `rsip::Uri` (SIP URI format)
/// * `rsip::HostWithPort` (host/port only)
#[derive(Debug, Eq, PartialEq, Clone, Default)]
pub struct SipAddr {
    pub r#type: Option<rsip::transport::Transport>,
    pub addr: HostWithPort,
}

impl fmt::Display for SipAddr {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            SipAddr {
                r#type: Some(r#type),
                addr,
            } => write!(f, "{} {}", r#type, addr),
            SipAddr { r#type: None, addr } => write!(f, "{}", addr),
        }
    }
}

impl Hash for SipAddr {
    fn hash<H: std::hash::Hasher>(&self, state: &mut H) {
        self.r#type.hash(state);
        match self.addr.host {
            host_with_port::Host::Domain(ref domain) => domain.hash(state),
            host_with_port::Host::IpAddr(ref ip_addr) => ip_addr.hash(state),
        }
        self.addr.port.map(|port| port.value().hash(state));
    }
}

impl SipAddr {
    pub fn new(transport: rsip::transport::Transport, addr: HostWithPort) -> Self {
        SipAddr {
            r#type: Some(transport),
            addr,
        }
    }

    pub fn get_socketaddr(&self) -> Result<SocketAddr> {
        match &self.addr.host {
            host_with_port::Host::Domain(domain) => Err(crate::Error::Error(format!(
                "Cannot convert domain {} to SocketAddr",
                domain
            ))),
            host_with_port::Host::IpAddr(ip_addr) => {
                let port = self.addr.port.map_or(5060, |p| p.value().to_owned());
                Ok(SocketAddr::new(ip_addr.to_owned(), port))
            }
        }
    }

    /// The RFC 3261 default port for this transport.
    ///
    /// Used to normalize SipAddr instances so that `sip:proxy.example`
    /// (no explicit port) and `sip:proxy.example:5060` compare equal for
    /// connection-cache lookups.
    pub fn default_port(transport: Option<rsip::transport::Transport>) -> u16 {
        use rsip::transport::Transport;
        match transport {
            Some(Transport::Tls) | Some(Transport::TlsSctp) => 5061,
            Some(Transport::Ws) => 80,
            Some(Transport::Wss) => 443,
            // UDP/TCP/SCTP and unknown default to 5060.
            _ => 5060,
        }
    }

    /// Return a copy with the port filled in from the transport default
    /// when missing. Leaves the SipAddr unchanged if a port is already set.
    ///
    /// This exists specifically so the transport layer's connection cache
    /// treats `sip:52.9.254.123;transport=tcp` and
    /// `sip:52.9.254.123:5060;transport=tcp` as the same entry — otherwise
    /// a Record-Route URI with no port opens a duplicate TCP connection.
    pub fn with_default_port(&self) -> Self {
        if self.addr.port.is_some() {
            return self.clone();
        }
        let port = Self::default_port(self.r#type);
        SipAddr {
            r#type: self.r#type,
            addr: HostWithPort {
                host: self.addr.host.clone(),
                port: Some(rsip::Port::from(port)),
            },
        }
    }
}

impl From<SipAddr> for rsip::HostWithPort {
    fn from(val: SipAddr) -> Self {
        val.addr
    }
}

impl From<SipAddr> for rsip::Uri {
    fn from(val: SipAddr) -> Self {
        Self::from(&val)
    }
}

impl From<&SipAddr> for rsip::Uri {
    fn from(addr: &SipAddr) -> Self {
        let params = match addr.r#type {
            Some(Transport::Tcp) => vec![rsip::Param::Transport(Transport::Tcp)],
            Some(Transport::Tls) => vec![rsip::Param::Transport(Transport::Tls)],
            Some(Transport::Ws) => vec![rsip::Param::Transport(Transport::Ws)],
            Some(Transport::Wss) => vec![rsip::Param::Transport(Transport::Wss)],
            Some(Transport::TlsSctp) => vec![rsip::Param::Transport(Transport::TlsSctp)],
            Some(Transport::Sctp) => vec![rsip::Param::Transport(Transport::Sctp)],
            _ => vec![],
        };
        let scheme = match addr.r#type {
            Some(rsip::transport::Transport::Wss)
            | Some(rsip::transport::Transport::Tls)
            | Some(rsip::transport::Transport::TlsSctp) => rsip::Scheme::Sips,
            _ => rsip::Scheme::Sip,
        };
        rsip::Uri {
            scheme: Some(scheme),
            host_with_port: addr.addr.clone(),
            params,
            ..Default::default()
        }
    }
}

impl From<SocketAddr> for SipAddr {
    fn from(addr: SocketAddr) -> Self {
        let host_with_port = HostWithPort {
            host: addr.ip().into(),
            port: Some(addr.port().into()),
        };
        SipAddr {
            r#type: None,
            addr: host_with_port,
        }
    }
}

impl From<rsip::host_with_port::HostWithPort> for SipAddr {
    fn from(host_with_port: rsip::host_with_port::HostWithPort) -> Self {
        SipAddr {
            r#type: None,
            addr: host_with_port,
        }
    }
}

impl TryFrom<&rsip::Uri> for SipAddr {
    type Error = crate::Error;

    fn try_from(uri: &rsip::Uri) -> Result<Self> {
        let transport = uri.transport().cloned();
        Ok(SipAddr {
            r#type: transport,
            addr: uri.host_with_port.clone(),
        })
    }
}

impl TryFrom<rsip::Uri> for SipAddr {
    type Error = crate::Error;

    fn try_from(uri: rsip::Uri) -> Result<Self> {
        let transport = uri.transport().cloned();
        Ok(SipAddr {
            r#type: transport,
            addr: uri.host_with_port,
        })
    }
}

impl<'a> TryFrom<std::borrow::Cow<'a, rsip::Uri>> for SipAddr {
    type Error = crate::Error;

    fn try_from(uri: std::borrow::Cow<'a, rsip::Uri>) -> Result<Self> {
        match uri {
            std::borrow::Cow::Owned(uri) => uri.try_into(),
            std::borrow::Cow::Borrowed(uri) => uri.try_into(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use std::net::{IpAddr, Ipv4Addr};

    fn ip_addr(ip: &str, port: Option<u16>, transport: rsip::transport::Transport) -> SipAddr {
        SipAddr {
            r#type: Some(transport),
            addr: HostWithPort {
                host: host_with_port::Host::IpAddr(ip.parse().unwrap()),
                port: port.map(rsip::Port::from),
            },
        }
    }

    #[test]
    fn test_default_port_per_transport() {
        assert_eq!(SipAddr::default_port(Some(rsip::transport::Transport::Tcp)), 5060);
        assert_eq!(SipAddr::default_port(Some(rsip::transport::Transport::Udp)), 5060);
        assert_eq!(SipAddr::default_port(Some(rsip::transport::Transport::Tls)), 5061);
        assert_eq!(SipAddr::default_port(Some(rsip::transport::Transport::TlsSctp)), 5061);
        assert_eq!(SipAddr::default_port(Some(rsip::transport::Transport::Ws)), 80);
        assert_eq!(SipAddr::default_port(Some(rsip::transport::Transport::Wss)), 443);
        assert_eq!(SipAddr::default_port(None), 5060);
    }

    #[test]
    fn test_with_default_port_fills_missing() {
        let a = ip_addr("52.9.254.123", None, rsip::transport::Transport::Tcp);
        let normalized = a.with_default_port();
        assert_eq!(normalized.addr.port.as_ref().map(|p| *p.value()), Some(5060));
    }

    #[test]
    fn test_with_default_port_leaves_existing_port() {
        let a = ip_addr("52.9.254.123", Some(5080), rsip::transport::Transport::Tcp);
        let normalized = a.with_default_port();
        assert_eq!(normalized.addr.port.as_ref().map(|p| *p.value()), Some(5080));
    }

    #[test]
    fn test_with_default_port_tls_uses_5061() {
        let a = ip_addr("52.9.254.123", None, rsip::transport::Transport::Tls);
        assert_eq!(a.with_default_port().addr.port.as_ref().map(|p| *p.value()), Some(5061));
    }

    #[test]
    fn test_normalized_addr_matches_explicit_port_in_hashmap() {
        // Regression: Record-Route URI without port opened a duplicate TCP
        // connection because the cache key `sip:52.9.254.123;transport=tcp`
        // did not match the existing `sip:52.9.254.123:5060;transport=tcp`.
        // After normalization, both should be the same cache key.
        let with_port = ip_addr("52.9.254.123", Some(5060), rsip::transport::Transport::Tcp);
        let without_port = ip_addr("52.9.254.123", None, rsip::transport::Transport::Tcp);

        // Raw equality BEFORE normalization: not equal.
        assert_ne!(with_port, without_port);

        // After normalization: equal (port filled in).
        assert_eq!(with_port, without_port.with_default_port());

        // And a HashMap keyed by the normalized form finds either one.
        let mut map: HashMap<SipAddr, &str> = HashMap::new();
        map.insert(with_port.with_default_port(), "connection-1");
        assert_eq!(map.get(&without_port.with_default_port()), Some(&"connection-1"));
        assert_eq!(map.get(&with_port.with_default_port()), Some(&"connection-1"));
    }

    #[test]
    fn test_normalized_addr_different_ports_not_equal() {
        // Safety: the normalization must not collapse genuinely different
        // ports — only fill in missing ones.
        let a = ip_addr("52.9.254.123", Some(5060), rsip::transport::Transport::Tcp);
        let b = ip_addr("52.9.254.123", Some(5080), rsip::transport::Transport::Tcp);
        assert_ne!(a.with_default_port(), b.with_default_port());
    }

    #[test]
    fn test_get_socketaddr_still_defaults_to_5060() {
        // Sanity: the existing get_socketaddr helper continues to default
        // to 5060 when port is absent (previous behavior, unrelated to the
        // new with_default_port).
        let a = SipAddr {
            r#type: None,
            addr: HostWithPort {
                host: host_with_port::Host::IpAddr(IpAddr::V4(Ipv4Addr::new(10, 0, 0, 1))),
                port: None,
            },
        };
        let socket = a.get_socketaddr().unwrap();
        assert_eq!(socket.port(), 5060);
    }
}
