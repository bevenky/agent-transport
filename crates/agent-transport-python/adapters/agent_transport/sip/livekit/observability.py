"""Observability for agent-transport.

Uploads a session report (transcript + events + config + audio) to the Plivo
observability server at call end. Basic auth via AGENT_OBSERVABILITY_USER /
AGENT_OBSERVABILITY_PASS (optional — request is sent without auth if unset).

Set AGENT_OBSERVABILITY_URL to enable.
"""

import logging
import os

logger = logging.getLogger("agent_transport.observability")


def _get_observability_url() -> str | None:
    return os.environ.get("AGENT_OBSERVABILITY_URL")


def _build_auth_header() -> dict[str, str]:
    """Build basic auth header from env vars. Returns empty dict if not configured."""
    import base64
    user = os.environ.get("AGENT_OBSERVABILITY_USER")
    password = os.environ.get("AGENT_OBSERVABILITY_PASS")
    if not user or not password:
        return {}
    credentials = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {credentials}"}


def _build_report_dict(session, session_id: str) -> dict:
    """Build session report dict from session fields directly."""
    import time

    events = [
        e.model_dump() if hasattr(e, "model_dump") else dict(e)
        for e in (session._recorded_events or [])
        if getattr(e, "type", None) not in ("metrics_collected", "session_usage_updated")
    ]

    chat_history = session.history.copy() if hasattr(session.history, "copy") else session.history
    if hasattr(chat_history, "to_dict"):
        chat_history_dict = chat_history.to_dict(exclude_timestamp=False)
    elif hasattr(chat_history, "toJSON"):
        chat_history_dict = chat_history.toJSON(exclude_timestamp=False)
    else:
        chat_history_dict = {"items": list(chat_history) if chat_history else []}

    options = session.options
    options_dict = {}
    for k, v in vars(options).items():
        if k.startswith("_"):
            continue
        if isinstance(v, (str, int, float, bool, type(None))):
            options_dict[k] = v

    usage = None
    if session.usage and hasattr(session.usage, "model_usage") and session.usage.model_usage:
        usage = []
        for u in session.usage.model_usage:
            entry = u.model_dump() if hasattr(u, "model_dump") else dict(u) if hasattr(u, "__dict__") else {}
            usage.append({k: v for k, v in entry.items() if v not in (0, None, "")})

    return {
        "job_id": str(session_id),
        "room_id": str(session_id),
        "room": str(session_id),
        "events": events,
        "chat_history": chat_history_dict,
        "options": options_dict,
        "timestamp": time.time(),
        "usage": usage,
    }


async def upload_session_report(
    session,
    session_id: str,
    obs_url: str,
    agent_name: str,
    recording_path: str | None = None,
    recording_started_at: float | None = None,
    account_id: str | None = None,
    transport: str | None = None,
) -> None:
    """Build a session report and upload it to the observability server.

    Used by both AgentServer (SIP) and AudioStreamServer. `transport` is the
    transport that carried the call — "sip" or "audio_stream".
    """
    import json
    import aiohttp

    has_audio = recording_path and os.path.exists(recording_path)
    if recording_path:
        logger.info(
            "Recording path=%s exists=%s size=%s",
            recording_path,
            os.path.exists(recording_path),
            os.path.getsize(recording_path) if os.path.exists(recording_path) else "N/A",
        )

    room_tags = {}
    if account_id:
        room_tags["account_id"] = account_id

    header_payload = {
        "session_id": str(session_id),
        "room_tags": room_tags,
        "start_time": recording_started_at or 0,
    }
    if transport:
        header_payload["transport"] = transport
    header = json.dumps(header_payload)

    report_dict = _build_report_dict(session, session_id)
    chat_history_json = json.dumps(report_dict)

    mp = aiohttp.MultipartWriter("form-data")

    part = mp.append(header)
    part.set_content_disposition("form-data", name="header", filename="header.json")
    part.headers["Content-Type"] = "application/json"
    part.headers["Content-Length"] = str(len(header))

    part = mp.append(chat_history_json)
    part.set_content_disposition("form-data", name="chat_history", filename="chat_history.json")
    part.headers["Content-Type"] = "application/json"
    part.headers["Content-Length"] = str(len(chat_history_json))

    if has_audio:
        try:
            import aiofiles
            async with aiofiles.open(recording_path, "rb") as f:
                audio_bytes = await f.read()
        except Exception:
            audio_bytes = b""
        if audio_bytes:
            part = mp.append(audio_bytes)
            part.set_content_disposition("form-data", name="audio", filename="recording.ogg")
            part.headers["Content-Type"] = "audio/ogg"
            part.headers["Content-Length"] = str(len(audio_bytes))

    auth_headers = _build_auth_header()
    upload_headers = {"Content-Type": mp.content_type, **auth_headers}

    logger.info("Uploading session report for %s to %s (account_id=%s)", session_id, obs_url, account_id)
    async with aiohttp.ClientSession() as http_session:
        async with http_session.post(
            f"{obs_url}/observability/recordings/v0",
            data=mp,
            headers=upload_headers,
        ) as resp:
            resp.raise_for_status()
    logger.info("Session report uploaded for %s", session_id)
