# Feature Flags

Cargo feature flags for the `agent-transport` Rust core.

| Feature | Description |
|---------|-------------|
| `audio-stream` | WebSocket audio streaming transport |
| `jitter-buffer` | RTP jitter buffer (requires neteq) |
| `plc` | Packet loss concealment |
| `comfort-noise` | Comfort noise generation |
| `audio-processing` | All three above combined |
| `integration` | Live SIP integration tests (requires credentials) |

## Build Examples

```bash
cargo build                                     # Core library (SIP transport)
cargo build --features audio-stream             # + Plivo audio streaming
cargo build --features audio-processing         # + jitter buffer, PLC, comfort noise
cargo test --features integration               # Live SIP tests (needs SIP_USERNAME, SIP_PASSWORD)
```

---

# CLI Phone

Interactive command-line softphone for testing SIP calls with mic/speaker, DTMF, mute, and hold/unhold.

## Python

```bash
pip install sounddevice numpy

# Outbound call
SIP_USERNAME=xxx SIP_PASSWORD=yyy \
    python examples/cli/phone.py sip:+15551234567@phone.plivo.com

# Inbound (wait for a call)
SIP_USERNAME=xxx SIP_PASSWORD=yyy python examples/cli/phone.py
```

| Example | Description |
|---------|-------------|
| [`phone.py`](../examples/cli/phone.py) | Interactive CLI softphone with mic/speaker, DTMF, mute, hold/unhold |
| [`phone_advanced.py`](../examples/cli/phone_advanced.py) | Advanced CLI with recording, beep detection |

## Node.js

```bash
cd crates/agent-transport-node && npm run build

# Outbound call
SIP_USERNAME=xxx SIP_PASSWORD=yyy \
    node examples/cli/phone.js sip:+15551234567@phone.plivo.com
```

| Example | Description |
|---------|-------------|
| [`phone.js`](../examples/cli/phone.js) | SIP CLI demonstrating signaling, DTMF, pause/resume, flush/clear, wait-for-playout |
