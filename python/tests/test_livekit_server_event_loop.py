"""Regression tests for livekit server event loop handler exception isolation.

Both audio_stream_server.py and server.py had `while True:` dispatchers
where exceptions inside event handlers (e.g., KeyError, AttributeError)
propagated out and silently killed the event_task. The main thread would
wait on stop.wait() indefinitely, completely unaware the server was dead.

These tests exercise the _event_loop methods directly against a fake
endpoint that injects events (including malformed ones) and verify the
loop keeps dispatching.
"""

import asyncio
import pytest


# Import lazily inside fixtures to avoid loading the whole module if
# optional deps are missing.


class FakeEventEndpoint:
    """Fake endpoint with an injectable event queue for wait_for_event."""

    def __init__(self):
        self._events = []
        self._idx = 0
        self.answer_calls = 0
        self.clear_buffer_calls = 0
        self.shutdown_flag = False

    def push_event(self, ev):
        self._events.append(ev)

    def wait_for_event(self, timeout_ms):
        # Called via run_in_executor → blocking API
        if self.shutdown_flag:
            return None
        if self._idx < len(self._events):
            ev = self._events[self._idx]
            self._idx += 1
            return ev
        # Simulate a timeout tick
        import time
        time.sleep(timeout_ms / 1000.0)
        return None

    def answer(self, session_id):
        self.answer_calls += 1

    def clear_buffer(self, session_id):
        self.clear_buffer_calls += 1

    def hangup(self, session_id): pass
    def shutdown(self): self.shutdown_flag = True


class _FakeSession:
    def __init__(self, session_id, remote_uri="sip:caller@x"):
        self.session_id = session_id
        self.remote_uri = remote_uri
        self.local_uri = "stream-id-1"
        self.extra_headers = {}


@pytest.mark.asyncio
async def test_audio_stream_server_event_loop_survives_handler_exception():
    """Regression: handler exception must not kill the dispatcher.

    We inject a sequence of events where one causes a handler exception
    (the 'call_terminated' dispatch path touches ctx._room which raises),
    and verify that a subsequent event is still processed.
    """
    from agent_transport.sip.livekit.audio_stream_server import AudioStreamServer

    srv = AudioStreamServer.__new__(AudioStreamServer)
    srv._ep = FakeEventEndpoint()
    srv._session_contexts = {}
    srv._session_ended_events = {}

    # Craft a context whose attribute access raises — to force an
    # exception inside the call_terminated handler branch.
    class BrokenCtx:
        def __getattr__(self, name):
            raise RuntimeError("simulated ctx failure")

    srv._session_contexts["session-dead"] = BrokenCtx()

    # Push events:
    #   1. incoming_call (normal)
    #   2. call_terminated (triggers BrokenCtx attribute access → exception)
    #   3. incoming_call (must still be processed after #2 raised)
    srv._ep.push_event({
        "type": "incoming_call",
        "session": _FakeSession("session-A"),
    })
    srv._ep.push_event({
        "type": "call_terminated",
        "session": _FakeSession("session-dead"),
        "reason": "test",
    })
    srv._ep.push_event({
        "type": "incoming_call",
        "session": _FakeSession("session-B"),
    })

    loop_task = asyncio.create_task(srv._event_loop())

    # Poll until we've seen the events we care about
    for _ in range(50):
        await asyncio.sleep(0.02)
        # Count incoming events via the fake endpoint idx
        if srv._ep._idx >= 3:
            break

    # Stop the loop
    srv._ep.shutdown_flag = True
    loop_task.cancel()
    try:
        await asyncio.wait_for(loop_task, timeout=2.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    # All three events must have been pulled — exception on #2 did not
    # prevent #3 from being processed.
    assert srv._ep._idx == 3, f"processed only {srv._ep._idx}/3 events"


@pytest.mark.asyncio
async def test_sip_server_event_loop_survives_handler_exception():
    """Same regression test for the SIP (non-audio_stream) server."""
    from agent_transport.sip.livekit.server import AgentServer

    srv = AgentServer.__new__(AgentServer)
    srv._ep = FakeEventEndpoint()
    srv._call_contexts = {}
    srv._call_ended_events = {}

    class BrokenCtx:
        def __getattr__(self, name):
            raise RuntimeError("simulated ctx failure")

    srv._call_contexts["call-dead"] = BrokenCtx()

    srv._ep.push_event({
        "type": "call_terminated",
        "session": _FakeSession("call-dead"),
        "reason": "test",
    })
    srv._ep.push_event({
        "type": "dtmf_received",
        "session_id": "call-X",
        "digit": "5",
    })
    srv._ep.push_event({
        "type": "beep_timeout",
        "session_id": "call-X",
    })

    loop_task = asyncio.create_task(srv._sip_event_loop())

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

    assert srv._ep._idx == 3, f"processed only {srv._ep._idx}/3 events"
