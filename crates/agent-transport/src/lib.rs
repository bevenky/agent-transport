//! Agent Transport — SIP transport for AI agents.
//!
//! Pure Rust implementation using rsipstack for SIP signaling
//! and rtc-rtp for RTP media transport.
//!
//! # Example
//! ```no_run
//! use agent_transport::{SipEndpoint, EndpointConfig, Codec, AudioFrame};
//!
//! let config = EndpointConfig {
//!     sip_server: "phone.plivo.com".into(),
//!     codecs: vec![Codec::PCMU, Codec::PCMA],
//!     ..Default::default()
//! };
//!
//! let ep = SipEndpoint::new(config).unwrap();
//! ep.register("user", "pass").unwrap();
//!
//! let call_id = ep.call("sip:+15551234567@phone.plivo.com", None).unwrap();
//! // ... send/receive audio frames ...
//! ep.hangup(call_id).unwrap();
//! ep.shutdown().unwrap();
//! ```

mod audio;
mod call;
mod config;
mod dtmf;
mod endpoint;
mod error;
mod events;
mod recorder;
mod rtp_transport;
mod sdp;

pub use audio::AudioFrame;
pub use beep_detector::BeepDetectorConfig;
pub use call::{CallDirection, CallSession, CallState};
pub use config::{Codec, EndpointConfig, TurnConfig};
pub use endpoint::SipEndpoint;
pub use error::EndpointError;
pub use events::EndpointEvent;
