"""Regression tests for TransportRoom._on_session_ended.

Previously, `_on_session_ended` accessed ``self._track_forward_tasks`` directly,
but this attribute lives on ``_TransportLocalParticipant``, not on
``TransportRoom``. Every session end crashed with AttributeError:

    AttributeError: 'TransportRoom' object has no attribute '_track_forward_tasks'

These tests verify that session cleanup:
- Does not raise when there are no forwarded tracks
- Cancels any forwarding tasks on the local participant
- Emits the 'disconnected' event regardless
- Calls stop_recording on the endpoint
"""

import asyncio
import pytest

from agent_transport.sip.livekit._room_facade import TransportRoom


class FakeEndpoint:
    def __init__(self):
        self.input_sample_rate = 8000
        self.stop_recording_calls = 0

    def stop_recording(self, session_id):
        self.stop_recording_calls += 1


@pytest.mark.asyncio
async def test_on_session_ended_without_tracks_does_not_crash():
    """Regression: bare TransportRoom (no background audio) must cleanly end."""
    ep = FakeEndpoint()
    room = TransportRoom(
        endpoint=ep, session_id="call-1",
        agent_name="agent", caller_identity="sip:caller@x",
    )

    disconnected = []
    room.on("disconnected", lambda *a, **k: disconnected.append(True))

    # Should not raise AttributeError
    room._on_session_ended()

    assert room._connected is False
    assert disconnected == [True]
    assert ep.stop_recording_calls == 1


@pytest.mark.asyncio
async def test_on_session_ended_cancels_forwarding_tasks():
    """Track forwarding tasks on the local participant are cancelled."""
    ep = FakeEndpoint()
    room = TransportRoom(
        endpoint=ep, session_id="call-2",
        agent_name="agent", caller_identity="sip:caller@x",
    )

    # Simulate a long-running forwarding task owned by the local participant
    async def _forever():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise

    task = asyncio.create_task(_forever())
    room._local_participant._track_forward_tasks["track-1"] = task

    # Yield to let the task start
    await asyncio.sleep(0)
    assert not task.done()

    room._on_session_ended()

    # Give cancellation a moment to propagate
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.CancelledError:
        pass

    assert task.cancelled() or task.done()
    assert "track-1" not in room._local_participant._track_forward_tasks


@pytest.mark.asyncio
async def test_on_session_ended_survives_stop_recording_failure():
    """If ep.stop_recording() raises, cleanup still completes."""
    class FailingEp(FakeEndpoint):
        def stop_recording(self, session_id):
            self.stop_recording_calls += 1
            raise RuntimeError("simulated")

    ep = FailingEp()
    room = TransportRoom(
        endpoint=ep, session_id="call-3",
        agent_name="agent", caller_identity="sip:caller@x",
    )
    emitted = []
    room.on("disconnected", lambda *a, **k: emitted.append(True))

    room._on_session_ended()  # Must not raise

    assert room._connected is False
    assert ep.stop_recording_calls == 1
    assert emitted == [True]
