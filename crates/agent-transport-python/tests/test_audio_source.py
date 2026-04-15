"""Regression tests for SipAudioSource and AudioStreamAudioSource.

These tests use a fake endpoint to isolate the Python adapter logic
from the Rust FFI. They pin down the bug fixes in _audio_source.py:

- wait_for_playout must resolve the shared future even when the Rust
  FFI call raises, so concurrent awaiters don't hang.
- Previously, when ep.wait_for_playout_notify (SipAudioSource) or
  ep.flush (AudioStreamAudioSource) raised, the future was set to
  None without being resolved, orphaning any concurrent awaiters.
"""

import asyncio
import pytest

from agent_transport.sip.livekit._audio_source import (
    SipAudioSource,
    AudioStreamAudioSource,
)


class FakeEndpointRaising:
    """Fake endpoint whose wait_for_playout_notify / flush always raise."""

    def __init__(self):
        self.wait_for_playout_notify_calls = 0
        self.flush_calls = 0
        self.wait_for_playout_calls = 0

    # SipEndpoint-style API
    def send_audio_notify(self, *args, **kwargs): pass
    def clear_buffer(self, *args, **kwargs): pass
    def queued_duration_ms(self, *args): return 0.0

    def wait_for_playout_notify(self, session_id, callback):
        self.wait_for_playout_notify_calls += 1
        raise RuntimeError("simulated FFI failure")

    # AudioStreamEndpoint extras
    def flush(self, session_id):
        self.flush_calls += 1
        raise RuntimeError("simulated flush failure")

    def wait_for_playout(self, session_id, timeout_ms):
        self.wait_for_playout_calls += 1
        return True


class FakeEndpointOK:
    """Fake endpoint where callbacks fire normally."""

    def __init__(self):
        self._pending_cb = None
        self._wait_result = True

    def send_audio_notify(self, *args, **kwargs): pass
    def clear_buffer(self, *args, **kwargs): pass
    def queued_duration_ms(self, *args): return 0.0

    def wait_for_playout_notify(self, session_id, callback):
        self._pending_cb = callback

    def fire_playout(self):
        cb, self._pending_cb = self._pending_cb, None
        if cb:
            cb()

    def flush(self, session_id): pass
    def wait_for_playout(self, session_id, timeout_ms):
        return self._wait_result


@pytest.mark.asyncio
async def test_sip_audio_source_wait_for_playout_resolves_on_ffi_error():
    """Regression: orphaned future when wait_for_playout_notify FFI raises.

    Two callers share self._playout_fut. If the FFI call raises, the first
    caller used to clear _playout_fut = None without resolving it, hanging
    the second caller on the orphaned future.
    """
    ep = FakeEndpointRaising()
    loop = asyncio.get_running_loop()
    src = SipAudioSource.__new__(SipAudioSource)
    src._ep = ep
    src._id = "test"
    src._loop = loop
    src._playout_fut = None
    src._disposed = False

    # Concurrent waiters — second enters before first has set _playout_fut = None
    first_task = asyncio.create_task(src.wait_for_playout())
    # Yield so first coroutine creates the shared future
    await asyncio.sleep(0)
    second_task = asyncio.create_task(src.wait_for_playout())

    # Both must complete (not hang) within a short timeout
    await asyncio.wait_for(
        asyncio.gather(first_task, second_task, return_exceptions=True),
        timeout=1.0,
    )
    assert ep.wait_for_playout_notify_calls >= 1


@pytest.mark.asyncio
async def test_sip_audio_source_wait_for_playout_normal_flow():
    """Happy path: callback fires, future resolves."""
    ep = FakeEndpointOK()
    loop = asyncio.get_running_loop()
    src = SipAudioSource.__new__(SipAudioSource)
    src._ep = ep
    src._id = "test"
    src._loop = loop
    src._playout_fut = None
    src._disposed = False

    task = asyncio.create_task(src.wait_for_playout())
    await asyncio.sleep(0)  # let task register callback

    # Simulate Rust RTP thread firing the callback
    ep.fire_playout()

    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_audio_stream_audio_source_wait_for_playout_resolves_on_flush_error():
    """Regression: orphaned future when ep.flush() raises.

    In the prior implementation, the inner try's except:return skipped the
    finally block that resolves the shared future, hanging any concurrent
    awaiters and violating the shared-future contract.
    """
    ep = FakeEndpointRaising()
    loop = asyncio.get_running_loop()
    src = AudioStreamAudioSource.__new__(AudioStreamAudioSource)
    src._ep = ep
    src._id = "test"
    src._loop = loop
    src._playout_fut = None
    src._plivo_playout_fut = None
    src._disposed = False

    # Two concurrent callers — both must complete, not hang
    tasks = [asyncio.create_task(src.wait_for_playout()) for _ in range(2)]

    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2.0)
    assert ep.flush_calls >= 1


@pytest.mark.asyncio
async def test_audio_stream_audio_source_wait_for_playout_normal_flow():
    """Happy path: flush succeeds, wait_for_playout returns True."""
    ep = FakeEndpointOK()
    loop = asyncio.get_running_loop()
    src = AudioStreamAudioSource.__new__(AudioStreamAudioSource)
    src._ep = ep
    src._id = "test"
    src._loop = loop
    src._playout_fut = None
    src._plivo_playout_fut = None
    src._disposed = False

    await asyncio.wait_for(src.wait_for_playout(), timeout=2.0)
