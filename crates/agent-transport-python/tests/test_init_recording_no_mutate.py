"""Regression test for _StubJobContext.init_recording options restore.

Previously, init_recording set ``options["audio"] = False`` on the caller's
dict and never restored it, leaking a side-effect into user state.

The fix captures the original value and restores it in ``_on_session_end``,
so the caller's dict round-trips across the session lifecycle. This test
verifies both halves of the contract:

1. ``options["audio"] = False`` is applied while recording is active (so
   LiveKit's RecorderIO doesn't duplicate what Rust already records).
2. ``_on_session_end`` restores the original value (True / absent).
"""

import asyncio
import pytest

from agent_transport.sip.livekit._room_facade import TransportRoom, _StubJobContext


class FakeEndpoint:
    def __init__(self):
        self.input_sample_rate = 8000
        self.started = []
        self.stopped = []

    def start_recording(self, session_id, path, stereo):
        self.started.append((session_id, path, stereo))

    def stop_recording(self, session_id):
        self.stopped.append(session_id)


def _make_ctx():
    ep = FakeEndpoint()
    room = TransportRoom(
        endpoint=ep, session_id="call-1",
        agent_name="agent", caller_identity="sip:caller@x",
    )
    return _StubJobContext(room=room, agent_name="agent"), room, ep


def test_init_recording_sets_audio_false_while_active():
    ctx, _, ep = _make_ctx()
    options = {"audio": True, "video": False, "text": True}

    ctx.init_recording(options)

    assert options["audio"] is False
    # Other keys untouched.
    assert options["video"] is False
    assert options["text"] is True
    # The Rust endpoint was asked to start recording.
    assert len(ep.started) == 1
    assert ep.started[0][0] == "call-1"


@pytest.mark.asyncio
async def test_init_recording_restores_audio_on_session_end():
    ctx, _, _ = _make_ctx()
    options = {"audio": True}
    ctx.init_recording(options)
    assert options["audio"] is False

    await ctx._on_session_end()

    # Original value restored — user code that inspects options["audio"]
    # after session end sees what they originally set.
    assert options["audio"] is True


@pytest.mark.asyncio
async def test_init_recording_restores_missing_audio_key():
    """If the user didn't supply ``audio``, it must not be left behind."""
    ctx, _, _ = _make_ctx()
    # init_recording only runs if options["audio"] is truthy, so this
    # path covers the "user passed {audio: True} and we cleared it" case.
    options = {"audio": True}
    ctx.init_recording(options)

    # Simulate a broken session where the original flag was never set
    # (shouldn't happen in normal flow, but the restore must be robust).
    ctx._original_audio_recording_flag = None
    await ctx._on_session_end()

    # None means "the key wasn't present originally" → pop.
    assert "audio" not in options


def test_init_recording_noop_when_audio_disabled():
    """If the caller passed audio=False, init_recording should not start
    recording at all and must not touch the dict.
    """
    ctx, _, ep = _make_ctx()
    options = {"audio": False}
    ctx.init_recording(options)
    assert options == {"audio": False}
    assert ep.started == []
