"""Pipecat BaseTransport adapter for SIP transport.

Usage:
    from agent_transport import SipEndpoint
    from agent_transport_adapters.pipecat import SipTransport

    ep = SipEndpoint(sip_server="phone.plivo.com")
    ep.register(username, password)
    call_id = ep.call(dest_uri)

    transport = SipTransport(ep, call_id)
    pipeline = Pipeline([transport.input(), stt, llm, tts, transport.output()])
"""

import asyncio
import struct
from typing import Optional

try:
    from pipecat.frames.frames import (
        CancelFrame,
        EndFrame,
        Frame,
        InputAudioRawFrame,
        InputDTMFFrame,
        InterruptionFrame,
        OutputAudioRawFrame,
        OutputDTMFFrame,
        OutputDTMFUrgentFrame,
        StartFrame,
    )
    from pipecat.transports.base_input import BaseInputTransport
    from pipecat.transports.base_output import BaseOutputTransport
    from pipecat.transports.base_transport import BaseTransport
except ImportError:
    raise ImportError("pipecat-ai is required: pip install pipecat-ai")

from agent_transport import SipEndpoint, AudioFrame


class SipInputTransport(BaseInputTransport):
    """Receives audio from SIP call and pushes to Pipecat pipeline."""

    def __init__(self, endpoint: SipEndpoint, call_id: int, **kwargs):
        super().__init__(**kwargs)
        self._ep = endpoint
        self._cid = call_id
        self._running = False
        self._audio_task = None
        self._event_task = None

    async def start(self, frame: StartFrame):
        await super().start(frame)
        self._running = True
        self._audio_task = asyncio.create_task(self._audio_poll_loop())
        self._event_task = asyncio.create_task(self._event_poll_loop())

    async def stop(self, frame: EndFrame):
        self._running = False
        if self._audio_task:
            self._audio_task.cancel()
        if self._event_task:
            self._event_task.cancel()
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame):
        self._running = False
        if self._audio_task:
            self._audio_task.cancel()
        if self._event_task:
            self._event_task.cancel()
        await super().cancel(frame)

    async def _audio_poll_loop(self):
        """Poll recv_audio and push frames to pipeline."""
        while self._running:
            frame = self._ep.recv_audio(self._cid)
            if frame is not None:
                # Convert int16 list to bytes
                pcm_bytes = struct.pack(f"<{len(frame.data)}h", *frame.data)
                await self.push_audio_frame(
                    InputAudioRawFrame(
                        audio=pcm_bytes,
                        sample_rate=frame.sample_rate,
                        num_channels=frame.num_channels,
                    )
                )
            else:
                await asyncio.sleep(0.005)

    async def _event_poll_loop(self):
        """Poll events for DTMF and call state."""
        while self._running:
            event = self._ep.poll_event()
            if event is None:
                await asyncio.sleep(0.02)
                continue
            if event["type"] == "dtmf_received":
                await self.push_frame(InputDTMFFrame(digit=event["digit"]))
            elif event["type"] == "call_terminated":
                self._running = False
                await self.push_frame(EndFrame())


class SipOutputTransport(BaseOutputTransport):
    """Sends audio to SIP call from Pipecat pipeline."""

    def __init__(self, endpoint: SipEndpoint, call_id: int, **kwargs):
        super().__init__(**kwargs)
        self._ep = endpoint
        self._cid = call_id

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        """Send audio frame to SIP call."""
        # Convert bytes to int16 list
        n_samples = len(frame.audio) // 2
        data = list(struct.unpack(f"<{n_samples}h", frame.audio))
        agent_frame = AudioFrame(data, frame.sample_rate, frame.num_channels)
        try:
            self._ep.send_audio(self._cid, agent_frame)
            return True
        except Exception:
            return False

    def _supports_native_dtmf(self) -> bool:
        return True

    async def _write_dtmf_native(self, frame):
        """Send DTMF via SIP (RFC 4733 or SIP INFO)."""
        digit = str(frame.digit) if hasattr(frame, "digit") else str(frame)
        self._ep.send_dtmf(self._cid, digit)

    async def stop(self, frame: EndFrame):
        """Hang up the SIP call on pipeline end."""
        try:
            self._ep.hangup(self._cid)
        except Exception:
            pass
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame):
        """Hang up on cancel."""
        try:
            self._ep.hangup(self._cid)
        except Exception:
            pass
        await super().cancel(frame)


class SipTransport(BaseTransport):
    """Pipecat transport for SIP calls via agent-transport.

    Wraps a SipEndpoint and call_id into Pipecat's BaseTransport interface.
    """

    def __init__(
        self,
        endpoint: SipEndpoint,
        call_id: int,
        *,
        name: Optional[str] = None,
        input_name: Optional[str] = None,
        output_name: Optional[str] = None,
    ):
        super().__init__(
            name=name or "SipTransport",
            input_name=input_name,
            output_name=output_name,
        )
        self._ep = endpoint
        self._cid = call_id
        self._input = None
        self._output = None

    def input(self) -> SipInputTransport:
        if self._input is None:
            self._input = SipInputTransport(
                self._ep, self._cid, name=f"{self._name}-input"
            )
        return self._input

    def output(self) -> SipOutputTransport:
        if self._output is None:
            self._output = SipOutputTransport(
                self._ep, self._cid, name=f"{self._name}-output"
            )
        return self._output
