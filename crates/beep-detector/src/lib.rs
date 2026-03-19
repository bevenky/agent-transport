//! Voicemail beep detector using Goertzel filters.
//!
//! Detects pure tones (beeps) in audio streams by analyzing specific frequencies
//! with harmonic rejection to distinguish beeps from speech.
//!
//! # Example
//! ```
//! use beep_detector::{BeepDetector, BeepDetectorConfig, BeepDetectorResult};
//!
//! let config = BeepDetectorConfig::for_telephony();
//! let mut detector = BeepDetector::new(config);
//!
//! // Feed 20ms audio frames
//! let samples = vec![0i16; 320]; // 20ms at 16kHz
//! match detector.process_frame(&samples) {
//!     BeepDetectorResult::Detected(event) => println!("Beep at {}Hz", event.frequency_hz),
//!     BeepDetectorResult::Timeout => println!("No beep found"),
//!     BeepDetectorResult::Listening => {} // keep feeding frames
//! }
//! ```

pub mod config;
pub mod detector;
pub mod goertzel;

pub use config::BeepDetectorConfig;
pub use detector::{BeepDetector, BeepDetectorResult, BeepEvent};
pub use goertzel::{GoertzelFilter, TARGET_FREQUENCIES};
