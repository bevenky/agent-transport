//! Comfort Noise Generation (RFC 3389)
//!
//! Generates low-level background noise during silence to:
//! - Keep NAT pinholes open (some carriers drop calls on 30s silence)
//! - Provide natural-sounding silence (dead air sounds unnatural)
//! - Signal to the remote that the stream is still active
//!
//! Feature-gated: only compiled with `comfort-noise` feature.

/// Comfort noise payload type (RFC 3389).
pub const CN_PAYLOAD_TYPE: u8 = 13;

/// Default noise level in dBov (0 = max, 127 = silence).
/// 60 dBov ≈ very quiet background noise.
const DEFAULT_NOISE_LEVEL: u8 = 60;

/// Generate a comfort noise RTP payload (RFC 3389).
///
/// The payload is 1 byte: the noise level in dBov.
/// The remote side uses this to generate local comfort noise.
pub fn cn_payload(noise_level: Option<u8>) -> Vec<u8> {
    vec![noise_level.unwrap_or(DEFAULT_NOISE_LEVEL)]
}

/// Generate local comfort noise samples (PCM i16) for playout.
///
/// Produces very low amplitude random noise at the given sample count.
pub fn generate_comfort_noise(num_samples: usize, noise_level: u8) -> Vec<i16> {
    // Map dBov (0-127) to amplitude (0-127 linear scale)
    // 0 dBov = full scale, 127 = silence
    let amplitude = if noise_level >= 127 { 0.0 } else { 128.0 / (1.0 + noise_level as f32) };

    (0..num_samples).map(|_| {
        let r: f32 = (rand::random::<u16>() as f32 / u16::MAX as f32) * 2.0 - 1.0;
        (r * amplitude) as i16
    }).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_cn_payload_default() {
        let p = cn_payload(None);
        assert_eq!(p.len(), 1);
        assert_eq!(p[0], DEFAULT_NOISE_LEVEL);
    }

    #[test]
    fn test_cn_payload_custom() {
        let p = cn_payload(Some(40));
        assert_eq!(p[0], 40);
    }

    #[test]
    fn test_generate_comfort_noise_length() {
        let samples = generate_comfort_noise(160, 60);
        assert_eq!(samples.len(), 160);
    }

    #[test]
    fn test_generate_comfort_noise_low_amplitude() {
        let samples = generate_comfort_noise(1000, 60);
        let max_amp = samples.iter().map(|s| s.abs()).max().unwrap_or(0);
        assert!(max_amp < 200, "Comfort noise should be very quiet, max={}", max_amp);
    }

    #[test]
    fn test_generate_comfort_noise_silence() {
        let samples = generate_comfort_noise(160, 127);
        assert!(samples.iter().all(|&s| s == 0), "Level 127 should be silence");
    }
}
