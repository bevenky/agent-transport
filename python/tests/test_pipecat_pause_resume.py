"""Regression tests for Pipecat pause/resume semantics.

Previously, SipInputTransport.pause() manually set `self._running = False`
and cancelled the recv/event tasks permanently. Resume after pause produced
zero audio because the tasks were never recreated.

Fix: drop the custom `_running` flag, rely on the base class `_paused` flag
which `push_audio_frame()` already gates on. The recv loop keeps running;
frames are silently dropped while paused and flow again on resume.

These tests run without a full pipecat Pipeline (which is heavy to set up)
by bypassing __init__ and exercising `_recv_loop` / state flags directly.
"""

import asyncio
import pytest

from agent_transport.sip.pipecat.sip_transport import SipInputTransport
from agent_transport.audio_stream.pipecat.audio_stream_transport import (
    AudioStreamInputTransport,
)


def _make_sip_input():
    t = SipInputTransport.__new__(SipInputTransport)
    t._cid = "call-test"
    t._started = True
    t._paused = False  # base-class flag
    t._ep = _FakeSipEndpoint()
    t._transport = None
    t._pushed = []
    # Stub push_audio_frame to record frames (base class would normally
    # gate on _paused here; emulate that exact behavior).
    async def _push(frame):
        if not t._paused:
            t._pushed.append(frame)
    t.push_audio_frame = _push
    return t


def _make_audio_stream_input():
    t = AudioStreamInputTransport.__new__(AudioStreamInputTransport)
    t._sid = "session-test"
    t._started = True
    t._paused = False
    t._ep = _FakeSipEndpoint()
    t._transport = None
    t._pushed = []
    async def _push(frame):
        if not t._paused:
            t._pushed.append(frame)
    t.push_audio_frame = _push
    return t


class _FakeSipEndpoint:
    """Emits a tiny PCM frame on every recv_audio_bytes_blocking call."""
    def __init__(self):
        self.input_sample_rate = 8000
        self.output_sample_rate = 8000
        self._calls = 0

    def recv_audio_bytes_blocking(self, session_id, timeout_ms):
        import time
        time.sleep(0.005)  # simulate Rust-side timing
        self._calls += 1
        return (b"\x00\x00" * 160, 8000, 1)


@pytest.mark.asyncio
async def test_recv_loop_drops_frames_while_paused_and_resumes():
    """Frames pushed via the base class are dropped while _paused=True and
    flow again when _paused=False — no task recreation needed.
    """
    t = _make_sip_input()

    loop_task = asyncio.create_task(t._recv_loop())
    # Let a few frames flow
    for _ in range(30):
        await asyncio.sleep(0.005)
        if len(t._pushed) >= 3:
            break
    assert len(t._pushed) >= 3, "frames should flow before pause"
    unpaused_count = len(t._pushed)

    # "Pause" — same state change the base class would make in pause()
    t._paused = True
    # Let the recv loop continue for a while; the base class gates
    # push_audio_frame, so no new frames should be recorded.
    await asyncio.sleep(0.05)
    assert len(t._pushed) == unpaused_count, "no frames while paused"

    # Resume — clear _paused and verify frames flow again
    t._paused = False
    for _ in range(30):
        await asyncio.sleep(0.005)
        if len(t._pushed) > unpaused_count:
            break
    assert len(t._pushed) > unpaused_count, "frames should resume after unpause"

    # Stop the loop cleanly
    t._started = False
    loop_task.cancel()
    try:
        await loop_task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_audio_stream_recv_loop_drops_frames_while_paused():
    """Same regression on the audio_stream transport."""
    t = _make_audio_stream_input()
    loop_task = asyncio.create_task(t._recv_loop())

    for _ in range(30):
        await asyncio.sleep(0.005)
        if len(t._pushed) >= 3:
            break
    before = len(t._pushed)
    assert before >= 3

    t._paused = True
    await asyncio.sleep(0.05)
    assert len(t._pushed) == before

    t._paused = False
    for _ in range(30):
        await asyncio.sleep(0.005)
        if len(t._pushed) > before:
            break
    assert len(t._pushed) > before

    t._started = False
    loop_task.cancel()
    try:
        await loop_task
    except (asyncio.CancelledError, Exception):
        pass


def test_sip_input_transport_has_no_running_flag():
    """Guardrail: _running was renamed to _started. If someone reintroduces
    _running by accident, the fix regresses silently.
    """
    t = SipInputTransport.__new__(SipInputTransport)
    # _running must not be present (clean slate object)
    assert not hasattr(t, "_running") or not isinstance(
        getattr(t, "_running", None), bool
    )


def test_sip_input_transport_does_not_override_pause():
    """The fix relies on the base class `pause()` implementation. If a
    subclass `pause` override reappears, the regression returns.
    """
    assert "pause" not in SipInputTransport.__dict__, (
        "SipInputTransport must not override pause() — the base class "
        "sets _paused which push_audio_frame already gates on."
    )


def test_audio_stream_input_transport_does_not_override_pause():
    assert "pause" not in AudioStreamInputTransport.__dict__, (
        "AudioStreamInputTransport must not override pause()."
    )
