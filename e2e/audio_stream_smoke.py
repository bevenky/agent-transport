#!/usr/bin/env python3
"""Local audio-stream e2e smoke for CI.

This exercises the Rust-backed AudioStreamEndpoint through the Python binding
using a local Plivo-protocol WebSocket client. It needs no SIP credentials and
does not dial an external provider.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import socket
import sys
import time
from dataclasses import dataclass


class E2EFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class AudioStats:
    samples: int
    speech_samples: int

    @property
    def speech_percent(self) -> float:
        if self.samples == 0:
            return 0.0
        return 100.0 * self.speech_samples / self.samples


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Audio-stream WebSocket e2e smoke")
    parser.add_argument("--ci", action="store_true", help="Accepted for workflow readability")
    parser.add_argument("--dry-run", action="store_true", help="Validate CLI/import shape without opening a socket")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=0)
    parser.add_argument("--input-sample-rate", type=int, default=8000)
    parser.add_argument("--output-sample-rate", type=int, default=8000)
    parser.add_argument("--min-inbound-seconds", type=float, default=0.25)
    parser.add_argument("--min-outbound-seconds", type=float, default=0.25)
    parser.add_argument("--speech-threshold", type=int, default=500)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    return parser.parse_args(argv)


def load_deps():
    try:
        import websockets
    except ImportError as exc:
        raise E2EFailure("Missing dependency: websockets") from exc

    try:
        from agent_transport import AudioFrame, AudioStreamEndpoint, init_logging
    except ImportError as exc:
        raise E2EFailure("Missing dependency: agent_transport") from exc

    return websockets, AudioFrame, AudioStreamEndpoint, init_logging


def choose_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def sine_samples(sample_rate: int, seconds: float, freq: float = 440.0, amplitude: int = 8000) -> list[int]:
    count = int(sample_rate * seconds)
    return [
        int(amplitude * math.sin(2.0 * math.pi * freq * idx / sample_rate))
        for idx in range(count)
    ]


def pcm16le(samples: list[int]) -> bytes:
    return b"".join(int(sample).to_bytes(2, "little", signed=True) for sample in samples)


def b64_pcm16le(samples: list[int]) -> str:
    return base64.b64encode(pcm16le(samples)).decode("ascii")


def audio_stats(samples: list[int], threshold: int) -> AudioStats:
    speech = sum(1 for sample in samples if abs(sample) >= threshold)
    return AudioStats(samples=len(samples), speech_samples=speech)


async def wait_event(ep, event_type: str, timeout_seconds: float):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
        event = await asyncio.to_thread(ep.wait_for_event, min(remaining_ms, 250))
        if event is None:
            continue
        print(f"event={event.get('type')}")
        if event.get("type") == event_type:
            return event
        if event.get("type") in {"call_terminated", "shutdown"} and event_type != event.get("type"):
            raise E2EFailure(f"Unexpected terminal event while waiting for {event_type}: {event}")
    raise E2EFailure(f"Timed out waiting for event: {event_type}")


async def recv_endpoint_audio(ep, session_id: str, sample_rate: int, min_seconds: float, threshold: int, timeout_seconds: float):
    deadline = time.monotonic() + timeout_seconds
    samples: list[int] = []
    required = int(sample_rate * min_seconds)
    while len(samples) < required and time.monotonic() < deadline:
        frame = await asyncio.to_thread(ep.recv_audio_blocking, session_id, 250)
        if frame is None:
            continue
        samples.extend(int(sample) for sample in frame.data)
    stats = audio_stats(samples, threshold)
    if len(samples) < required:
        raise E2EFailure(f"Insufficient inbound audio: got {len(samples)} samples, need {required}")
    if stats.speech_percent < 1.0:
        raise E2EFailure(f"Insufficient inbound speech: {stats.speech_percent:.2f}%")
    return stats


def start_message(call_id: str, stream_id: str, sample_rate: int) -> str:
    return json.dumps(
        {
            "event": "start",
            "start": {
                "callId": call_id,
                "streamId": stream_id,
                "mediaFormat": {
                    "encoding": "audio/x-l16",
                    "sampleRate": sample_rate,
                },
            },
            "extra_headers": '{"X-PH-e2e": "audio-stream"}',
        }
    )


def media_message(samples: list[int]) -> str:
    return json.dumps({"event": "media", "media": {"payload": b64_pcm16le(samples)}})


async def read_json(ws, timeout_seconds: float):
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout_seconds)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


async def read_until(ws, predicate, timeout_seconds: float):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        msg = await read_json(ws, max(0.1, deadline - time.monotonic()))
        print(f"ws_event={msg.get('event')}")
        if predicate(msg):
            return msg
    raise E2EFailure("Timed out waiting for expected websocket message")


async def connect_with_retry(websockets, uri: str, timeout_seconds: float):
    deadline = time.monotonic() + timeout_seconds
    last_error = None
    while time.monotonic() < deadline:
        try:
            return await websockets.connect(uri, open_timeout=min(2.0, timeout_seconds))
        except OSError as exc:
            last_error = exc
            await asyncio.sleep(0.05)
    raise E2EFailure(f"Timed out connecting to {uri}: {last_error}")


async def run_smoke(args):
    websockets, AudioFrame, AudioStreamEndpoint, init_logging = load_deps()
    rust_log = os.environ.get("E2E_RUST_LOG", "info")
    init_logging(rust_log)

    port = args.listen_port or choose_port(args.listen_host)
    listen_addr = f"{args.listen_host}:{port}"
    uri = f"ws://{listen_addr}"
    print(f"listen_addr={listen_addr}")

    ep = AudioStreamEndpoint(
        listen_addr,
        "",
        "",
        args.input_sample_rate,
        args.output_sample_rate,
        False,
    )

    try:
        async with await connect_with_retry(websockets, uri, args.timeout_seconds) as ws:
            call_id = "audio-stream-e2e-call"
            stream_id = "audio-stream-e2e-stream"
            await ws.send(start_message(call_id, stream_id, args.input_sample_rate))

            answered = await wait_event(ep, "call_answered", args.timeout_seconds)
            session = answered["session"]
            session_id = session.session_id
            print(f"session_id={session_id}")

            inbound = sine_samples(args.input_sample_rate, args.min_inbound_seconds + 0.1)
            frame_size = int(args.input_sample_rate * 0.02)
            for idx in range(0, len(inbound), frame_size):
                await ws.send(media_message(inbound[idx : idx + frame_size]))
                await asyncio.sleep(0.005)

            inbound_stats = await recv_endpoint_audio(
                ep,
                session_id,
                args.input_sample_rate,
                args.min_inbound_seconds,
                args.speech_threshold,
                args.timeout_seconds,
            )
            print(
                "inbound_audio="
                f"{inbound_stats.samples / args.input_sample_rate:.3f}s "
                f"speech={inbound_stats.speech_percent:.2f}%"
            )

            await ws.send(json.dumps({"event": "dtmf", "dtmf": {"digit": "7"}}))
            dtmf = await wait_event(ep, "dtmf_received", args.timeout_seconds)
            if dtmf.get("digit") != "7" or dtmf.get("method") != "audio_stream":
                raise E2EFailure(f"Unexpected DTMF event: {dtmf}")
            print("dtmf_received=7")

            outbound = sine_samples(args.output_sample_rate, args.min_outbound_seconds + 0.25, freq=660.0)
            outbound_frame_size = int(args.output_sample_rate * 0.02)
            for idx in range(0, len(outbound), outbound_frame_size):
                ep.send_audio(
                    session_id,
                    AudioFrame(outbound[idx : idx + outbound_frame_size], args.output_sample_rate, 1),
                )
                await asyncio.sleep(0.02)
            ep.flush(session_id)

            required_outbound_bytes = int(args.output_sample_rate * args.min_outbound_seconds * 2)
            outbound_bytes = bytearray()
            checkpoint_name = None
            deadline = time.monotonic() + args.timeout_seconds
            while (len(outbound_bytes) < required_outbound_bytes or checkpoint_name is None) and time.monotonic() < deadline:
                msg = await read_json(ws, max(0.1, deadline - time.monotonic()))
                event = msg.get("event")
                print(f"ws_event={event}")
                if event == "playAudio":
                    media = msg.get("media") or {}
                    if media.get("contentType") != "audio/x-l16":
                        raise E2EFailure(f"Unexpected outbound contentType: {media.get('contentType')}")
                    outbound_bytes.extend(base64.b64decode(media.get("payload", "")))
                elif event == "checkpoint":
                    checkpoint_name = msg.get("name")
                    await ws.send(json.dumps({"event": "playedStream", "name": checkpoint_name}))

            if len(outbound_bytes) < required_outbound_bytes:
                raise E2EFailure(
                    f"Insufficient outbound audio: got {len(outbound_bytes)} bytes, need {required_outbound_bytes}"
                )
            if checkpoint_name is None:
                raise E2EFailure("Did not receive outbound checkpoint")
            if not await asyncio.to_thread(ep.wait_for_playout, session_id, int(args.timeout_seconds * 1000)):
                raise E2EFailure("Timed out waiting for playout acknowledgement")

            outbound_samples = [
                int.from_bytes(outbound_bytes[idx : idx + 2], "little", signed=True)
                for idx in range(0, len(outbound_bytes) - 1, 2)
            ]
            outbound_stats = audio_stats(outbound_samples, args.speech_threshold)
            if outbound_stats.speech_percent < 1.0:
                raise E2EFailure(f"Insufficient outbound speech: {outbound_stats.speech_percent:.2f}%")
            print(
                "outbound_audio="
                f"{len(outbound_bytes) / 2 / args.output_sample_rate:.3f}s "
                f"speech={outbound_stats.speech_percent:.2f}%"
            )

            await ws.send(json.dumps({"event": "stop"}))
            await wait_event(ep, "call_terminated", args.timeout_seconds)
    finally:
        await asyncio.to_thread(ep.shutdown)


def run(args):
    print(f"dry_run={args.dry_run}")
    print(f"input_sample_rate={args.input_sample_rate}")
    print(f"output_sample_rate={args.output_sample_rate}")
    if args.dry_run:
        print("dry run passed")
        return 0
    asyncio.run(run_smoke(args))
    print("audio stream smoke passed")
    return 0


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    try:
        return run(args)
    except E2EFailure as exc:
        print(f"E2E FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
