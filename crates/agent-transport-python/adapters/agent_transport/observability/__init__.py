"""Observability for the agent-transport pipecat adapters.

Mirrors the LiveKit observability flow (see
``agent_transport/sip/livekit/observability.py``): at session end, build a
multipart session report (header JSON + chat history JSON + OGG audio) and
POST it to the standalone observability server at
``/observability/recordings/v0``. Recording is gated on
``AGENT_OBSERVABILITY_URL`` — when unset, both the recording and the upload
are skipped, so there is zero overhead.

Pipecat has no equivalent of livekit-agents' ``session.history`` /
``session.options`` / ``session.usage``, so the chat history payload is
assembled from a :class:`SessionRecorder` (a ``BaseObserver`` the user adds
to their pipeline) plus a ``TranscriptProcessor`` hook.
"""

from ._env import (
    _build_auth_header,
    _get_observability_url,
    _recording_dir,
)
from ._pipeline_introspect import (
    enable_start_frame_metrics,
    populate_options_from_pipeline,
)
from ._recorder import SessionRecorder
from ._uploader import upload_session_report

__all__ = [
    "SessionRecorder",
    "upload_session_report",
    "enable_start_frame_metrics",
    "populate_options_from_pipeline",
    "_get_observability_url",
    "_build_auth_header",
    "_recording_dir",
]
