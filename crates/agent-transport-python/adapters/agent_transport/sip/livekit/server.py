"""AgentServer — drop-in equivalent of LiveKit's AgentServer for SIP transport.

Matches LiveKit's pattern:
    server = AgentServer()

    @server.sip_session()
    async def entrypoint(ctx: JobContext):
        session = AgentSession(vad=..., stt=..., llm=..., tts=...)
        await ctx.start(session, agent=Assistant())

    if __name__ == "__main__":
        run_app(server)

CLI commands (matching LiveKit):
    python agent.py start   — production mode (INFO logging)
    python agent.py dev     — development mode (DEBUG for adapters/pipeline)
    python agent.py debug   — full debug (including Rust SIP/RTP)
"""

import asyncio
import json
import logging
import os
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

import prometheus_client
from aiohttp import web

from agent_transport import SipEndpoint, init_logging
from livekit.agents.inference_runner import _InferenceRunner
from livekit.agents.utils.hw import get_cpu_monitor
from livekit.agents.utils import MovingAverage
from livekit.rtc.room import SipDTMF
from .sip_io import SipAudioInput, SipAudioOutput
from ._room_facade import TransportRoom, create_transport_context
from ._aio_utils import call_setup as _call_setup
from .observability import _get_observability_url

logger = logging.getLogger("agent_transport.server")


class JobProcess:
    """Stub matching LiveKit's JobProcess — holds prewarm data."""
    def __init__(self):
        self.userdata: dict[str, Any] = {}


_inference_ctx_token = None

def _set_inference_context(executor) -> None:
    """Temporarily make inference executor available via get_job_context().
    Used only during @setup() so MultilingualModel() works without explicit args."""
    global _inference_ctx_token
    from livekit.agents.job import _JobContextVar

    class _Stub:
        @property
        def inference_executor(self):
            return executor

    _inference_ctx_token = _JobContextVar.set(_Stub())


def _clear_inference_context() -> None:
    """Remove the temporary stub so AgentSession.start() gets RuntimeError (expected)."""
    global _inference_ctx_token
    if _inference_ctx_token is not None:
        from livekit.agents.job import _JobContextVar
        _JobContextVar.reset(_inference_ctx_token)
        _inference_ctx_token = None


def _create_inference_executor(loop: asyncio.AbstractEventLoop):
    """Create LiveKit's InferenceProcExecutor for local model inference.

    Uses the same subprocess-based executor as LiveKit's AgentServer.
    Models (e.g., turn detection ONNX) run in a separate process for isolation.
    """
    from livekit.agents.ipc.inference_proc_executor import InferenceProcExecutor
    import multiprocessing as mp

    runners = _InferenceRunner.registered_runners
    if not runners:
        return None

    executor = InferenceProcExecutor(
        runners=runners,
        initialize_timeout=5 * 60,
        close_timeout=5,
        memory_warn_mb=2000,
        memory_limit_mb=0,
        ping_interval=5,
        ping_timeout=60,
        high_ping_threshold=2.5,
        mp_ctx=mp.get_context("spawn"),
        loop=loop,
        http_proxy=None,
    )
    return executor

# ─── Prometheus metrics ───────────────────────────────────────────────────────
# Reuse LiveKit's existing gauges (already registered by telemetry/metrics.py)
# and add SIP-specific metrics.

from livekit.agents.telemetry.metrics import RUNNING_JOB_GAUGE, CPU_LOAD_GAUGE
from livekit.agents import utils as _lk_utils

SIP_CALLS_TOTAL = prometheus_client.Counter(
    "lk_agents_sip_calls_total",
    "Total SIP calls handled",
    ["nodename", "direction"],
)

SIP_CALL_DURATION = prometheus_client.Histogram(
    "lk_agents_sip_call_duration_seconds",
    "SIP call duration in seconds",
    ["nodename"],
    buckets=[1, 5, 10, 30, 60, 120, 300, 600],
)

def _nodename() -> str:
    return _lk_utils.nodename()


def _get_sdk_version() -> str:
    """Return livekit-agents version — same as what LiveKit reports in /worker."""
    try:
        from livekit.agents.version import __version__
        return __version__
    except ImportError:
        return "unknown"


class _LoadMonitor:
    """CPU load monitor — matches LiveKit's _DefaultLoadCalc exactly.

    Background thread samples cpu_percent every 0.5s, averaged over
    a moving window of 5 samples (2.5s).
    """

    def __init__(self) -> None:
        self._avg = MovingAverage(5)
        self._cpu_monitor = get_cpu_monitor()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample_loop, daemon=True)
        self._thread.start()

    def _sample_loop(self) -> None:
        # Cooperative shutdown via _stop event so test harnesses and rapid
        # start/stop cycles don't leak threads. The thread is daemon=True so
        # process exit is unblocked regardless, but a clean stop avoids the
        # 0.5s wasted-CPU window during shutdown.
        while not self._stop.is_set():
            cpu = self._cpu_monitor.cpu_percent(interval=0.5)
            with self._lock:
                self._avg.add_sample(cpu)

    def get_load(self) -> float:
        with self._lock:
            return self._avg.get_avg()

    def stop(self) -> None:
        """Signal the sampler thread to exit and join with a short timeout."""
        self._stop.set()
        # cpu_percent's 0.5s sleep is uninterruptible, so we wait at most
        # ~600ms for it to finish naturally.
        self._thread.join(timeout=1.0)


@dataclass
class JobContext:
    """Context passed to the @sip_session handler — equivalent of LiveKit's JobContext.

    Matches LiveKit's standard pattern exactly:
        @server.sip_session()
        async def entrypoint(ctx: JobContext):
            session = AgentSession(vad=..., stt=..., llm=..., tts=...)
            ctx.session = session
            await session.start(agent=Assistant(), room=ctx.room)

    Setting ctx.session automatically wires SIP audio I/O and registers
    the close handler. Then session.start(room=ctx.room) works exactly
    like LiveKit WebRTC.

    DTMF events (equivalent of room.on("sip_dtmf_received") in WebRTC):
        job_ctx = get_job_context()
        job_ctx.room.on("sip_dtmf_received", handler)
    """

    session_id: str
    remote_uri: str
    direction: str  # "inbound" or "outbound"
    endpoint: SipEndpoint
    userdata: dict[str, Any] = field(default_factory=dict)
    extra_headers: dict[str, str] = field(default_factory=dict)
    account_id: str | None = None
    """Account ID for multi-tenancy — set by the consumer per session."""

    _agent_name: str = field(default="sip-agent", repr=False)
    _session: Any = field(default=None, repr=False)
    _call_ended: asyncio.Event | None = field(default=None, repr=False)
    _room: Any = field(default=None, repr=False)
    _job_ctx_token: Any = field(default=None, repr=False)
    _event_listeners: dict = field(default_factory=dict, repr=False)
    _proc: Any = field(default=None, repr=False)
    _shutdown_callbacks: list = field(default_factory=list, repr=False)

    @property
    def session(self):
        return self._session

    @session.setter
    def session(self, session: Any) -> None:
        """Set the agent session — automatically wires SIP audio I/O.

        This replaces the manual ctx.start() pattern. After setting ctx.session,
        call session.start(agent=, room=ctx.room) directly.
        """
        self._session = session

        # Wire SIP audio I/O before session.start() is called
        session.input.audio = SipAudioInput(self.endpoint, self.session_id)
        session.output.audio = SipAudioOutput(self.endpoint, self.session_id)

        # Listen to session close event — handles agent-initiated shutdown
        @session.on("close")
        def _on_session_close(ev):
            logger.info("Call %s session closed (reason=%s)", self.session_id, getattr(ev, 'reason', 'unknown'))
            if self._call_ended is not None and not self._call_ended.is_set():
                self._call_ended.set()
            try:
                self.endpoint.hangup(self.session_id)
            except Exception:
                pass

        if logging.getLogger("agent_transport.sip").isEnabledFor(logging.DEBUG):
            @session.on("agent_state_changed")
            def _on_agent_state(ev):
                logger.info("Call %s agent: %s -> %s", self.session_id, ev.old_state, ev.new_state)
            @session.on("user_state_changed")
            def _on_user_state(ev):
                logger.info("Call %s user: %s -> %s", self.session_id, ev.old_state, ev.new_state)

    def on(self, event_name: str, callback: Callable | None = None) -> Callable:
        """Register an event listener. Can be used as a decorator."""
        def decorator(fn):
            self._event_listeners.setdefault(event_name, []).append(fn)
            return fn
        if callback is not None:
            return decorator(callback)
        return decorator

    def _emit(self, event_name: str, *args, **kwargs) -> None:
        for listener in self._event_listeners.get(event_name, []):
            try:
                listener(*args, **kwargs)
            except Exception:
                logger.exception("Error in %s listener", event_name)

    @property
    def room(self):
        """Room facade — use with session.start(room=ctx.room) like LiveKit WebRTC."""
        return self._room

    @property
    def proc(self):
        """Process context — access prewarm data via ctx.proc.userdata."""
        return self._proc

    def add_shutdown_callback(self, callback):
        """Register a callback to run when the session ends."""
        self._shutdown_callbacks.append(callback)


class AgentServer:
    """SIP voice agent server — handles inbound and outbound calls.

    Equivalent of LiveKit's AgentServer.
    """

    def __init__(
        self,
        *,
        sip_server: str | None = None,
        sip_port: int | None = None,
        sip_username: str | None = None,
        sip_password: str | None = None,
        host: str = "0.0.0.0",
        port: int | None = None,
        agent_name: str = "sip-agent",
        auth: Callable[..., bool | Coroutine] | None = None,
        recording: bool = True,
        recording_dir: str = "/tmp/agent-sessions",
        recording_stereo: bool = True,
    ) -> None:
        self._sip_server = sip_server or os.environ.get("SIP_DOMAIN", "phone.plivo.com")
        self._sip_port = sip_port or int(os.environ.get("SIP_PORT", "5060"))
        self._sip_username = sip_username or os.environ.get("SIP_USERNAME", "")
        self._sip_password = sip_password or os.environ.get("SIP_PASSWORD", "")
        self._host = host
        self._port = port or int(os.environ.get("PORT", "8080"))
        self._agent_name = agent_name
        self._auth = auth
        self._recording = recording
        self._recording_dir = recording_dir
        self._recording_stereo = recording_stereo
        self._entrypoint_fnc: Callable[..., Coroutine] | None = None
        self._setup_fnc: Callable | None = None
        self._proc = JobProcess()
        self._userdata: dict[str, Any] = {}
        self._ep: SipEndpoint | None = None
        # Session IDs are strings (returned by Rust CallSession.session_id).
        # The type hints used `int` before — purely cosmetic since Python dict
        # keys are duck-typed, but fix them so mypy/pyright don't scream.
        self._active_calls: dict[str, asyncio.Task] = {}
        self._call_ended_events: dict[str, asyncio.Event] = {}
        self._call_contexts: dict[str, JobContext] = {}
        # Strong-reference set for fire-and-forget asyncio tasks (outbound
        # dial wrappers, inbound _start_call dispatch). Python's event loop
        # only holds weak references to tasks; without storing them here,
        # the GC can collect a task mid-execution and emit
        # "Task was destroyed but it is pending!" warnings.
        self._background_tasks: set[asyncio.Task] = set()
        # Outbound sessions whose `_start_call` task has been scheduled but
        # may not yet be visible in `_active_calls`. The `call_answered`
        # event handler checks this set to avoid racing against an outbound
        # session that `_start_call` will create asynchronously.
        self._outbound_session_ids: set[str] = set()
        self._load_monitor = _LoadMonitor()
        # Server-level event listeners (distinct from per-JobContext events
        # which only exist after session creation). Used for pre-answer
        # hooks like `ringing` that fire before the JobContext exists.
        self._server_listeners: dict[str, list[Callable]] = {}

    @property
    def setup_fnc(self):
        return self._setup_fnc

    @setup_fnc.setter
    def setup_fnc(self, fn):
        """Set prewarm function — fn(proc: JobProcess). Matches LiveKit's server.setup_fnc = prewarm."""
        self._setup_fnc = fn

    def setup(self) -> Callable:
        """Decorator to register a setup function that runs once at startup.

        The function should return a dict of shared resources (VAD, turn detector, etc.)
        that will be available via ctx.userdata in each call.

        Example::
            @server.setup()
            def prewarm():
                return {"vad": silero.VAD.load(), "turn_detector": MultilingualModel()}
        """
        def decorator(fn: Callable) -> Callable:
            self._setup_fnc = fn
            return fn
        return decorator

    def sip_session(self) -> Callable:
        """Decorator to register the call handler — equivalent of @server.rtc_session()."""
        def decorator(fn: Callable[..., Coroutine]) -> Callable:
            self._entrypoint_fnc = fn
            return fn
        return decorator

    def on(self, event_name: str, callback: Callable | None = None) -> Callable:
        """Register a server-level event listener.

        Server events fire before a per-call :class:`JobContext` exists, so
        they can't be attached to ``ctx``. Use these for pre-answer hooks
        like call screening, logging, and metrics.

        Available events:

        - ``"ringing"`` — fires on inbound SIP calls immediately after Rust
          has sent ``180 Ringing``. Handler receives a ``CallSession`` with
          ``session_id``, ``remote_uri``, ``call_uuid``, and
          ``extra_headers``. Rust auto-answers right after this event, so
          the hook is currently observational only — return values are
          ignored. Future enhancement: allow handlers to return
          ``False``/raise to reject the call before auto-answer runs.

        Handlers may be ``async def`` or plain functions. Async handlers
        are scheduled as background tasks.

        Usage::

            @server.on("ringing")
            def on_ringing(session):
                logger.info("Incoming call from %s", session.remote_uri)

            @server.on("ringing")
            async def log_to_db(session):
                await db.insert_call_record(session.call_uuid)
        """
        def decorator(fn: Callable) -> Callable:
            self._server_listeners.setdefault(event_name, []).append(fn)
            return fn
        if callback is not None:
            return decorator(callback)
        return decorator

    def _emit_server_event(self, event_name: str, *args, **kwargs) -> None:
        """Fire a server-level event to all registered listeners."""
        listeners = self._server_listeners.get(event_name, [])
        for listener in listeners:
            try:
                result = listener(*args, **kwargs)
                if asyncio.iscoroutine(result):
                    task = asyncio.create_task(result)
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)
            except Exception:
                logger.exception("Server event listener for %r failed", event_name)

    def run(self, port: int | None = None) -> None:
        """Build CLI and run — equivalent of cli.run_app(server)."""
        if port is not None:
            self._port = port

        try:
            import typer
            from typing import Annotated
        except ImportError:
            asyncio.run(self._run(log_mode="start"))
            return

        app = typer.Typer()

        @app.command()
        def start(
            port: Annotated[int | None, typer.Option(help="HTTP server port", envvar="PORT")] = None,
        ) -> None:
            """Run in production mode (INFO logging)."""
            if port is not None:
                self._port = port
            asyncio.run(self._run(log_mode="start"))

        @app.command()
        def dev(
            port: Annotated[int | None, typer.Option(help="HTTP server port", envvar="PORT")] = None,
        ) -> None:
            """Run in development mode (DEBUG for adapters/pipeline, INFO for Rust)."""
            if port is not None:
                self._port = port
            asyncio.run(self._run(log_mode="dev"))

        @app.command()
        def debug(
            port: Annotated[int | None, typer.Option(help="HTTP server port", envvar="PORT")] = None,
        ) -> None:
            """Run in debug mode (DEBUG everything including Rust SIP/RTP)."""
            if port is not None:
                self._port = port
            asyncio.run(self._run(log_mode="debug"))

        @app.command(name="download-files")
        def download_files() -> None:
            """Download model files for plugins (turn detection, VAD, etc.)."""
            import logging as _logging
            from livekit.agents import Plugin

            _logging.basicConfig(level=_logging.DEBUG)
            for plugin in Plugin.registered_plugins:
                logger.info("Downloading files for %s", plugin.package)
                plugin.download_files()
                logger.info("Finished downloading files for %s", plugin.package)

        app()

    async def _run(self, *, log_mode: str = "start") -> None:
        self._configure_logging(log_mode)

        if not self._sip_username or not self._sip_password:
            logger.error("Set SIP_USERNAME and SIP_PASSWORD environment variables")
            sys.exit(1)

        if self._entrypoint_fnc is None:
            logger.error(
                "No SIP session entrypoint registered.\n"
                "Define one using the @server.sip_session() decorator, for example:\n"
                '    @server.sip_session()\n'
                "    async def entrypoint(ctx: JobContext):\n"
                "        ..."
            )
            sys.exit(1)

        loop = asyncio.get_running_loop()

        # Initialize inference executor for local model inference (turn detection, etc.)
        # Same subprocess approach as LiveKit's AgentServer.
        # We set it on the job context var so MultilingualModel() works transparently —
        # users write the same code as they would with LiveKit's WebRTC transport.
        self._inference_executor = _create_inference_executor(loop)
        if self._inference_executor:
            await self._inference_executor.start()
            await self._inference_executor.initialize()
            logger.info("Inference executor ready (turn detection models available)")

        # Run user's setup function to prewarm models.
        # Temporarily set inference executor on job context so MultilingualModel()
        # works without explicit args — cleared before AgentSession runs.
        if self._setup_fnc:
            if self._inference_executor:
                _set_inference_context(self._inference_executor)
            try:
                await _call_setup(self._setup_fnc, self._proc)
            except Exception:
                logger.exception("Setup function failed")
                if self._inference_executor:
                    _clear_inference_context()
                raise
            if self._inference_executor:
                _clear_inference_context()
            self._userdata = self._proc.userdata
            logger.info("Setup complete: %s", list(self._userdata.keys()))

        self._ep = SipEndpoint(sip_server=self._sip_server)

        # Register with SIP provider
        await loop.run_in_executor(
            None, self._ep.register, self._sip_username, self._sip_password
        )
        ev = await loop.run_in_executor(None, self._ep.wait_for_event, 10000)
        if not ev or ev["type"] != "registered":
            logger.error("SIP registration failed: %s", ev)
            sys.exit(1)

        logger.info("Registered as %s@%s:%d", self._sip_username, self._sip_server, self._sip_port)

        obs_url = _get_observability_url()
        if obs_url:
            logger.info("Observability enabled, target %s", obs_url)

        # Start HTTP server
        http_app = self._build_http_app()
        runner = web.AppRunner(http_app)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port, reuse_address=True)
        await site.start()
        logger.info("HTTP server on http://%s:%d", self._host, self._port)

        # Start SIP event loop
        event_task = asyncio.create_task(self._sip_event_loop())

        # Wait for shutdown signal
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        await stop.wait()
        logger.info("Shutting down...")
        event_task.cancel()

        if self._active_calls:
            logger.info("Draining %d active call(s)...", len(self._active_calls))
            await asyncio.gather(*self._active_calls.values(), return_exceptions=True)

        await runner.cleanup()
        if self._inference_executor:
            await self._inference_executor.aclose()
        # Stop background threads cleanly before exiting.
        self._load_monitor.stop()
        # ep.shutdown() does block_on for unregister + per-call hangup. Wrap
        # in run_in_executor so the asyncio loop isn't blocked for the
        # ~200ms it takes to talk to the proxy.
        await loop.run_in_executor(None, self._ep.shutdown)

    def _configure_logging(self, mode: str) -> None:
        if mode == "debug":
            logging.basicConfig(
                level=logging.DEBUG,
                format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s %(message)s",
                datefmt="%H:%M:%S",
                force=True,
            )
            init_logging(os.environ.get("RUST_LOG", "debug"))
        elif mode == "dev":
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s %(message)s",
                datefmt="%H:%M:%S",
                force=True,
            )
            logging.getLogger("agent_transport.sip").setLevel(logging.DEBUG)
            logging.getLogger("livekit.agents").setLevel(logging.DEBUG)
            logging.getLogger("livekit.plugins").setLevel(logging.DEBUG)
            init_logging(os.environ.get("RUST_LOG", "info"))
        else:
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s %(levelname)s %(name)s %(message)s",
                force=True,
            )
            init_logging(os.environ.get("RUST_LOG", "info"))

    def _build_http_app(self) -> web.Application:
        app = web.Application()
        app.add_routes([
            web.get("/", self._health_handler),
            web.get("/worker", self._worker_handler),
            web.get("/metrics", self._metrics_handler),
            web.post("/call", self._call_handler),
        ])
        return app

    async def _check_auth(self, request: web.Request) -> web.Response | None:
        """Returns 401 if auth fails. None if OK or no auth configured."""
        if self._auth is None:
            return None
        result = self._auth(request)
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return None
        return web.json_response({"error": "unauthorized"}, status=401)

    async def _metrics_handler(self, request: web.Request) -> web.Response:
        """Prometheus metrics endpoint — matches LiveKit's /metrics exactly."""
        if err := await self._check_auth(request):
            return err
        loop = asyncio.get_running_loop()
        # Update gauges before scrape
        node = _nodename()
        CPU_LOAD_GAUGE.labels(nodename=node).set(self._load_monitor.get_load())
        RUNNING_JOB_GAUGE.labels(nodename=node).set(len(self._active_calls))

        data = await loop.run_in_executor(None, prometheus_client.generate_latest)
        return web.Response(
            body=data,
            headers={
                "Content-Type": prometheus_client.CONTENT_TYPE_LATEST,
                "Content-Length": str(len(data)),
            },
        )

    async def _health_handler(self, request: web.Request) -> web.Response:
        if not self._ep:
            return web.Response(status=503, text="SIP endpoint not initialized")
        return web.Response(text="OK")

    async def _worker_handler(self, request: web.Request) -> web.Response:
        if err := await self._check_auth(request):
            return err
        return web.json_response({
            "agent_name": self._agent_name,
            "worker_type": "JT_SIP",
            "worker_load": self._load_monitor.get_load(),
            "active_jobs": len(self._active_calls),
            "sdk_version": _get_sdk_version(),
            "project_type": "python",
            "sip_server": self._sip_server,
            "sip_port": self._sip_port,
        })

    async def _call_handler(self, request: web.Request) -> web.Response:
        if err := await self._check_auth(request):
            return err
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        raw_to = data.get("to", "")
        if not raw_to:
            return web.json_response({"error": "missing 'to' field"}, status=400)

        # Normalize destination for SIP: add sip: prefix and @domain if missing
        destination = raw_to
        if not destination.startswith("sip:"):
            destination = "sip:" + destination
        if "@" not in destination.split(":", 1)[1]:
            destination = destination + "@" + self._sip_server

        from_uri = data.get("from")  # Optional SIP From URI
        raw_from = from_uri or ""
        headers = data.get("headers")  # Optional custom SIP headers
        wait = data.get("wait_until_answered", False)

        loop = asyncio.get_running_loop()

        if wait:
            # Blocking mode: wait for call to connect, then return
            try:
                session_id = await loop.run_in_executor(
                    None, lambda: self._ep.call(destination, from_uri, headers)
                )
            except Exception as e:
                return web.json_response({"error": str(e)}, status=500)

            logger.info("Outbound call %s to %s connected (from=%s)", session_id, destination, from_uri or "default")
            # Mark synchronously so the event loop doesn't race-create a
            # duplicate session when `call_answered` arrives.
            self._outbound_session_ids.add(session_id)
            t = asyncio.create_task(self._start_call(session_id, destination, direction="outbound"))
            self._background_tasks.add(t)
            t.add_done_callback(self._background_tasks.discard)
            return web.json_response({
                "session_id": session_id, "status": "connected",
                "to": raw_to, "from": raw_from,
            })
        else:
            # Non-blocking (default): generate session_id upfront, dial in background
            session_id = "c" + uuid.uuid4().hex[:16]
            # Reserve the session id up-front so call_answered knows it's
            # an outbound call we're driving.
            self._outbound_session_ids.add(session_id)

            async def _dial():
                try:
                    returned_id = await loop.run_in_executor(
                        None, lambda: self._ep.call(destination, from_uri, headers, session_id)
                    )
                    logger.info("Outbound call %s to %s connected (from=%s)", returned_id, destination, from_uri or "default")
                    await self._start_call(returned_id, destination, direction="outbound")
                except Exception as e:
                    logger.warning("Outbound call %s to %s failed: %s", session_id, destination, e)
                    self._outbound_session_ids.discard(session_id)

            t = asyncio.create_task(_dial())
            self._background_tasks.add(t)
            t.add_done_callback(self._background_tasks.discard)
            return web.json_response({
                "session_id": session_id, "status": "dialing",
                "to": raw_to, "from": raw_from,
            })

    async def _sip_event_loop(self) -> None:
        """Single event dispatcher — reads all SIP events and routes them.

        Avoids multiple consumers racing on wait_for_event.
        """
        loop = asyncio.get_running_loop()

        while True:
            try:
                ev = await loop.run_in_executor(None, self._ep.wait_for_event, 1000)
            except Exception:
                logger.exception("sip wait_for_event failed")
                break

            if not ev:
                continue

            try:
                ev_type = ev["type"]

                if ev_type == "shutdown":
                    # Sentinel pushed by ep.shutdown() so this loop wakes
                    # immediately instead of waiting for the next 1s poll
                    # timeout. Exit cleanly.
                    logger.debug("sip event loop received shutdown sentinel")
                    break

                if ev_type == "call_ringing":
                    # Pre-answer hook. Rust has sent 180 Ringing and will
                    # auto-answer immediately after this event. We don't
                    # create the session yet — that happens on call_answered.
                    # This is purely observational: fire server-level
                    # "ringing" listeners for logging / screening.
                    session = ev["session"]
                    logger.info(
                        "Incoming call %s ringing (from=%s, call_uuid=%s)",
                        session.session_id, session.remote_uri, session.call_uuid,
                    )
                    self._emit_server_event("ringing", session)

                elif ev_type == "call_answered":
                    # Call is answered and media is active — safe to create
                    # the agent session. Fires for both inbound and outbound.
                    # Outbound sessions are reserved synchronously by the
                    # HTTP outbound path (via _outbound_session_ids), so we
                    # skip the event for them.
                    session = ev["session"]
                    session_id = session.session_id
                    if session_id in self._outbound_session_ids:
                        # Outbound path owns session creation. Clear the
                        # marker so a hypothetical session_id reuse after
                        # this call ends doesn't keep matching.
                        self._outbound_session_ids.discard(session_id)
                        continue
                    if session_id in self._active_calls:
                        # Defensive: session already being driven (e.g.,
                        # duplicate event, retry). Ignore.
                        continue
                    t = asyncio.create_task(
                        self._start_call(session_id, session.remote_uri, direction="inbound")
                    )
                    self._background_tasks.add(t)
                    t.add_done_callback(self._background_tasks.discard)

                elif ev_type == "call_terminated":
                    session_id = ev["session"].session_id
                    reason = ev.get("reason", "unknown")
                    logger.info("Call %s terminated (reason=%s)", session_id, reason)

                    # Clear audio buffer to abort pending playout — but skip when
                    # recording so the RTP send loop can drain remaining audio
                    # into the recorder before shutdown.
                    if not self._recording:
                        try:
                            self._ep.clear_buffer(session_id)
                        except Exception:
                            pass

                    # Shut down the session gracefully BEFORE emitting
                    # participant_disconnected. LiveKit's default handler calls
                    # `_close_soon(drain=False)`, which force-interrupts any
                    # in-flight LLM/TTS response — the final assistant message
                    # (e.g. the reply after a tool call) would never land in
                    # chat_history. Calling `shutdown(drain=True)` first sets
                    # `_closing_task` so the subsequent `_close_soon` becomes a
                    # no-op and the session drains normally, letting in-flight
                    # speech finalize into history.
                    ctx = self._call_contexts.get(session_id)
                    if ctx and ctx._session is not None:
                        try:
                            ctx._session.shutdown(drain=True)
                        except Exception:
                            logger.warning(
                                "Graceful session shutdown failed; falling back "
                                "to default close",
                                exc_info=True,
                            )

                    # Emit participant_disconnected on Room facade (matches
                    # LiveKit WebRTC). RoomIO._on_participant_disconnected calls
                    # `_close_soon(drain=False)`, which is a no-op here because
                    # `_closing_task` was already set above.
                    if ctx and ctx._room:
                        remote = ctx._room._remote
                        remote.disconnect_reason = 1  # CLIENT_INITIATED
                        ctx._room.emit("participant_disconnected", remote)

                    # Signal active call to end
                    if session_id in self._call_ended_events:
                        self._call_ended_events[session_id].set()

                elif ev_type == "dtmf_received":
                    # Session ID is a string at every other call site; the old
                    # `-1` default would produce a lookup miss and silently drop
                    # a malformed DTMF event. Use None so the guard below logs
                    # and skips explicitly.
                    session_id = ev.get("session_id")
                    digit = ev.get("digit", "")
                    if not session_id:
                        logger.warning("DTMF event missing session_id, dropping: %r", ev)
                        continue
                    logger.debug("DTMF '%s' on call %s", digit, session_id)
                    ctx = self._call_contexts.get(session_id)
                    if ctx:
                        ctx._emit("dtmf_received", digit)
                        if ctx._room:
                            dtmf_ev = SipDTMF(code=ord(digit) if digit else 0, digit=digit,
                                              participant=ctx._room._remote)
                            ctx._room.emit("sip_dtmf_received", dtmf_ev)

                elif ev_type == "beep_detected":
                    session_id = ev.get("session_id")
                    if not session_id:
                        logger.warning("beep_detected event missing session_id, dropping: %r", ev)
                        continue
                    freq = ev.get("frequency_hz", 0.0)
                    dur = ev.get("duration_ms", 0)
                    logger.info("Beep detected on call %s (freq=%.0fHz, dur=%dms)", session_id, freq, dur)
                    ctx = self._call_contexts.get(session_id)
                    if ctx:
                        ctx._emit("beep_detected", freq, dur)
                        if ctx._room:
                            ctx._room.emit("beep_detected", {"frequency_hz": freq, "duration_ms": dur})

                elif ev_type == "beep_timeout":
                    session_id = ev.get("session_id")
                    if not session_id:
                        logger.warning("beep_timeout event missing session_id, dropping: %r", ev)
                        continue
                    logger.debug("Beep timeout on call %s", session_id)
                    ctx = self._call_contexts.get(session_id)
                    if ctx:
                        ctx._emit("beep_timeout")
                        if ctx._room:
                            ctx._room.emit("beep_timeout", {})
            except Exception:
                logger.exception("Error handling sip event %r", ev.get("type") if isinstance(ev, dict) else ev)

    async def _upload_report(
        self, session, session_id: str, obs_url: str,
        recording_path: str | None = None, recording_started_at: float | None = None,
        account_id: str | None = None,
    ) -> None:
        from .observability import upload_session_report
        await upload_session_report(
            session, session_id, obs_url, self._agent_name,
            recording_path, recording_started_at, account_id,
            transport="sip",
        )

    async def _start_call(self, session_id: str, remote_uri: str, direction: str) -> None:
        call_ended = asyncio.Event()
        self._call_ended_events[session_id] = call_ended

        # Create Room facade BEFORE handler runs
        room = TransportRoom(
            self._ep, session_id,
            agent_name=self._agent_name,
            caller_identity=remote_uri,
        )
        job_stub, job_ctx_token = create_transport_context(
            room, agent_name=self._agent_name)

        ctx = JobContext(
            session_id=session_id,
            remote_uri=remote_uri,
            direction=direction,
            endpoint=self._ep,
            userdata=self._userdata,
            _agent_name=self._agent_name,
            _call_ended=call_ended,
            _room=room,
            _job_ctx_token=job_ctx_token,
            _proc=self._proc,
        )
        self._call_contexts[session_id] = ctx

        async def _run_call():
            node = _nodename()
            SIP_CALLS_TOTAL.labels(nodename=node, direction=direction).inc()
            call_start = time.monotonic()

            # Start recording if enabled — only when observability is configured,
            # since the recording's only purpose is to be uploaded as part of the
            # session report. Without observability there's nowhere to send it
            # and nothing would clean up the file.
            rec_path = None
            rec_started_at = None
            if self._recording and _get_observability_url():
                try:
                    os.makedirs(self._recording_dir, exist_ok=True)
                    rec_path = os.path.join(self._recording_dir, f"recording_{session_id}.ogg")
                    self._ep.start_recording(session_id, rec_path, self._recording_stereo)
                    rec_started_at = time.time()
                except Exception:
                    rec_path = None
                    logger.warning("Failed to start recording for call %s", session_id, exc_info=True)

            try:
                await self._entrypoint_fnc(ctx)
                # Entrypoint returned — session.start() is non-blocking,
                # so wait for call to actually end (BYE or agent shutdown)
                if call_ended and not call_ended.is_set():
                    await call_ended.wait()
            except Exception:
                logger.exception("Call %s handler failed", session_id)
            finally:
                SIP_CALL_DURATION.labels(nodename=node).observe(time.monotonic() - call_start)
                logger.info("Call %s cleanup: session=%s", session_id, "set" if ctx._session is not None else "None")

                if ctx._session is not None:
                    try:
                        usage = ctx._session.usage
                        if usage and usage.model_usage:
                            logger.info("Call %s usage: %s", session_id, usage)
                    except Exception:
                        pass

                    # Stop recording and wait for file to be finalized
                    if rec_path:
                        try:
                            self._ep.stop_recording(session_id)
                            # Rust recorder finalizes on a background thread;
                            # poll until the file appears (up to 2s)
                            for _ in range(20):
                                if os.path.exists(rec_path):
                                    break
                                await asyncio.sleep(0.1)
                        except Exception:
                            pass

                    # Wait for session close (triggered by participant_disconnected
                    # → _close_soon) rather than calling aclose() which cancels
                    # in-progress LLM/TTS and discards the response from history.
                    # The session's close event was wired in the session setter to
                    # set call_ended — by the time we're here, it may already be
                    # closing. Give it a few seconds to finalize in-flight responses.
                    try:
                        close_event = asyncio.Event()
                        ctx._session.on("close", lambda *_: close_event.set())
                        try:
                            await asyncio.wait_for(close_event.wait(), timeout=5.0)
                        except asyncio.TimeoutError:
                            # Force close if graceful close didn't happen
                            await ctx._session.aclose()
                    except Exception:
                        try:
                            await ctx._session.aclose()
                        except Exception:
                            pass

                    # Upload session report after session close so history is complete.
                    # Cleanup runs in finally so the on-disk recording is always removed
                    # after an upload attempt — including when the upload fails.
                    obs_url = _get_observability_url()
                    try:
                        if obs_url:
                            try:
                                await self._upload_report(
                                    ctx._session, session_id, obs_url, rec_path, rec_started_at,
                                    account_id=ctx.account_id,
                                )
                            except Exception:
                                logger.warning("Failed to upload session report for call %s", session_id, exc_info=True)
                    finally:
                        if rec_path:
                            try:
                                os.remove(rec_path)
                            except Exception:
                                logger.warning("Failed to clean up recording %s", rec_path, exc_info=True)

                for cb in ctx._shutdown_callbacks:
                    try:
                        result = cb()
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        logger.exception("Shutdown callback failed")
                try:
                    self._ep.hangup(session_id)
                except Exception:
                    pass
                # Cleanup Room facade and JobContext
                room._on_session_ended()
                from livekit.agents.job import _JobContextVar
                try:
                    _JobContextVar.reset(job_ctx_token)
                except ValueError:
                    pass
                self._active_calls.pop(session_id, None)
                self._call_ended_events.pop(session_id, None)
                self._call_contexts.pop(session_id, None)
                logger.info("Call %s ended (%s)", session_id, direction)

        task = asyncio.create_task(_run_call())
        self._active_calls[session_id] = task


def run_app(server: AgentServer) -> None:
    """Run the agent server — equivalent of livekit.agents.cli.run_app(server)."""
    try:
        import typer
    except ImportError:
        asyncio.run(server._run(log_mode="start"))
        return

    app = typer.Typer()

    @app.command()
    def start() -> None:
        """Run in production mode (INFO logging)."""
        asyncio.run(server._run(log_mode="start"))

    @app.command()
    def dev() -> None:
        """Run in development mode (DEBUG for adapters/pipeline, INFO for Rust)."""
        asyncio.run(server._run(log_mode="dev"))

    @app.command()
    def debug() -> None:
        """Run in debug mode (DEBUG everything including Rust SIP/RTP)."""
        asyncio.run(server._run(log_mode="debug"))

    @app.command(name="download-files")
    def download_files() -> None:
        """Download model files for plugins (turn detection, VAD, etc.)."""
        import logging as _logging
        from livekit.agents import Plugin

        _logging.basicConfig(level=_logging.DEBUG)
        for plugin in Plugin.registered_plugins:
            logger.info("Downloading files for %s", plugin.package)
            plugin.download_files()
            logger.info("Finished downloading files for %s", plugin.package)

    app()
