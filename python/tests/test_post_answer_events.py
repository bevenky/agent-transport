"""Regression tests for the post-answer event refactor.

Covers:

1. `server.on("ringing", ...)` fires when Rust emits `call_ringing`.
2. The SIP server creates the agent session on `call_answered` (not the
   legacy `call_media_active`).
3. The audio_stream server creates the agent session on `call_answered`
   (drops the two-phase `incoming_call → call_media_active` pattern).
4. Pipecat SIP `SipServer.call()` starts the session directly after
   `ep.call()` returns, rather than relying on the old "unknown session
   = outbound" fallback in the event loop.
5. Outbound `call_answered` events are skipped when the session id is
   reserved in `_outbound_session_ids` — the outbound path owns session
   creation and must not be double-dispatched by the event loop.
"""

import asyncio
import pytest


# ─── Fake endpoint with an injectable event queue ────────────────────────

class FakeEventEndpoint:
    def __init__(self):
        self._events: list[dict] = []
        self._idx = 0
        self.shutdown_flag = False
        self.call_calls: list[tuple[str, ...]] = []

    def push_event(self, ev: dict) -> None:
        self._events.append(ev)

    def wait_for_event(self, timeout_ms: int):
        import time
        if self.shutdown_flag:
            return None
        if self._idx < len(self._events):
            ev = self._events[self._idx]
            self._idx += 1
            return ev
        time.sleep(timeout_ms / 1000.0)
        return None

    def answer(self, session_id): pass
    def clear_buffer(self, session_id): pass
    def hangup(self, session_id): pass
    def shutdown(self): self.shutdown_flag = True

    # For pipecat SipServer.call() path
    def call(self, dest_uri, from_uri=None, headers=None):
        self.call_calls.append((dest_uri, from_uri, headers))
        return "outbound-session-1"


class _FakeSession:
    def __init__(self, session_id, remote_uri="sip:caller@x", direction="Inbound"):
        self.session_id = session_id
        self.remote_uri = remote_uri
        self.local_uri = "stream-id-1"
        self.extra_headers = {}
        self.call_uuid = f"uuid-{session_id}"
        self.direction = direction


# ─── LiveKit AgentServer (SIP): ringing + call_answered ─────────────────

@pytest.mark.asyncio
async def test_livekit_sip_server_fires_ringing_hook():
    from agent_transport.sip.livekit.server import AgentServer

    srv = AgentServer.__new__(AgentServer)
    srv._ep = FakeEventEndpoint()
    srv._call_contexts = {}
    srv._call_ended_events = {}
    srv._active_calls = {}
    srv._background_tasks = set()
    srv._outbound_session_ids = set()
    srv._server_listeners = {}

    ringing_observed = []

    @srv.on("ringing")
    def _on_ringing(session):
        ringing_observed.append((session.session_id, session.remote_uri))

    # Push a call_ringing event
    srv._ep.push_event({
        "type": "call_ringing",
        "session": _FakeSession("sip-call-1", remote_uri="sip:alice@x"),
    })

    loop_task = asyncio.create_task(srv._sip_event_loop())
    for _ in range(50):
        await asyncio.sleep(0.02)
        if ringing_observed:
            break

    srv._ep.shutdown_flag = True
    loop_task.cancel()
    try:
        await asyncio.wait_for(loop_task, timeout=1.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    assert ringing_observed == [("sip-call-1", "sip:alice@x")], (
        f"expected ringing hook to fire, got {ringing_observed}"
    )


@pytest.mark.asyncio
async def test_livekit_sip_server_starts_session_on_call_answered():
    from agent_transport.sip.livekit.server import AgentServer

    srv = AgentServer.__new__(AgentServer)
    srv._ep = FakeEventEndpoint()
    srv._call_contexts = {}
    srv._call_ended_events = {}
    srv._active_calls = {}
    srv._background_tasks = set()
    srv._outbound_session_ids = set()
    srv._server_listeners = {}

    start_call_invocations = []

    async def fake_start_call(sid, uri, direction):
        start_call_invocations.append((sid, uri, direction))

    srv._start_call = fake_start_call  # type: ignore

    srv._ep.push_event({
        "type": "call_answered",
        "session": _FakeSession("sip-call-42", remote_uri="sip:bob@x"),
    })

    loop_task = asyncio.create_task(srv._sip_event_loop())
    for _ in range(50):
        await asyncio.sleep(0.02)
        if start_call_invocations:
            break

    srv._ep.shutdown_flag = True
    loop_task.cancel()
    try:
        await asyncio.wait_for(loop_task, timeout=1.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    assert start_call_invocations == [("sip-call-42", "sip:bob@x", "inbound")]


@pytest.mark.asyncio
async def test_livekit_sip_server_skips_outbound_call_answered():
    """Outbound sessions are reserved in `_outbound_session_ids`, so the
    event loop must NOT create a second agent session for them.
    """
    from agent_transport.sip.livekit.server import AgentServer

    srv = AgentServer.__new__(AgentServer)
    srv._ep = FakeEventEndpoint()
    srv._call_contexts = {}
    srv._call_ended_events = {}
    srv._active_calls = {}
    srv._background_tasks = set()
    srv._outbound_session_ids = {"outbound-1"}
    srv._server_listeners = {}

    start_call_invocations = []

    async def fake_start_call(sid, uri, direction):
        start_call_invocations.append((sid, uri, direction))

    srv._start_call = fake_start_call  # type: ignore

    srv._ep.push_event({
        "type": "call_answered",
        "session": _FakeSession("outbound-1", direction="Outbound"),
    })

    loop_task = asyncio.create_task(srv._sip_event_loop())
    for _ in range(20):
        await asyncio.sleep(0.02)
    srv._ep.shutdown_flag = True
    loop_task.cancel()
    try:
        await asyncio.wait_for(loop_task, timeout=1.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    assert start_call_invocations == [], (
        "outbound call_answered must be skipped (session owned by outbound path)"
    )
    # Marker must be cleared on consumption.
    assert "outbound-1" not in srv._outbound_session_ids


# ─── LiveKit AudioStreamServer: single-phase session start ──────────────

@pytest.mark.asyncio
async def test_livekit_audio_stream_server_starts_on_call_answered():
    """Audio_stream no longer waits for first media — session starts
    immediately on call_answered (which fires on Plivo WS start).
    """
    from agent_transport.sip.livekit.audio_stream_server import AudioStreamServer

    srv = AudioStreamServer.__new__(AudioStreamServer)
    srv._ep = FakeEventEndpoint()
    srv._active_sessions = {}
    srv._session_ended_events = {}
    srv._session_contexts = {}
    srv._background_tasks = set()
    srv._server_listeners = {}

    started = []

    async def fake_start(sid, call_uuid, stream_id, extra):
        started.append((sid, call_uuid, stream_id, dict(extra)))

    srv._start_session = fake_start  # type: ignore

    srv._ep.push_event({
        "type": "call_answered",
        "session": _FakeSession("ws-1", remote_uri="call-uuid-xyz"),
    })

    loop_task = asyncio.create_task(srv._event_loop())
    for _ in range(50):
        await asyncio.sleep(0.02)
        if started:
            break

    srv._ep.shutdown_flag = True
    loop_task.cancel()
    try:
        await asyncio.wait_for(loop_task, timeout=1.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    assert len(started) == 1
    assert started[0][0] == "ws-1"
    assert started[0][1] == "call-uuid-xyz"
    assert started[0][2] == "stream-id-1"


# ─── Pipecat SIP: SipServerTransport.call() starts session directly ─────

@pytest.mark.asyncio
async def test_pipecat_sip_call_starts_session_directly():
    """SipServerTransport.call() must populate outbound_session_ids and
    call _start_session() directly — not rely on the "unknown = outbound"
    fallback that used to live in the event loop.
    """
    from agent_transport.sip.pipecat.transports.sip import SipServerTransport

    srv = SipServerTransport.__new__(SipServerTransport)
    srv._ep = FakeEventEndpoint()
    srv._active_sessions = {}
    srv._session_start_times = {}
    srv._session_event_queues = {}
    srv._background_tasks = set()
    srv._outbound_session_ids = set()
    srv._server_listeners = {}
    srv._handler_fnc = None
    srv._transport_params = None

    start_invocations = []

    def fake_start(session_id, session_data):
        start_invocations.append((session_id, dict(session_data)))

    srv._start_session = fake_start  # type: ignore

    result = await srv.call(
        "sip:+1234567890@phone.plivo.com",
        from_uri="sip:+0987654321@phone.plivo.com",
    )

    assert result == "outbound-session-1"
    # _start_session was called synchronously — no reliance on event loop
    assert len(start_invocations) == 1
    assert start_invocations[0][0] == "outbound-session-1"
    assert start_invocations[0][1]["direction"] == "Outbound"
    # The outbound id is reserved so the event loop will skip it
    assert "outbound-session-1" in srv._outbound_session_ids
    # Rust's ep.call() was invoked with the right args
    assert srv._ep.call_calls == [(
        "sip:+1234567890@phone.plivo.com",
        "sip:+0987654321@phone.plivo.com",
        None,
    )]


@pytest.mark.asyncio
async def test_pipecat_sip_event_loop_skips_outbound_call_answered():
    """Mirror of the LiveKit test: outbound sessions are owned by .call()
    so the event loop must not create duplicates.
    """
    from agent_transport.sip.pipecat.transports.sip import SipServerTransport

    srv = SipServerTransport.__new__(SipServerTransport)
    srv._ep = FakeEventEndpoint()
    srv._active_sessions = {}
    srv._session_start_times = {}
    srv._session_event_queues = {}
    srv._background_tasks = set()
    srv._outbound_session_ids = {"outbound-xyz"}
    srv._server_listeners = {}
    srv._handler_fnc = None
    srv._transport_params = None

    start_invocations = []

    def fake_start(sid, data):
        start_invocations.append((sid, data))

    srv._start_session = fake_start  # type: ignore

    srv._ep.push_event({
        "type": "call_answered",
        "session": _FakeSession("outbound-xyz", direction="Outbound"),
    })

    loop_task = asyncio.create_task(srv._event_loop())
    for _ in range(20):
        await asyncio.sleep(0.02)
    srv._ep.shutdown_flag = True
    loop_task.cancel()
    try:
        await asyncio.wait_for(loop_task, timeout=1.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    assert start_invocations == []
    assert "outbound-xyz" not in srv._outbound_session_ids


# ─── No-op: old "call_media_active" / "incoming_call" events are gone ───

@pytest.mark.asyncio
async def test_old_event_names_are_no_ops():
    """Guardrail: if the Rust core accidentally regresses and emits an
    old-style event name (e.g., due to a bad backport), the adapters
    should not crash — they should silently ignore the unknown type.
    """
    from agent_transport.sip.livekit.server import AgentServer

    srv = AgentServer.__new__(AgentServer)
    srv._ep = FakeEventEndpoint()
    srv._call_contexts = {}
    srv._call_ended_events = {}
    srv._active_calls = {}
    srv._background_tasks = set()
    srv._outbound_session_ids = set()
    srv._server_listeners = {}

    crashed = False
    try:
        # Old-name events — adapter should ignore (not crash, not start session)
        srv._ep.push_event({"type": "incoming_call", "session": _FakeSession("x")})
        srv._ep.push_event({"type": "call_media_active", "session_id": "x"})

        loop_task = asyncio.create_task(srv._sip_event_loop())
        for _ in range(15):
            await asyncio.sleep(0.02)
        srv._ep.shutdown_flag = True
        loop_task.cancel()
        try:
            await asyncio.wait_for(loop_task, timeout=1.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    except Exception:
        crashed = True

    assert not crashed
    assert srv._active_calls == {}
