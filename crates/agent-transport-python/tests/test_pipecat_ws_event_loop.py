"""Regression test for Pipecat WebsocketServerTransport._event_loop.

The server-side event dispatcher previously had no exception handling
around event processing. Any exception (e.g., KeyError from a malformed
event, AttributeError from a ctx access) would propagate out and silently
kill the dispatcher task, leaving the server unresponsive.

This test injects a malformed event followed by a well-formed one and
verifies the dispatcher continues running.
"""

import asyncio
import pytest

from agent_transport.audio_stream.pipecat.transports.websocket import (
    WebsocketServerTransport,
)


class FakeEp:
    """Fake endpoint with an injectable event list for wait_for_event."""

    def __init__(self):
        self._events = []
        self._idx = 0
        self.shutdown_flag = False

    def push_event(self, ev):
        self._events.append(ev)

    def wait_for_event(self, timeout_ms=1000):
        if self.shutdown_flag:
            return None
        if self._idx < len(self._events):
            ev = self._events[self._idx]
            self._idx += 1
            return ev
        import time
        time.sleep(timeout_ms / 1000.0)
        return None

    def shutdown(self):
        self.shutdown_flag = True


class _FakeSession:
    def __init__(self, sid, call_uuid="cu", local_uri="stream"):
        self.session_id = sid
        self.call_uuid = call_uuid
        self.remote_uri = "sip:x@x"
        self.local_uri = local_uri
        self.direction = "inbound"
        self.extra_headers = {}


@pytest.mark.asyncio
async def test_ws_event_loop_survives_malformed_event():
    srv = WebsocketServerTransport.__new__(WebsocketServerTransport)
    srv._ep = FakeEp()
    srv._session_event_queues = {}
    srv._active_sessions = {}
    srv._session_start_times = {}

    # Malformed: missing "session" key → KeyError inside handler
    srv._ep.push_event({"type": "incoming_call"})
    # Well-formed
    srv._ep.push_event({
        "type": "incoming_call",
        "session": _FakeSession("session-good"),
    })
    # Another well-formed dtmf_received
    srv._ep.push_event({
        "type": "dtmf_received",
        "session_id": "unknown",
        "digit": "5",
    })

    loop_task = asyncio.create_task(srv._event_loop())

    for _ in range(50):
        await asyncio.sleep(0.02)
        if srv._ep._idx >= 3:
            break

    srv._ep.shutdown_flag = True
    loop_task.cancel()
    try:
        await asyncio.wait_for(loop_task, timeout=2.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    assert srv._ep._idx == 3, f"processed {srv._ep._idx}/3 events"
