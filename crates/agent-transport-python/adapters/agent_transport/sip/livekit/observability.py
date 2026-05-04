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
        finally:
            _shutdown_telemetry()

    logger.info("Native LiveKit session report uploaded for %s", session_id)
