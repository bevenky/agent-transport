# E2E Smoke Tests

This folder contains CI-oriented e2e smoke tests that exercise the Python
bindings against real transport surfaces. They are intentionally separate from
`examples/cli`: examples stay user-facing, while these scripts own CI policy,
timeouts, artifact paths, strict exit codes, and `E2E_` environment names.

## GitHub Actions Setup

The headless workflow is `.github/workflows/e2e-headless.yml`.

Required repository secrets:

- `E2E_SIP_USERNAME`
- `E2E_SIP_PASSWORD`
- `E2E_SIP_DEST_URI`

Optional repository secrets or variables:

- `E2E_SIP_DOMAIN`, default `phone.plivo.com`
- `E2E_RUST_LOG`, default `info`

The workflow runs on:

- `workflow_dispatch`
- pull requests labeled `e2e-headless`

The workflow uses a global concurrency group because the SIP smoke is expected
to use a dedicated SIP account. Audio-stream e2e is local and runs as the hard
PR signal. Live SIP is quarantined on pull requests because carrier/provider
state can return valid failures such as busy destinations; the same SIP smoke
is a hard failure on manual runs.

## Local Commands

Dry-run both scripts without network or package imports:

```bash
python e2e/headless_sip_smoke.py --dry-run --ci
python e2e/audio_stream_smoke.py --dry-run --ci
```

Run the local audio-stream matrix after installing the Python package:

```bash
python -m pip install websockets
python -m pip install ./crates/agent-transport-python
python e2e/audio_stream_smoke.py --ci --timeout-seconds 10
```

Run the live SIP smoke with credentials:

```bash
E2E_SIP_USERNAME=... \
E2E_SIP_PASSWORD=... \
E2E_SIP_DEST_URI=... \
python e2e/headless_sip_smoke.py --ci --output /tmp/received_audio.wav
```

## Coverage Checklist

### SIP

Covered by `headless_sip_smoke.py`:

- [x] Loads only `E2E_` SIP configuration in CI mode.
- [x] Registers the SIP account through `SipEndpoint`.
- [x] Places an outbound call to `E2E_SIP_DEST_URI`.
- [x] Waits for call answer with a bounded timeout.
- [x] Sends real 8 kHz mono PCM audio from a WAV fixture.
- [x] Sends DTMF on the active call.
- [x] Receives media from the remote endpoint and writes a WAV artifact.
- [x] Fails on insufficient received duration or speech.
- [x] Hangs up and fails on unclean shutdown/termination paths.

Not covered yet:

- [ ] Deterministic local SIP registrar/dialog/RTP peer in CI.
- [ ] Inbound SIP calls.
- [ ] Multiple simultaneous SIP calls on the same endpoint/account.
- [ ] SIP redirects, re-INVITE, hold/resume, transfer, or REFER flows.
- [ ] Codec negotiation beyond the configured 8 kHz smoke path.
- [ ] NAT, packet loss, jitter, and provider failover behavior.
- [ ] Carrier-specific busy/no-answer cases as a hard PR gate.

### Audio Stream

Covered by `audio_stream_smoke.py`:

- [x] Local Plivo-compatible WebSocket `start` and `stop` lifecycle.
- [x] Inbound L16 8 kHz media into `AudioStreamEndpoint`.
- [x] Inbound L16 16 kHz media with endpoint-side resampling to 8 kHz.
- [x] Inbound mu-law 8 kHz media decode path.
- [x] Audio duration and speech-threshold assertions.
- [x] Inbound DTMF event handling.
- [x] Outbound `playAudio` payloads, content type, and sample rate.
- [x] Outbound flush checkpoint and `playedStream` playout acknowledgement.
- [x] `pause`, `resume`, and `clear_buffer` control messages.
- [x] Outbound `sendDTMF`.
- [x] Manual checkpoint message emission.
- [x] WebSocket disconnect cleanup and post-disconnect send failure.

Not covered yet:

- [ ] Real Plivo WebSocket infrastructure or webhook/auth integration.
- [ ] Multiple simultaneous audio-stream sessions.
- [ ] Long-duration calls and sustained backpressure.
- [ ] Packet loss, jitter, out-of-order media, malformed JSON, or malformed base64.
- [ ] Mute/unmute stream events.
- [ ] Background audio mixing, recording, and beep detection paths.
- [ ] Pipecat/LiveKit adapter-level behavior on top of the raw endpoint.
- [ ] Non-8 kHz pipeline sample rates and multi-channel audio.
