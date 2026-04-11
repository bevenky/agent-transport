"""Rust-backed Plivo audio streaming serializer.

Drop-in replacement for pipecat.serializers.plivo.PlivoFrameSerializer.
Codec negotiation, audio encoding/decoding, and Plivo protocol handling
are done in Rust — this class holds the configuration.

Inherits from pipecat's `FrameSerializer` so that `isinstance(x, FrameSerializer)`
checks in user code and in pipecat's own transport plumbing succeed. The
abstract `serialize` / `deserialize` methods are implemented as no-ops that
return None — every codec op is performed in Rust inside the endpoint, so
the Python layer is never asked to produce or parse wire bytes.

Usage:
    from agent_transport.audio_stream.pipecat.serializers.plivo import PlivoFrameSerializer
    from agent_transport.audio_stream.pipecat.transports.websocket import WebsocketServerTransport

    serializer = PlivoFrameSerializer(auth_id="...", auth_token="...")
    transport = WebsocketServerTransport(serializer=serializer)
"""

import os
from typing import Optional

try:
    from pipecat.frames.frames import Frame, StartFrame
    from pipecat.serializers.base_serializer import FrameSerializer
except ImportError:  # pragma: no cover — pipecat-ai is declared in [all] extra
    raise ImportError("pipecat-ai is required: pip install pipecat-ai")


class PlivoFrameSerializer(FrameSerializer):
    """Plivo audio streaming configuration.

    Matches `pipecat.serializers.plivo.PlivoFrameSerializer` constructor shape
    so existing user code migrating from the raw pipecat Websocket transport
    keeps working. All real serialization happens in Rust: this subclass only
    carries config (auth, listen address, sample rate) and satisfies the
    abstract base class contract with no-op serialize/deserialize.
    """

    def __init__(
        self,
        *,
        auth_id: Optional[str] = None,
        auth_token: Optional[str] = None,
        listen_addr: Optional[str] = None,
        sample_rate: int = 8000,
        auto_hangup: bool = True,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.auth_id = auth_id or os.environ.get("PLIVO_AUTH_ID", "")
        self.auth_token = auth_token or os.environ.get("PLIVO_AUTH_TOKEN", "")
        self.listen_addr = listen_addr or os.environ.get("AUDIO_STREAM_ADDR", "0.0.0.0:8080")
        self.sample_rate = sample_rate
        self.auto_hangup = auto_hangup

    async def setup(self, frame: "StartFrame") -> None:
        """No-op. The Rust endpoint is already running when StartFrame arrives."""
        return None

    async def serialize(self, frame: "Frame"):
        """No-op. Serialization is performed by the Rust AudioStreamEndpoint.

        Returning None signals "nothing to write on the wire for this frame"
        to pipecat's WebSocket server transport — which is the correct answer
        because our adapter bypasses pipecat's wire path entirely.
        """
        return None

    async def deserialize(self, data):
        """No-op. Deserialization is performed by the Rust AudioStreamEndpoint.

        Returning None signals "no frame produced" — our adapter receives
        already-decoded PCM from Rust via `recv_audio_bytes_blocking`, not
        through the serializer.
        """
        return None
