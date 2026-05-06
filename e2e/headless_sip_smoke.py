#!/usr/bin/env python3
"""
Headless SIP e2e smoke for CI.

This script follows the same SIP/audio flow as the CLI phone examples, but keeps
CI policy here: E2E_* environment names, bounded waits, strict exit codes, and a
received WAV artifact.
"""

import argparse
import importlib
import math
import os
import struct
import sys
import threading
import time
import wave
from pathlib import Path

SAMPLE_RATE = 8000
CHANNELS = 1
FRAME_SAMPLES = SAMPLE_RATE * 20 // 1000
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLIP = REPO_ROOT / "examples" / "audio" / "caller_greeting_8k.wav"
DEFAULT_OUTPUT = Path("/tmp/received_audio.wav")


class E2EFailure(RuntimeError):
    pass


def load_agent_transport():
    module = importlib.import_module("agent_transport")
    return module.SipEndpoint, module.AudioFrame, module.init_logging


def load_wav(path):
    with wave.open(str(path)) as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        raw = w.readframes(w.getnframes())

    n_samples = len(raw) // 2
    samples = list(struct.unpack(f"<{n_samples}h", raw))
    if channels > 1:
        samples = samples[::channels]

    if rate != SAMPLE_RATE:
        n_out = int(len(samples) / rate * SAMPLE_RATE)
        samples = [
            int(
                samples[int(i * (len(samples) - 1) / max(1, n_out - 1))]
            )
            for i in range(n_out)
        ]

    return samples


def save_wav(path, samples):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def audio_stats(samples):
    if not samples:
        return 0, 0
    peak = max(abs(s) for s in samples)
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    return peak, rms


def wait_for_event_type(ep, event_type, timeout_s):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        event = ep.wait_for_event(timeout_ms=500)
        if event is None:
            continue
        current_type = event.get("type")
        if current_type == event_type:
            return event
        if current_type == "registration_failed":
            raise E2EFailure(f"Registration failed: {event.get('error') or event}")
        if current_type == "call_terminated":
            raise E2EFailure(f"Call terminated before {event_type}: {event.get('reason', '')}")
    raise E2EFailure(f"Timed out after {timeout_s:.0f}s waiting for {event_type}")


def send_frames_realtime(ep, AudioFrame, session_id, samples, label, call_ended):
    peak, rms = audio_stats(samples)
    duration = len(samples) / SAMPLE_RATE
    print(f"[{label}] sending {duration:.1f}s audio (peak={peak}, rms={rms:.0f})")

    frames_sent = 0
    for i in range(0, len(samples), FRAME_SAMPLES):
        if call_ended.is_set():
            raise E2EFailure(f"Call ended while sending {label}")
        chunk = samples[i:i + FRAME_SAMPLES]
        if len(chunk) < FRAME_SAMPLES:
            chunk = chunk + [0] * (FRAME_SAMPLES - len(chunk))
        try:
            ep.send_audio(session_id, AudioFrame(chunk, SAMPLE_RATE, CHANNELS))
            frames_sent += 1
        except Exception as exc:
            raise E2EFailure(f"{label} send failed at frame {frames_sent}: {exc}") from exc
        time.sleep(0.02)

    if frames_sent == 0:
        raise E2EFailure(f"{label} sent no frames")
    print(f"[{label}] sent {frames_sent} frames")


def send_silence(ep, AudioFrame, session_id, seconds, call_ended):
    silence = [0] * FRAME_SAMPLES
    for frame_idx in range(int(seconds / 0.02)):
        if call_ended.is_set():
            break
        try:
            ep.send_audio(session_id, AudioFrame(silence, SAMPLE_RATE, CHANNELS))
        except Exception as exc:
            raise E2EFailure(f"silence send failed at frame {frame_idx}: {exc}") from exc
        time.sleep(0.02)


def start_receivers(ep, session_id):
    running = threading.Event()
    running.set()
    call_ended = threading.Event()
    recv_lock = threading.Lock()
    received_samples = []
    errors = []

    def recv_loop():
        frame_count = 0
        while running.is_set() and not call_ended.is_set():
            try:
                result = ep.recv_audio_bytes_blocking(session_id, 20)
            except Exception as exc:
                errors.append(f"recv_audio_bytes_blocking failed: {exc}")
                break
            if result is None:
                continue
            audio_bytes, sample_rate, _ = result
            n_samples = len(audio_bytes) // 2
            if n_samples == 0:
                continue
            samples = list(struct.unpack(f"<{n_samples}h", bytes(audio_bytes)))
            with recv_lock:
                received_samples.extend(samples)
            frame_count += 1
            if frame_count == 1:
                print(f"[recv] first frame: {n_samples} samples at {sample_rate}Hz")
            elif frame_count % 500 == 0:
                with recv_lock:
                    duration = len(received_samples) / SAMPLE_RATE
                _, rms = audio_stats(samples)
                print(f"[recv] {frame_count} frames ({duration:.1f}s total, rms={rms:.0f})")

    def event_loop():
        while running.is_set() and not call_ended.is_set():
            try:
                event = ep.wait_for_event(timeout_ms=200)
            except Exception as exc:
                errors.append(f"wait_for_event failed: {exc}")
                break
            if event is None:
                continue
            if event.get("type") == "call_terminated":
                print(f"[event] call ended: {event.get('reason', '')}")
                call_ended.set()
                break
            if event.get("type") == "dtmf_received":
                print(f"[event] DTMF received: {event.get('digit')}")

    threading.Thread(target=recv_loop, daemon=True).start()
    threading.Thread(target=event_loop, daemon=True).start()
    return running, call_ended, recv_lock, received_samples, errors


def wait_for_received_audio(samples, recv_lock, min_seconds, timeout_s, call_ended):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and not call_ended.is_set():
        with recv_lock:
            duration = len(samples) / SAMPLE_RATE
        if duration >= min_seconds:
            return
        time.sleep(0.1)
    raise E2EFailure(f"Timed out waiting for {min_seconds:.1f}s of received audio")


def analyze(samples, output_path, speech_threshold):
    save_wav(output_path, samples)
    peak, rms = audio_stats(samples)
    duration = len(samples) / SAMPLE_RATE
    speech_samples = sum(1 for sample in samples if abs(sample) > speech_threshold)
    speech_percent = 100 * speech_samples / len(samples) if samples else 0.0
    print(f"received={duration:.2f}s peak={peak} rms={rms:.0f} speech={speech_percent:.2f}%")
    print(f"artifact={output_path}")
    return duration, speech_percent


def e2e_env(args):
    username = os.environ.get("E2E_SIP_USERNAME")
    password = os.environ.get("E2E_SIP_PASSWORD")
    dest_uri = os.environ.get("E2E_SIP_DEST_URI")
    sip_domain = os.environ.get("E2E_SIP_DOMAIN") or "phone.plivo.com"
    rust_log = os.environ.get("E2E_RUST_LOG", "info")
    missing = [
        name
        for name, value in [
            ("E2E_SIP_USERNAME", username),
            ("E2E_SIP_PASSWORD", password),
            ("E2E_SIP_DEST_URI", dest_uri),
        ]
        if not value
    ]
    if missing and not args.dry_run:
        raise E2EFailure(f"Missing required env vars: {', '.join(missing)}")
    return username, password, dest_uri, sip_domain, rust_log


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Headless SIP e2e smoke")
    parser.add_argument("--ci", action="store_true", help="Accepted for workflow readability")
    parser.add_argument("--dry-run", action="store_true", help="Validate CLI/env shape without dialing")
    parser.add_argument("--clip", type=Path, default=DEFAULT_CLIP)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-register-seconds", type=float, default=10.0)
    parser.add_argument("--max-answer-seconds", type=float, default=30.0)
    parser.add_argument("--max-call-seconds", type=float, default=90.0)
    parser.add_argument("--max-hangup-seconds", type=float, default=10.0)
    parser.add_argument("--min-received-seconds", type=float, default=1.0)
    parser.add_argument("--min-speech-percent", type=float, default=1.0)
    parser.add_argument("--speech-threshold", type=int, default=500)
    parser.add_argument("--dtmf-digit", default="1")
    return parser.parse_args(argv)


def run(args):
    username, password, dest_uri, sip_domain, rust_log = e2e_env(args)
    print(f"dry_run={args.dry_run}")
    print("sip env loaded")
    print(f"clip={args.clip}")
    print(f"output={args.output}")

    if args.dry_run:
        print("dry run passed")
        return 0

    SipEndpoint, AudioFrame, init_logging = load_agent_transport()
    init_logging(rust_log)

    if not args.clip.exists():
        raise E2EFailure(f"Audio clip not found: {args.clip}")
    clip = load_wav(args.clip)

    ep = None
    running = None
    call_ended = None
    recv_lock = None
    received_samples = []
    errors = []

    try:
        ep = SipEndpoint(sip_server=sip_domain, log_level=3)
        print("registering SIP account")
        ep.register(username, password)
        wait_for_event_type(ep, "registered", args.max_register_seconds)
        print("registered")

        print("calling E2E_SIP_DEST_URI")
        try:
            session_id = ep.call(dest_uri)
        except Exception as exc:
            raise E2EFailure(f"Call initiation failed: {exc}") from exc
        wait_for_event_type(ep, "call_answered", args.max_answer_seconds)
        print(f"connected session_id={session_id}")

        running, call_ended, recv_lock, received_samples, errors = start_receivers(ep, session_id)
        deadline = time.monotonic() + args.max_call_seconds

        send_silence(ep, AudioFrame, session_id, 0.5, call_ended)
        send_frames_realtime(ep, AudioFrame, session_id, clip, "caller-clip", call_ended)

        print(f"sending DTMF {args.dtmf_digit}")
        try:
            ep.send_dtmf(session_id, args.dtmf_digit)
        except Exception as exc:
            raise E2EFailure(f"DTMF send failed: {exc}") from exc

        wait_for_received_audio(
            received_samples,
            recv_lock,
            args.min_received_seconds,
            min(15.0, max(0.0, deadline - time.monotonic())),
            call_ended,
        )
        send_silence(ep, AudioFrame, session_id, 1.0, call_ended)

        if call_ended.is_set():
            raise E2EFailure("Call ended before scripted hangup")
        print("hanging up")
        try:
            ep.hangup(session_id)
        except Exception as exc:
            raise E2EFailure(f"Hangup failed: {exc}") from exc
        if not call_ended.wait(timeout=args.max_hangup_seconds):
            raise E2EFailure("Timed out waiting for call termination after hangup")
    finally:
        if running is not None:
            running.clear()
            time.sleep(0.5)
        if ep is not None:
            try:
                ep.shutdown()
            except Exception as exc:
                print(f"warning: shutdown failed: {exc}")

    if errors:
        raise E2EFailure("; ".join(errors))
    with recv_lock:
        samples = list(received_samples)
    duration, speech_percent = analyze(samples, args.output, args.speech_threshold)

    if duration < args.min_received_seconds:
        raise E2EFailure(
            f"Received audio duration {duration:.2f}s is below {args.min_received_seconds:.2f}s"
        )
    if speech_percent < args.min_speech_percent:
        raise E2EFailure(
            f"Speech percent {speech_percent:.2f}% is below {args.min_speech_percent:.2f}%"
        )

    print("e2e smoke passed")
    return 0


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        return run(args)
    except E2EFailure as exc:
        print(f"E2E FAILED: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
