"""Observability upload integration for agent-transport.

The Python LiveKit SDK owns the native observability protocol: protobuf
recording uploads, OTLP log records, Tagger outcomes, and JudgeGroup results.
agent-transport only adapts its SIP/audio_stream JobContext into the SDK's
SessionReport shape, attaches transport tags through the SDK Tagger, and then
delegates upload to LiveKit's telemetry helpers.

Set AGENT_OBSERVABILITY_URL plus LIVEKIT_API_KEY / LIVEKIT_API_SECRET to enable
native SDK uploads. The API key/secret are shared with agent-observability so it
can validate the SDK Bearer JWT locally.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("agent_transport.observability")

_sdk_upload_lock = None
_sdk_upload_lock_loop = None


def _get_sdk_upload_lock():
    import asyncio

    global _sdk_upload_lock, _sdk_upload_lock_loop
    loop = asyncio.get_running_loop()
    if _sdk_upload_lock is None or _sdk_upload_lock_loop is not loop:
        _sdk_upload_lock = asyncio.Lock()
        _sdk_upload_lock_loop = loop
    return _sdk_upload_lock


def _get_observability_url() -> str | None:
    return os.environ.get("AGENT_OBSERVABILITY_URL")


def _ensure_transport_tags(
    tagger: Any,
    *,
    account_id: str | None,
    transport: str | None,
    direction: str | None,
    agent_name: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    add = getattr(tagger, "add", None)
    if not callable(add):
        return

    session_metadata = {
        **(metadata or {}),
        "agent_name": agent_name,
        **({"account_id": account_id} if account_id else {}),
        **({"transport": transport} if transport else {}),
        **({"direction": direction} if direction else {}),
    }

    add("agent.session", metadata=session_metadata)
    add(f"agent.name:{agent_name}", metadata={"agent_name": agent_name})
    if account_id:
        add(f"account_id:{account_id}", metadata={"account_id": account_id})
    if transport:
        add(f"transport:{transport}", metadata={"transport": transport})
    if direction:
        add(f"direction:{direction}", metadata={"direction": direction})


def _get_sdk_tagger() -> Any:
    from livekit.agents.observability import Tagger

    return Tagger()


def _emit_runtime_events(session: Any, report: Any) -> None:
    """Ship the runtime event log the SDK leaves out of the wire format.

    `livekit.agents.telemetry.traces._upload_session_report` only emits OTLP
    records for chat_history.items, options/tags/usage, and tagger
    evaluations — it does NOT serialize ``session._recorded_events``, even
    though that list contains every state change, user transcription,
    function-tool execution, etc. that fired during the call.

    To get parity with the legacy multipart uploader (which built its own
    rich event log by subscribing to runtime callbacks), we walk
    ``session._recorded_events`` after the SDK upload and emit one extra
    OTLP record per non-conversation-item event. The obs server merges
    them into the session's events array via the ``"runtime event"`` body
    branch in ``persistLiveKitOtlpLogs``.
    """
    import time

    try:
        from opentelemetry._logs import SeverityNumber, get_logger_provider
    except Exception:
        return

    events = getattr(session, "_recorded_events", None) or []
    if not events:
        return

    rt_logger = get_logger_provider().get_logger(
        name="runtime_events",
        attributes={
            "room_id": report.room_id,
            "job_id": report.job_id,
            "room": report.room,
        },
    )

    for ev in events:
        ev_type = getattr(ev, "type", None)
        # The SDK already ships conversation_item_added items via "chat item"
        # records — re-emitting them here would duplicate every message,
        # tool call, and handoff in the dashboard.
        if not ev_type or ev_type == "conversation_item_added":
            continue
        try:
            payload = ev.model_dump(mode="json")
        except Exception:
            payload = {"type": ev_type}

        ts_seconds = payload.get("created_at") if isinstance(payload, dict) else None
        try:
            timestamp_ns = int(float(ts_seconds) * 1e9) if ts_seconds is not None else int(time.time() * 1e9)
        except (TypeError, ValueError):
            timestamp_ns = int(time.time() * 1e9)

        try:
            rt_logger.emit(
                body="runtime event",
                timestamp=timestamp_ns,
                attributes={"event.type": ev_type, "event": payload},
                severity_number=SeverityNumber.UNSPECIFIED,
                severity_text="unspecified",
            )
        except Exception:
            logger.warning("Failed to emit runtime event of type %s", ev_type, exc_info=True)


async def upload_session_report(
    session: Any,
    session_id: str,
    obs_url: str,
    agent_name: str,
    recording_path: str | None = None,
    recording_started_at: float | None = None,
    account_id: str | None = None,
    transport: str | None = None,
    direction: str | None = None,
    metadata: dict[str, Any] | None = None,
    tagger: Any = None,
    job_context: Any = None,
) -> None:
    """Upload the session through LiveKit's native SDK telemetry helpers."""
    if not os.environ.get("LIVEKIT_API_KEY") or not os.environ.get("LIVEKIT_API_SECRET"):
        raise RuntimeError(
            "LIVEKIT_API_KEY and LIVEKIT_API_SECRET are required for native "
            "LiveKit observability uploads"
        )

    import aiohttp
    from livekit.agents.telemetry.traces import (
        _setup_cloud_tracer,
        _shutdown_telemetry,
        _upload_session_report,
    )

    tagger = tagger or getattr(job_context, "tagger", None) or _get_sdk_tagger()
    _ensure_transport_tags(
        tagger,
        account_id=account_id,
        transport=transport,
        direction=direction,
        agent_name=agent_name,
        metadata=metadata,
    )

    make_report = getattr(job_context, "make_session_report", None)
    if not callable(make_report):
        raise RuntimeError("job_context with make_session_report is required for native upload")

    report = make_report(
        session,
        recording_path=recording_path,
        recording_started_at=recording_started_at,
        recording_options={
            "audio": bool(recording_path and os.path.exists(recording_path)),
            "traces": False,
            "logs": True,
            "transcript": True,
        },
    )

    logger.info("Uploading native LiveKit session report for %s to %s", session_id, obs_url)
    async with _get_sdk_upload_lock():
        _setup_cloud_tracer(
            room_id=report.room_id,
            job_id=report.job_id,
            observability_url=obs_url,
            enable_traces=False,
            enable_logs=True,
        )
        try:
            async with aiohttp.ClientSession() as http_session:
                await _upload_session_report(
                    agent_name=agent_name,
                    observability_url=obs_url,
                    report=report,
                    tagger=tagger,
                    http_session=http_session,
                )
                # Emit runtime events into the same OTLP pipeline so they
                # ride to obs alongside the SDK's records. _shutdown_telemetry
                # below flushes everything out.
                _emit_runtime_events(session, report)
        finally:
            _shutdown_telemetry()

    logger.info("Native LiveKit session report uploaded for %s", session_id)
