"""Env-var helpers for observability — the gating live wires."""

import base64
import os


def _get_observability_url() -> str | None:
    """Return the observability server URL, or ``None`` when disabled.

    When this returns ``None``, both recording and upload are skipped.
    """
    url = os.environ.get("AGENT_OBSERVABILITY_URL")
    return url or None


def _build_auth_header() -> dict[str, str]:
    """Build a basic-auth header from env vars.

    Returns an empty dict when ``AGENT_OBSERVABILITY_USER`` or
    ``AGENT_OBSERVABILITY_PASS`` is unset — the request goes out without
    an Authorization header in that case.
    """
    user = os.environ.get("AGENT_OBSERVABILITY_USER")
    password = os.environ.get("AGENT_OBSERVABILITY_PASS")
    if not user or not password:
        return {}
    credentials = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {credentials}"}


def _recording_dir() -> str:
    """Directory for temporary recording files. Default ``/tmp/agent-sessions``."""
    return os.environ.get("RECORDING_DIR", "/tmp/agent-sessions")
