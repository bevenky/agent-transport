"""Regression test for the `_forward_audio` stale-frame guard.

Mirrors upstream `_ParticipantAudioOutput._forward_audio`
(livekit/agents/voice/room_io/_output.py), which gates frame forwarding
on BOTH `_interrupted_event.is_set()` AND `_pushed_duration == 0`.

The `_pushed_duration == 0` branch protects against a specific race:

1. Speech handle 1 runs to completion: `_pushed_duration` accumulates
   while frames are captured, then `_wait_for_playout` completes,
   resets `_pushed_duration = 0`, and clears `_interrupted_event`.
2. A stale frame for speech handle 1 was still sitting in `_audio_buf`
   (the Chan) at the time the reset happened — e.g., because the
   `_forward_audio` task was momentarily blocked or hadn't yet
   consumed it. Could also come from preemptive generation queueing
   into the bstream before the next `capture_frame` call.
3. Without the `_pushed_duration == 0` check, `_forward_audio` would
   wake up, see `_interrupted_event` cleared, see no interruption in
   progress, and **replay the stale frame** as if it belonged to the
   next turn.
4. With the check, `_forward_audio` skips it because the field is
   still zero (next turn hasn't started capturing yet).

Previously `audio_stream_io.py` only checked `_interrupted_event.is_set()`
and missed this case. This test pins both the SIP and audio_stream
variants to upstream's behavior so neither regresses.
"""

import asyncio
import pytest

from livekit import rtc

from agent_transport.sip.livekit.audio_stream_io import AudioStreamOutput
from agent_transport.sip.livekit.sip_io import SipAudioOutput


class _RecordingAudioSource:
    """Stub AudioSource that records every frame passed to capture_frame()."""

    def __init__(self):
        self.captured: list[rtc.AudioFrame] = []
        self.sample_rate = 8000
        self.num_channels = 1
        self.queued_duration = 0.0

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        self.captured.append(frame)

    def clear_queue(self) -> None:
        pass

    async def wait_for_playout(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


def _make_frame(samples: int = 160) -> rtc.AudioFrame:
    return rtc.AudioFrame(
        data=b"\x01\x00" * samples,
        sample_rate=8000,
        num_channels=1,
        samples_per_channel=samples,
    )


def _bypass_init(cls):
    """Create an *AudioOutput instance by bypassing __init__ so we don't need
    a real endpoint / napi binding. Monkey-patch `on_playback_started` /
    `on_playback_finished` to no-ops since the base class expects private
    fields we didn't initialize.
    """
    from agent_transport.sip.livekit._channel import Chan

    t = cls.__new__(cls)
    t._audio_source = _RecordingAudioSource()
    t._audio_buf = Chan()
    t._playback_enabled = asyncio.Event()
    t._playback_enabled.set()
    t._interrupted_event = asyncio.Event()
    t._first_frame_event = asyncio.Event()
    t._flush_task = None
    t._pushed_duration = 0.0
    # Base AudioOutput uses name-mangled private fields we haven't set up;
    # stub out the callbacks so `on_playback_started` doesn't blow up when
    # _forward_audio fires it on the first forwarded frame.
    t.on_playback_started = lambda *a, **kw: None
    t.on_playback_finished = lambda *a, **kw: None
    return t


def _bypass_init_audio_stream():
    return _bypass_init(AudioStreamOutput)


def _bypass_init_sip():
    return _bypass_init(SipAudioOutput)


# ─── Tests ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_audio_stream_skips_frame_when_pushed_duration_is_zero():
    """AudioStreamOutput._forward_audio must skip frames while
    `_pushed_duration == 0` — they belong to a speech handle that has
    already been finalized (pushed_duration reset).
    """
    t = _bypass_init_audio_stream()
    # Pre-stage a stale frame with _pushed_duration = 0
    t._audio_buf.send_nowait(_make_frame())

    task = asyncio.create_task(t._forward_audio())

    # Give the forward task a tick to consume the frame
    for _ in range(10):
        await asyncio.sleep(0.005)
        if t._audio_buf.empty():
            break

    assert t._audio_source.captured == [], (
        "Stale frame should be skipped while _pushed_duration == 0"
    )

    # Now simulate the next speech turn starting: pushed_duration non-zero
    t._pushed_duration = 0.02  # one 20ms frame of new speech
    t._audio_buf.send_nowait(_make_frame())
    for _ in range(10):
        await asyncio.sleep(0.005)
        if len(t._audio_source.captured) > 0:
            break

    assert len(t._audio_source.captured) == 1, (
        "Frame should be forwarded once _pushed_duration is non-zero"
    )

    # Cleanup
    t._audio_buf.close()
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_sip_skips_frame_when_pushed_duration_is_zero():
    """Same guard on the SIP output transport — already implemented,
    this is a guardrail against regression.
    """
    t = _bypass_init_sip()
    t._audio_buf.send_nowait(_make_frame())

    task = asyncio.create_task(t._forward_audio())

    for _ in range(10):
        await asyncio.sleep(0.005)
        if t._audio_buf.empty():
            break

    assert t._audio_source.captured == [], (
        "SipAudioOutput must skip frames while _pushed_duration == 0"
    )

    t._pushed_duration = 0.02
    t._audio_buf.send_nowait(_make_frame())
    for _ in range(10):
        await asyncio.sleep(0.005)
        if len(t._audio_source.captured) > 0:
            break

    assert len(t._audio_source.captured) == 1

    t._audio_buf.close()
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_audio_stream_skips_frame_while_interrupted():
    """Existing behavior: frames are also skipped while `_interrupted_event`
    is set. Keep this working alongside the new guard.
    """
    t = _bypass_init_audio_stream()
    t._pushed_duration = 0.02
    t._interrupted_event.set()
    t._audio_buf.send_nowait(_make_frame())

    task = asyncio.create_task(t._forward_audio())
    for _ in range(10):
        await asyncio.sleep(0.005)
        if t._audio_buf.empty():
            break

    assert t._audio_source.captured == [], "Interrupted frames must be skipped"

    t._audio_buf.close()
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_both_outputs_have_matching_forward_audio_guards():
    """Guardrail: source-level check that both variants use the same
    stale-frame guard. Failing this means the two implementations have
    drifted out of sync again.
    """
    import inspect
    sip_src = inspect.getsource(SipAudioOutput._forward_audio)
    as_src = inspect.getsource(AudioStreamOutput._forward_audio)
    needle = "_interrupted_event.is_set() or self._pushed_duration == 0"
    assert needle in sip_src, (
        f"SipAudioOutput._forward_audio missing guard: {needle!r}"
    )
    assert needle in as_src, (
        f"AudioStreamOutput._forward_audio missing guard: {needle!r}"
    )
