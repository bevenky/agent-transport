"""Observability setup for agent-transport.

Delegates to livekit-agents' _setup_cloud_tracer() so we get the same
traces, logs, and recording support as standard LiveKit agents.

Set LIVEKIT_OBSERVABILITY_URL to enable.
"""

import logging
import os

logger = logging.getLogger("agent_transport.observability")

_initialized = False


def _get_observability_url() -> str | None:
    return os.environ.get("LIVEKIT_OBSERVABILITY_URL")


def setup_observability(*, call_id: int | str = "server") -> bool:
    """Set up OTel trace and log export via LiveKit's built-in telemetry.

    Returns True if observability was configured, False otherwise.
    """
    global _initialized
    if _initialized:
        return True

    obs_url = _get_observability_url()
    if not obs_url:
        return False

    try:
        from livekit.agents.telemetry.traces import _setup_cloud_tracer
    except ImportError:
        logger.warning(
            "livekit-agents telemetry not available — observability disabled."
        )
        return False

    _setup_cloud_tracer(
        room_id=str(call_id),
        job_id=str(call_id),
        observability_url=obs_url,
    )

    _initialized = True
    logger.info("Observability configured → %s", obs_url)
    return True


def shutdown_observability() -> None:
    """Flush and shut down OTel exporters."""
    global _initialized
    if not _initialized:
        return

    try:
        from livekit.agents.telemetry.traces import _shutdown_telemetry
        _shutdown_telemetry()
    except Exception:
        logger.debug("telemetry shutdown error", exc_info=True)

    _initialized = False


async def upload_session_report(
    session,
    session_id: str,
    obs_url: str,
    agent_name: str,
    recording_path: str | None = None,
    recording_started_at: float | None = None,
    account_id: str | None = None,
) -> None:
    """Build a SessionReport and upload it to the observability server.

    Custom upload because livekit-agents' _upload_session_report doesn't
    support room_tags on MetricsRecordingHeader. We pass account_id
    (plivo_auth_id) via room_tags for account filtering.

    Used by both AgentServer (SIP) and AudioStreamServer.
    """
    import json
    import aiohttp
    from datetime import timedelta
    from pathlib import Path
    from livekit import api
    from livekit.protocol import metrics as proto_metrics
    from livekit.agents.voice.report import SessionReport
    from livekit.agents.utils import http_context

    has_audio = recording_path and os.path.exists(recording_path)
    if recording_path:
        logger.info("Recording path=%s exists=%s size=%s", recording_path, os.path.exists(recording_path), os.path.getsize(recording_path) if os.path.exists(recording_path) else "N/A")
    report = SessionReport(
        recording_options={"audio": bool(has_audio), "traces": True, "logs": True, "transcript": True},
        job_id=str(session_id),
        room_id=str(session_id),
        room=str(session_id),
        options=session.options,
        events=session._recorded_events,
        chat_history=session.history.copy(),
        audio_recording_path=Path(recording_path) if has_audio else None,
        audio_recording_started_at=recording_started_at if has_audio else None,
        started_at=session._started_at,
        model_usage=session.usage.model_usage if session.usage else None,
    )

    room_tags = {}
    if account_id:
        room_tags["account_id"] = account_id

    header_msg = proto_metrics.MetricsRecordingHeader(
        room_id=report.room_id,
        room_tags=room_tags,
    )
    header_msg.start_time.FromMilliseconds(int((report.audio_recording_started_at or 0) * 1000))
    header_bytes = header_msg.SerializeToString()

    access_token = (
        api.AccessToken()
        .with_observability_grants(api.ObservabilityGrants(write=True))
        .with_ttl(timedelta(hours=6))
    )
    jwt = access_token.to_jwt()

    mp = aiohttp.MultipartWriter("form-data")

    part = mp.append(header_bytes)
    part.set_content_disposition("form-data", name="header", filename="header.binpb")
    part.headers["Content-Type"] = "application/protobuf"
    part.headers["Content-Length"] = str(len(header_bytes))

    chat_history_json = json.dumps(report.to_dict())
    part = mp.append(chat_history_json)
    part.set_content_disposition("form-data", name="chat_history", filename="chat_history.json")
    part.headers["Content-Type"] = "application/json"
    part.headers["Content-Length"] = str(len(chat_history_json))

    if has_audio and report.audio_recording_path:
        try:
            import aiofiles
            async with aiofiles.open(report.audio_recording_path, "rb") as f:
                audio_bytes = await f.read()
        except Exception:
            audio_bytes = b""
        if audio_bytes:
            part = mp.append(audio_bytes)
            part.set_content_disposition("form-data", name="audio", filename="recording.ogg")
            part.headers["Content-Type"] = "audio/ogg"
            part.headers["Content-Length"] = str(len(audio_bytes))

    logger.info("Uploading session report for %s to %s (account_id=%s)", session_id, obs_url, account_id)
    http_session = http_context.http_session()
    async with http_session.post(
        f"{obs_url}/observability/recordings/v0",
        data=mp,
        headers={"Authorization": f"Bearer {jwt}", "Content-Type": mp.content_type},
    ) as resp:
        resp.raise_for_status()
    logger.info("Session report uploaded for %s", session_id)
