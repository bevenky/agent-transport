"""Regression tests for pause/resume state consistency.

Previously, SipAudioOutput and AudioStreamOutput set `_rust_paused = True`
BEFORE calling the FFI. If the FFI raised, the flag stayed True but Rust
wasn't paused, so the next pause() was short-circuited and Rust kept
sending audio.

These tests instantiate the output classes without invoking the LiveKit
base class __init__ (which requires a real AudioOutput parent), then
verify the _rust_paused state transitions on both success and failure.
"""

import pytest

from agent_transport.sip.livekit.sip_io import SipAudioOutput
from agent_transport.sip.livekit.audio_stream_io import AudioStreamOutput


class FakeEndpointSuccess:
    def __init__(self):
        self.pause_calls = 0
        self.resume_calls = 0

    def pause(self, cid):
        self.pause_calls += 1

    def resume(self, cid):
        self.resume_calls += 1


class FakeEndpointFailing:
    def __init__(self):
        self.pause_calls = 0
        self.resume_calls = 0

    def pause(self, cid):
        self.pause_calls += 1
        raise RuntimeError("simulated pause failure")

    def resume(self, cid):
        self.resume_calls += 1
        raise RuntimeError("simulated resume failure")


def _make_sip_output(ep):
    """Construct a SipAudioOutput without calling the base __init__."""
    o = SipAudioOutput.__new__(SipAudioOutput)
    o._ep = ep
    o._cid = "call-test"
    o._rust_paused = False
    # Stub out base-class state that pause()/resume() touches
    import asyncio
    o._playback_enabled = asyncio.Event()
    o._playback_enabled.set()
    o._first_frame_event = asyncio.Event()

    # Monkey-patch super().pause / super().resume to no-ops for the test
    class _Noop:
        def pause(self_inner): pass
        def resume(self_inner): pass
    o._super_stub = _Noop()
    return o


def _make_audio_stream_output(ep):
    o = AudioStreamOutput.__new__(AudioStreamOutput)
    o._ep = ep
    o._sid = "session-test"
    o._rust_paused = False
    import asyncio
    o._playback_enabled = asyncio.Event()
    o._playback_enabled.set()
    o._first_frame_event = asyncio.Event()
    class _Noop:
        def pause(self_inner): pass
        def resume(self_inner): pass
    o._super_stub = _Noop()
    return o


def test_sip_output_pause_resume_success_flow():
    ep = FakeEndpointSuccess()
    o = _make_sip_output(ep)

    # Monkey-patch super().pause/resume via the parent class
    from unittest.mock import patch
    with patch.object(type(o).__mro__[1], 'pause', lambda self: None), \
         patch.object(type(o).__mro__[1], 'resume', lambda self: None):
        assert not o._rust_paused
        o.pause()
        assert o._rust_paused
        assert ep.pause_calls == 1

        # Double pause is idempotent
        o.pause()
        assert o._rust_paused
        assert ep.pause_calls == 1, "second pause() should be short-circuited"

        o.resume()
        assert not o._rust_paused
        assert ep.resume_calls == 1


def test_sip_output_pause_failure_does_not_set_flag():
    """Regression: if ep.pause() raises, _rust_paused must stay False."""
    ep = FakeEndpointFailing()
    o = _make_sip_output(ep)

    from unittest.mock import patch
    with patch.object(type(o).__mro__[1], 'pause', lambda self: None), \
         patch.object(type(o).__mro__[1], 'resume', lambda self: None):
        o.pause()
        # ep.pause() raised → flag must NOT be set to True
        assert not o._rust_paused, "pause failure must leave _rust_paused False"
        assert ep.pause_calls == 1

        # Next pause() retries the FFI call (would hang forever in the bug)
        o.pause()
        assert ep.pause_calls == 2, "failed pause must allow retry on next call"


def test_sip_output_resume_failure_does_not_clear_flag():
    ep_ok = FakeEndpointSuccess()
    o = _make_sip_output(ep_ok)

    from unittest.mock import patch
    with patch.object(type(o).__mro__[1], 'pause', lambda self: None), \
         patch.object(type(o).__mro__[1], 'resume', lambda self: None):
        o.pause()
        assert o._rust_paused

        # Swap to a failing endpoint for resume
        o._ep = FakeEndpointFailing()
        o.resume()
        assert o._rust_paused, "resume failure must leave _rust_paused True"
        # Next resume() will retry
        o._ep = ep_ok
        o.resume()
        assert not o._rust_paused
        assert ep_ok.resume_calls == 1


def test_audio_stream_output_pause_failure_does_not_set_flag():
    ep = FakeEndpointFailing()
    o = _make_audio_stream_output(ep)

    from unittest.mock import patch
    with patch.object(type(o).__mro__[1], 'pause', lambda self: None), \
         patch.object(type(o).__mro__[1], 'resume', lambda self: None):
        o.pause()
        assert not o._rust_paused, "AudioStream pause failure must leave flag False"
        # Retry works
        o.pause()
        assert ep.pause_calls == 2


def test_audio_stream_output_pause_resume_success_flow():
    ep = FakeEndpointSuccess()
    o = _make_audio_stream_output(ep)

    from unittest.mock import patch
    with patch.object(type(o).__mro__[1], 'pause', lambda self: None), \
         patch.object(type(o).__mro__[1], 'resume', lambda self: None):
        o.pause()
        assert o._rust_paused
        assert ep.pause_calls == 1

        o.resume()
        assert not o._rust_paused
        assert ep.resume_calls == 1
