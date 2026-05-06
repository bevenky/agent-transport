"""Regression tests for audio_stream teardown while TTS is still playing."""

import asyncio

import pytest
from livekit import rtc

from agent_transport.sip.livekit._audio_source import AudioStreamAudioSource
from agent_transport.sip.livekit.audio_stream_io import AudioStreamOutput


def _make_frame(samples: int = 400) -> rtc.AudioFrame:
    return rtc.AudioFrame(
        data=b"\x01\x00" * samples,
        sample_rate=8000,
        num_channels=1,
        samples_per_channel=samples,
    )


async def _wait_for(predicate, *, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not satisfied before timeout")
        await asyncio.sleep(0.01)


class _EndpointBase:
    output_sample_rate = 8000

    def __init__(self):
        self.send_audio_calls = 0
        self.clear_buffer_calls = 0
        self.flush_calls = 0
        self.wait_for_playout_calls = 0

    def queued_duration_ms(self, session_id):
        return 0.0

    def clear_buffer(self, session_id):
        self.clear_buffer_calls += 1

    def pause(self, session_id):
        pass

    def resume(self, session_id):
        pass


class _SendRaisesCallNotActiveEndpoint(_EndpointBase):
    def send_audio_notify(self, session_id, audio, sample_rate, num_channels, callback):
        self.send_audio_calls += 1
        raise RuntimeError(f"call not active: {session_id}")


class _SendNeverCompletesEndpoint(_EndpointBase):
    def __init__(self):
        super().__init__()
        self.pending_callback = None

    def send_audio_notify(self, session_id, audio, sample_rate, num_channels, callback):
        self.send_audio_calls += 1
        self.pending_callback = callback


class _PlayoutRaisesEndpoint(_EndpointBase):
    def __init__(self, *, fail_flush: bool):
        super().__init__()
        self.fail_flush = fail_flush

    def send_audio_notify(self, session_id, audio, sample_rate, num_channels, callback):
        callback()

    def flush(self, session_id):
        self.flush_calls += 1
        if self.fail_flush:
            raise RuntimeError(f"call not active: {session_id}")

    def wait_for_playout(self, session_id, timeout_ms):
        self.wait_for_playout_calls += 1
        raise RuntimeError(f"call not active: {session_id}")


@pytest.mark.asyncio
async def test_audio_stream_forward_audio_marks_call_not_active_as_interrupted():
    ep = _SendRaisesCallNotActiveEndpoint()
    output = AudioStreamOutput(ep, "session-dead")

    await output.capture_frame(_make_frame())
    await _wait_for(lambda: ep.send_audio_calls == 1)

    ev = await asyncio.wait_for(output.wait_for_playout(), timeout=1.0)

    assert ev.interrupted is True
    await output.aclose()


@pytest.mark.asyncio
async def test_audio_stream_transport_close_resolves_pending_capture_future():
    ep = _SendNeverCompletesEndpoint()
    output = AudioStreamOutput(ep, "session-dead")

    await output.capture_frame(_make_frame())
    await _wait_for(lambda: ep.pending_callback is not None)

    output.mark_transport_closed()
    ev = await asyncio.wait_for(output.wait_for_playout(), timeout=1.0)

    assert ev.interrupted is True
    assert output._audio_source._pending_capture_futs == set()
    await output.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_flush", [True, False])
async def test_audio_stream_playout_call_not_active_notifies_output(fail_flush):
    terminal_count = 0

    def _on_terminal():
        nonlocal terminal_count
        terminal_count += 1

    ep = _PlayoutRaisesEndpoint(fail_flush=fail_flush)
    src = AudioStreamAudioSource(
        ep,
        "session-dead",
        sample_rate=8000,
        on_terminal_error=_on_terminal,
    )

    await asyncio.wait_for(src.wait_for_playout(), timeout=1.0)

    assert terminal_count == 1
    assert ep.flush_calls == 1
    assert ep.wait_for_playout_calls == (0 if fail_flush else 1)


@pytest.mark.asyncio
async def test_audio_stream_call_terminated_notifies_output_before_session_close():
    from agent_transport.sip.livekit.audio_stream_server import AudioStreamServer

    class FakeEndpoint:
        def __init__(self):
            self._events = []
            self._idx = 0
            self.clear_buffer_calls = 0
            self.shutdown_flag = False

        def push_event(self, ev):
            self._events.append(ev)

        def wait_for_event(self, timeout_ms):
            if self.shutdown_flag:
                return None
            if self._idx < len(self._events):
                ev = self._events[self._idx]
                self._idx += 1
                return ev
            import time
            time.sleep(timeout_ms / 1000.0)
            return None

        def clear_buffer(self, session_id):
            self.clear_buffer_calls += 1

    class FakeEventSession:
        session_id = "session-dead"

    class FakeAudioOutput:
        def __init__(self):
            self.mark_transport_closed_calls = 0

        def mark_transport_closed(self):
            self.mark_transport_closed_calls += 1

    class FakeSession:
        def __init__(self):
            self.output = type("Output", (), {"audio": FakeAudioOutput()})()
            self.shutdown_calls = []

        def shutdown(self, *, drain=True):
            self.shutdown_calls.append(drain)

    class FakeCtx:
        def __init__(self):
            self._session = FakeSession()
            self._room = None

    srv = AudioStreamServer.__new__(AudioStreamServer)
    srv._ep = FakeEndpoint()
    ctx = FakeCtx()
    srv._session_contexts = {"session-dead": ctx}
    srv._session_ended_events = {"session-dead": asyncio.Event()}

    srv._ep.push_event({
        "type": "call_terminated",
        "session": FakeEventSession(),
        "reason": "ws disconnected",
    })

    loop_task = asyncio.create_task(srv._event_loop())
    await _wait_for(lambda: srv._ep._idx == 1)
    srv._ep.shutdown_flag = True
    loop_task.cancel()
    try:
        await asyncio.wait_for(loop_task, timeout=1.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    audio = ctx._session.output.audio
    assert audio.mark_transport_closed_calls == 1
    assert ctx._session.shutdown_calls == []
    assert srv._ep.clear_buffer_calls == 1
    assert srv._session_ended_events["session-dead"].is_set()
