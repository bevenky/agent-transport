//! Configuration for WebSocket audio streaming transport.

/// Configuration for the audio streaming endpoint.
///
/// Provider-specific settings (auth credentials, etc.) are passed via the
/// `StreamProtocol` implementation, not this config.
#[derive(Debug, Clone)]
pub struct AudioStreamConfig {
    /// Address to listen on for WebSocket connections (e.g., "0.0.0.0:8765").
    pub listen_addr: String,
    /// Input sample rate in Hz (default: 8000).
    /// Audio from the provider is resampled to this rate before delivery.
    pub input_sample_rate: u32,
    /// Output sample rate in Hz (default: 8000).
    /// Audio from TTS is expected at this rate and resampled to wire rate.
    pub output_sample_rate: u32,
    /// Automatically hang up the call on shutdown (default: true).
    pub auto_hangup: bool,
    /// Path to TLS certificate file (PEM format, fullchain).
    /// If set with tls_key_path, enables wss:// (TLS WebSocket).
    pub tls_cert_path: Option<String>,
    /// Path to TLS private key file (PEM format).
    /// Required when tls_cert_path is set.
    pub tls_key_path: Option<String>,
}

impl Default for AudioStreamConfig {
    fn default() -> Self {
        Self {
            listen_addr: "0.0.0.0:8080".into(),
            input_sample_rate: 8000,
            output_sample_rate: 8000,
            auto_hangup: true,
            tls_cert_path: None,
            tls_key_path: None,
        }
    }
}
