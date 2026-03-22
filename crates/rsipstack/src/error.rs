use crate::{dialog::DialogId, transaction::key::TransactionKey, transport::SipAddr};
use thiserror::Error as ThisError;

#[derive(Debug, ThisError)]
pub enum Error {
    #[error("SIP message error: {0}")]
    SipMessageError(#[from] rsip::Error),

    #[error("DNS resolution error: {0}")]
    DnsResolutionError(String),

    #[error("Transport layer error: {0}: {1}")]
    TransportLayerError(String, SipAddr),

    #[error("Transaction error: {0}: {1}")]
    TransactionError(String, TransactionKey),

    #[error("Endpoint error: {0}")]
    EndpointError(String),

    #[error("Dialog error:{2}({0})")]
    DialogError(String, DialogId, rsip::StatusCode),

    #[error("I/O error: {0}")]
    IoError(#[from] std::io::Error),

    #[error("Address parse error: {0}")]
    AddrParseError(#[from] std::net::AddrParseError),

    #[error("WebSocket error: {0}")]
    WebSocketError(#[from] tokio_tungstenite::tungstenite::Error),

    #[error("Error: {0}")]
    Error(String),
}

impl<T> From<tokio::sync::mpsc::error::SendError<T>> for Error {
    fn from(e: tokio::sync::mpsc::error::SendError<T>) -> Self {
        Error::Error(e.to_string())
    }
}
