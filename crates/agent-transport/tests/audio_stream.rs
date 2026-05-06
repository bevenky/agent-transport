//! Unit tests for audio streaming message format and codec logic.
//!
//! Tests the JSON message construction and parsing used by
//! the Plivo WebSocket audio streaming transport.
#![cfg(feature = "audio-stream")]

use agent_transport::AudioFrame;
use agent_transport::audio_stream::config::AudioStreamConfig;
use agent_transport::audio_stream::endpoint::AudioStreamEndpoint;
use agent_transport::audio_stream::plivo::PlivoProtocol;
use agent_transport::EndpointError;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};

#[test]
fn test_audio_frame_to_bytes_roundtrip() {
    let frame = AudioFrame::new(vec![100, -200, 300, -400], 16000, 1);
    let bytes = frame.as_bytes();
    assert_eq!(bytes.len(), 8); // 4 samples × 2 bytes
    let restored = AudioFrame::from_bytes(&bytes, 16000, 1);
    assert_eq!(restored.data, vec![100, -200, 300, -400]);
}

#[test]
fn test_audio_frame_silence() {
    let frame = AudioFrame::silence(16000, 1, 20); // 20ms at 16kHz
    assert_eq!(frame.samples_per_channel, 320);
    assert_eq!(frame.data.len(), 320);
    assert!(frame.data.iter().all(|&s| s == 0));
}

#[test]
fn test_audio_frame_duration() {
    let frame = AudioFrame::new(vec![0; 320], 16000, 1);
    assert_eq!(frame.duration_ms(), 20);

    let frame = AudioFrame::new(vec![0; 160], 8000, 1);
    assert_eq!(frame.duration_ms(), 20);
}

#[test]
fn test_g711_ulaw_encode_decode_via_codec() {
    use agent_transport::Codec;
    let samples = vec![0i16, 1000, -1000, 8000, -8000, 16000, -16000];
    let encoded = Codec::PCMU.encode(&samples);
    assert_eq!(encoded.len(), 7);
    let decoded = Codec::PCMU.decode(&encoded);
    assert_eq!(decoded.len(), 7);
    // Lossy codec — verify roundtrip is within tolerance
    for (orig, dec) in samples.iter().zip(decoded.iter()) {
        let diff = (*orig as i32 - *dec as i32).unsigned_abs();
        assert!(diff < (orig.unsigned_abs() as u32 / 5).max(200),
            "PCMU roundtrip: {} -> {} (diff {})", orig, dec, diff);
    }
}

#[test]
fn test_g711_alaw_encode_decode_via_codec() {
    use agent_transport::Codec;
    let samples = vec![0i16, 1000, -1000, 8000, -8000];
    let encoded = Codec::PCMA.encode(&samples);
    let decoded = Codec::PCMA.decode(&encoded);
    for (orig, dec) in samples.iter().zip(decoded.iter()) {
        let diff = (*orig as i32 - *dec as i32).unsigned_abs();
        assert!(diff < (orig.unsigned_abs() as u32 / 5).max(200),
            "PCMA roundtrip: {} -> {} (diff {})", orig, dec, diff);
    }
}

#[test]
fn test_base64_mulaw_roundtrip() {
    // Simulate the Plivo audio streaming encode/decode path:
    // PCM int16 → mu-law → base64 → decode → mu-law → PCM int16
    use audio_codec_algorithms::{encode_ulaw, decode_ulaw};
    use base64::Engine;

    let pcm_samples = vec![0i16, 500, -500, 2000, -2000, 8000, -8000];

    // Encode: PCM → mu-law bytes
    let ulaw_bytes: Vec<u8> = pcm_samples.iter().map(|&s| encode_ulaw(s)).collect();

    // Transport: mu-law → base64
    let b64 = base64::engine::general_purpose::STANDARD.encode(&ulaw_bytes);

    // Decode: base64 → mu-law → PCM
    let decoded_ulaw = base64::engine::general_purpose::STANDARD.decode(&b64).unwrap();
    let decoded_pcm: Vec<i16> = decoded_ulaw.iter().map(|&b| decode_ulaw(b)).collect();

    // Verify roundtrip
    assert_eq!(ulaw_bytes.len(), pcm_samples.len());
    assert_eq!(decoded_pcm.len(), pcm_samples.len());

    for (orig, dec) in pcm_samples.iter().zip(decoded_pcm.iter()) {
        let diff = (*orig as i32 - *dec as i32).unsigned_abs();
        assert!(diff < (orig.unsigned_abs() as u32 / 5).max(200),
            "base64 mu-law roundtrip: {} -> {} (diff {})", orig, dec, diff);
    }
}

#[test]
fn test_plivo_play_audio_json_format() {
    use base64::Engine;
    let payload = base64::engine::general_purpose::STANDARD.encode(&[0xFF; 160]);
    let msg = serde_json::json!({
        "event": "playAudio",
        "media": {
            "contentType": "audio/x-mulaw",
            "sampleRate": 8000,
            "payload": payload,
        }
    });
    let json_str = serde_json::to_string(&msg).unwrap();
    assert!(json_str.contains("\"event\":\"playAudio\""));
    assert!(json_str.contains("\"contentType\":\"audio/x-mulaw\""));
    assert!(json_str.contains("\"sampleRate\":8000"));
}

#[test]
fn test_plivo_clear_audio_json_format() {
    let msg = serde_json::json!({
        "event": "clearAudio",
        "streamId": "test-stream-123",
    });
    let json_str = serde_json::to_string(&msg).unwrap();
    assert!(json_str.contains("\"event\":\"clearAudio\""));
    assert!(json_str.contains("\"streamId\":\"test-stream-123\""));
}

#[test]
fn test_missing_session_callback_send_returns_call_not_active() {
    let ep = AudioStreamEndpoint::new(
        AudioStreamConfig {
            listen_addr: "127.0.0.1:0".into(),
            auto_hangup: false,
            ..Default::default()
        },
        Arc::new(PlivoProtocol::new("auth-id".into(), "auth-token".into())),
    )
    .unwrap();

    let callback_called = Arc::new(AtomicBool::new(false));
    let callback_called_inner = callback_called.clone();
    let frame = AudioFrame::silence(8000, 1, 20);

    let err = ep
        .send_audio_with_callback(
            "missing-session",
            &frame,
            Box::new(move || {
                callback_called_inner.store(true, Ordering::SeqCst);
            }),
        )
        .unwrap_err();

    assert!(matches!(err, EndpointError::CallNotActive(id) if id == "missing-session"));
    assert!(!callback_called.load(Ordering::SeqCst));
    assert!(ep.clear_buffer("missing-session").is_ok());
}
