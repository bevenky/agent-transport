"""Regression tests for pipecat event loop handler exception isolation.

Previously, if `_handle_event` raised any exception, it would propagate
out of `_event_loop_from_queue` / `_event_loop_from_endpoint`, killing
the event dispatcher task silently. The loop must survive handler
exceptions and continue processing subsequent events.
"""

import asyncio
import pytest

from agent_transport.sip.pipecat.sip_transport import SipInputTransport
from agent_transport.audio_stream.pipecat.audio_stream_transport import (
    AudioStreamInputTransport,
)


def _make_sip_input():
    """Construct SipInputTransport without running its full __init__."""
    t = SipInputTransport.__new__(SipInputTransport)
    t._cid = "call-test"
    t._started = True
    t._event_queue = asyncio.Queue()
    t._ep = None
    t._transport = None
    return t


def _make_audio_stream_input():
    t = AudioStreamInputTransport.__new__(AudioStreamInputTransport)
    t._sid = "session-test"
    t._started = True
    t._event_queue = asyncio.Queue()
    t._ep = None
    t._transport = None
    return t


@pytest.mark.asyncio
async def test_sip_event_loop_survives_handler_exception():
    """Regression: a bad event must not kill the dispatcher."""
    t = _make_sip_input()
    handled = []

    async def _bad_handler(event):
        handled.append(event.get("type"))
        if event.get("type") == "bad":
            raise ValueError("simulated handler bug")

    t._handle_event = _bad_handler

    loop_task = asyncio.create_task(t._event_loop_from_queue())

    # Push: good → bad → good → shutdown
    await t._event_queue.put({"type": "good1"})
    await t._event_queue.put({"type": "bad"})
    await t._event_queue.put({"type": "good2"})

    # Give the loop a chance to process
    for _ in range(20):
        await asyncio.sleep(0.01)
        if len(handled) >= 3:
            break

    t._started = False
    await asyncio.wait_for(loop_task, timeout=2.0)

    assert "good1" in handled
    assert "bad" in handled
    assert "good2" in handled, "loop must continue after handler exception"


@pytest.mark.asyncio
async def test_audio_stream_event_loop_survives_handler_exception():
    t = _make_audio_stream_input()
    handled = []

    async def _bad_handler(event):
        handled.append(event.get("type"))
        if event.get("type") == "boom":
            raise KeyError("simulated missing key")

    t._handle_event = _bad_handler

    loop_task = asyncio.create_task(t._event_loop_from_queue())

    await t._event_queue.put({"type": "a"})
    await t._event_queue.put({"type": "boom"})
    await t._event_queue.put({"type": "b"})
    await t._event_queue.put({"type": "c"})

    for _ in range(20):
        await asyncio.sleep(0.01)
        if len(handled) >= 4:
            break

    t._started = False
    await asyncio.wait_for(loop_task, timeout=2.0)

    assert handled == ["a", "boom", "b", "c"], f"got {handled}"


@pytest.mark.asyncio
async def test_sip_event_loop_stops_on_running_false():
    """Loop exits cleanly when _started is set to False."""
    t = _make_sip_input()

    async def _handle(event):
        pass

    t._handle_event = _handle
    loop_task = asyncio.create_task(t._event_loop_from_queue())
    await asyncio.sleep(0.05)
    t._started = False
    await asyncio.wait_for(loop_task, timeout=2.0)
