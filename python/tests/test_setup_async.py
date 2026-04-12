"""Regression tests for async setup function handling.

Previously, WebsocketServerTransport, AgentServer and AudioStreamServer all
called setup_fnc() synchronously. If the user defined an async setup function:

    @server.setup()
    async def prewarm():
        return {"vad": await load_vad()}

the call returned a coroutine, which was never awaited. isinstance(result, dict)
was False → userdata stayed empty → every handler that reached into
userdata["vad"] crashed with KeyError.

These tests use the shared ``call_setup`` helper to verify both calling
conventions (``fn(proc)`` and ``fn()``) work across sync and async variants.
"""

import pytest

from agent_transport.sip.livekit._aio_utils import call_setup


class _FakeProc:
    def __init__(self):
        self.userdata = {}


# ─── Sync, old pattern: fn() -> dict ────────────────────────────────────────

@pytest.mark.asyncio
async def test_sync_setup_old_pattern_returns_dict():
    def setup_fn():
        return {"vad": "loaded"}

    proc = _FakeProc()
    await call_setup(setup_fn, proc)
    assert proc.userdata == {"vad": "loaded"}


# ─── Sync, new pattern: fn(proc) that mutates proc.userdata ─────────────────

@pytest.mark.asyncio
async def test_sync_setup_new_pattern_mutates_proc():
    def setup_fn(proc):
        proc.userdata["turn"] = "loaded"

    proc = _FakeProc()
    await call_setup(setup_fn, proc)
    assert proc.userdata == {"turn": "loaded"}


# ─── Async, old pattern: async fn() -> dict ─────────────────────────────────

@pytest.mark.asyncio
async def test_async_setup_old_pattern_returns_dict():
    """Regression: async fn() used to return an un-awaited coroutine."""
    async def setup_fn():
        return {"vad": "async-loaded"}

    proc = _FakeProc()
    await call_setup(setup_fn, proc)
    assert proc.userdata == {"vad": "async-loaded"}


# ─── Async, new pattern: async fn(proc) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_async_setup_new_pattern_mutates_proc():
    """Regression: async fn(proc) used to leak as un-awaited coroutine."""
    async def setup_fn(proc):
        proc.userdata["vad"] = "async-new"

    proc = _FakeProc()
    await call_setup(setup_fn, proc)
    assert proc.userdata == {"vad": "async-new"}


# ─── Sync fn that returns an awaitable (coroutine from a wrapper) ───────────

@pytest.mark.asyncio
async def test_sync_setup_returning_awaitable_is_awaited():
    async def inner():
        return {"from": "inner"}

    def setup_fn():
        return inner()

    proc = _FakeProc()
    await call_setup(setup_fn, proc)
    assert proc.userdata == {"from": "inner"}


# ─── Exception propagation ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_setup_exception_propagates():
    async def setup_fn(proc):
        raise RuntimeError("kaboom")

    proc = _FakeProc()
    with pytest.raises(RuntimeError, match="kaboom"):
        await call_setup(setup_fn, proc)
