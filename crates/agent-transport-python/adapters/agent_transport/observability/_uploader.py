"""Multipart session report uploader.

Adapted from ``sip/livekit/observability.py`` — same multipart shape and
endpoint, but reads the chat history payload from a ``SessionRecorder``
instead of livekit's ``session.history`` / ``session.options`` /
``session.usage`` triple.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Optional

from loguru import logger

from ._env import _build_auth_header

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ._recorder import SessionRecorder


async def upload_session_report(
    *,
    recorder: "SessionRecorder",
    session_id: str,
    obs_url: str,
    recording_path: Optional[str] = None,
    recording_started_at: Optional[float] = None,
    account_id: Optional[str] = None,
    transport: Optional[str] = None,
) -> None:
    """Build a session report from the recorder and upload it.

    ``transport`` is the underlying carrier ("sip" or "audio_stream") and is
    surfaced in the header so the obs server can group sessions by transport.
    """
    import aiohttp

    has_audio = bool(recording_path) and os.path.exists(recording_path or "")
    if recording_path:
        logger.info(
            "Recording path={} exists={} size={}",
            recording_path,
            os.path.exists(recording_path),
            os.path.getsize(recording_path) if os.path.exists(recording_path) else "N/A",
        )

    room_tags: dict = {}
    if account_id:
        room_tags["account_id"] = account_id

    header_payload: dict = {
        "session_id": str(session_id),
        "room_tags": room_tags,
        "start_time": recording_started_at or 0,
    }
    if transport:
        header_payload["transport"] = transport
    header = json.dumps(header_payload)

    chat_history_json = json.dumps(recorder.to_report_dict(session_id=session_id))

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
            with open(recording_path, "rb") as f:
                audio_bytes = f.read()
        except Exception:
            audio_bytes = b""
        if audio_bytes:
            part = mp.append(audio_bytes)
            part.set_content_disposition("form-data", name="audio", filename="recording.ogg")
            part.headers["Content-Type"] = "audio/ogg"
            part.headers["Content-Length"] = str(len(audio_bytes))

    auth_headers = _build_auth_header()
    upload_headers = {"Content-Type": mp.content_type, **auth_headers}

    logger.info(
        "Uploading session report for {} to {} (account_id={}, parts={}, "
        "items={}, usage_entries={}, has_audio={})",
        session_id, obs_url, account_id,
        3 if has_audio else 2,
        len(recorder._items),
        len(recorder._usage),
        has_audio,
    )
    async with aiohttp.ClientSession() as http_session:
        async with http_session.post(
            f"{obs_url}/observability/recordings/v0",
            data=mp,
            headers=upload_headers,
        ) as resp:
            resp.raise_for_status()
    logger.info("Session report uploaded for {}", session_id)
