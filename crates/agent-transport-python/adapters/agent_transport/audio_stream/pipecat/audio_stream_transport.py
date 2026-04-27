"""Pipecat BaseTransport adapter for Plivo audio streaming.

Replaces Pipecat's WebsocketServerTransport + PlivoFrameSerializer entirely.
All audio codec/resampling/pacing is handled in Rust. Python only bridges frames.

100% compatible with Pipecat's transport interface and Plivo AudioStream protocol:
- Event handlers: on_client_connected, on_client_disconnected
- Session metadata: session_id, stream_id, extra_headers
- All Plivo features: mute, recording, checkpoint, background audio, etc.

Frame handling:
- OutputAudioRawFrame → send_audio_notify (Rust backpressure + 20ms send loop pacing)
- InterruptionFrame → clear_buffer (sends clearAudio to Plivo)
- OutputDTMFFrame → send_dtmf (sends sendDTMF to Plivo)
- OutputTransportMessageFrame → send_raw_message (JSON pass-through over WS)
- EndFrame/CancelFrame → hangup (REST API DELETE)
- InputAudioRawFrame ← recv_audio_bytes_blocking (Rust decodes + resamples)
- InputDTMFFrame ← event polling (from Plivo dtmf events)

Bot speaking state (BotStartedSpeaking/BotStoppedSpeaking) is handled by
Pipecat's BaseOutputTransport MediaSender infrastructure — we call
set_transport_ready() which creates the MediaSender with audio task,
chunking, and bot speaking detection.
"""

import asyncio
import json

from typing import Any, Dict, Optional

from loguru import logger

try:
    from pipecat.audio.dtmf.types import KeypadEntry
    from pipecat.frames.frames import (
        CancelFrame, EndFrame, Frame, InputAudioRawFrame,
        InputDTMFFrame, InterruptionFrame, OutputAudioRawFrame,
        OutputTransportMessageFrame, OutputTransportMessageUrgentFrame,
        StartFrame, StopFrame,
    )
    from pipecat.processors.frame_processor import FrameDirection
    from pipecat.transports.base_input import BaseInputTransport
    from pipecat.transports.base_output import BaseOutputTransport
    from pipecat.transports.base_transport import BaseTransport, TransportParams
except ImportError:
    raise ImportError("pipecat-ai is required: pip install pipecat-ai")


# ─── Input Transport ────────────────────────────────────────────────────────


class AudioStreamInputTransport(BaseInputTransport):
    """Receives audio + DTMF from Plivo audio stream.

    Audio is received from the Rust endpoint via blocking recv (GIL released),
    decoded and resampled in Rust, and pushed as InputAudioRawFrame into the
    Pipecat pipeline. DTMF and call lifecycle events are polled separately.
    """

    def __init__(self, endpoint, session_id: str, transport: "AudioStreamTransport",
                 params: Optional[TransportParams] = None,
                 event_queue: Optional[asyncio.Queue] = None, **kwargs):
        if params is None:
            params = TransportParams(
                audio_in_enabled=True,
                audio_in_passthrough=True,
            )
        # Propagate Rust endpoint's sample rate to pipecat's base input
        # transport. BaseInputTransport.start() reads audio_in_sample_rate
        # to configure VAD/turn-analyzers/audio-filters; leaving it None
        # causes it to fall back to frame.audio_in_sample_rate from the
        # synthesized StartFrame, which may not match Rust's output.
        _ep_in = endpoint.input_sample_rate
        if params.audio_in_sample_rate is None:
            params.audio_in_sample_rate = _ep_in
        elif params.audio_in_sample_rate != _ep_in:
            logger.warning(
                "AudioStreamInputTransport: params.audio_in_sample_rate={} does not "
                "match endpoint.input_sample_rate={}. Rust emits at the endpoint rate; "
                "reconfigure the endpoint at construction to match.",
                params.audio_in_sample_rate, _ep_in,
            )
        super().__init__(params, **kwargs)
        self._ep = endpoint
        self._sid = session_id
        self._transport = transport
        self._event_queue = event_queue  # per-session queue from server (avoids event-stealing)
        # Started flag — distinct from _paused (the base-class pause flag).
        # _started gates start()/stop() idempotency. The recv loop runs while
        # the tasks exist; pause/resume are handled by the base class via
        # `self._paused` which `push_audio_frame` already gates on.
        self._started = False
        self._recv_task: Optional[asyncio.Task] = None
        self._event_task: Optional[asyncio.Task] = None

    async def start(self, frame: StartFrame):
        if self._started:
            return
        recorder = getattr(self._transport, "session_recorder", None)
        if recorder is not None:
            try:
                from ....observability import enable_start_frame_metrics
                enable_start_frame_metrics(frame)
            except Exception:
                logger.warning(
                    "Failed to enable pipecat metrics for observability", exc_info=True,
                )
        await super().start(frame)
        self._started = True
        await self.set_transport_ready(frame)
        # Auto-attach the SessionRecorder as a pipeline observer so
        # AGENT_OBSERVABILITY_URL just works without user-side wiring.
        # FrameProcessor.setup() populates self._observer with the
        # PipelineTask's TaskObserver, which exposes add_observer().
        if recorder is not None and self._observer is not None:
            try:
                if recorder not in getattr(self._observer, "_observers", []):
                    self._observer.add_observer(recorder)
            except Exception:
                logger.warning(
                    "Failed to auto-attach SessionRecorder observer", exc_info=True,
                )
        if recorder is not None:
            try:
                from ....observability import populate_options_from_pipeline
                populate_options_from_pipeline(recorder, self, frame)
                logger.info(
                    "Session options populated from pipeline ({} processors)",
                    len(recorder._options.get("pipeline", [])),
                )
            except Exception:
                logger.warning(
                    "Failed to populate session options from pipeline", exc_info=True,
                )
        self._recv_task = asyncio.create_task(self._recv_loop())
        self._event_task = asyncio.create_task(self._event_loop())
        await self._transport._call_event_handler("on_client_connected")

    async def stop(self, frame: EndFrame):
        if not self._started:
            await super().stop(frame)
            return
        self._started = False
        await self._cancel_tasks()
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame):
        self._started = False
        await self._cancel_tasks()
        await super().cancel(frame)

    # NOTE: pause(StopFrame) intentionally not overridden — base-class
    # pause sets `self._paused = True` which `push_audio_frame()` already
    # gates on. The recv loop keeps running and frames are dropped at the
    # base-class push site while paused. On resume the flag clears and
    # frames flow again, no task recreation needed.

    async def _cancel_tasks(self):
        """Cancel and await background tasks."""
        tasks = [t for t in [self._recv_task, self._event_task] if t and not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._recv_task = None
        self._event_task = None

    async def _recv_loop(self):
        """Receive audio from Rust endpoint via blocking call (GIL released)."""
        loop = asyncio.get_running_loop()
        try:
            while self._started:
                try:
                    result = await loop.run_in_executor(
                        None, lambda: self._ep.recv_audio_bytes_blocking(self._sid, 20)
                    )
                except Exception:
                    # Session ended (remote close removed the session from
                    # the Rust session map). Exit the recv loop cleanly —
                    # the event loop fires on_client_disconnected.
                    break
                if result is not None:
                    audio_bytes, sample_rate, num_channels = result
                    # push_audio_frame() drops the frame if base-class
                    # `_paused == True`, so no extra check needed here.
                    await self.push_audio_frame(InputAudioRawFrame(
                        audio=bytes(audio_bytes), sample_rate=sample_rate, num_channels=num_channels,
                    ))
        except asyncio.CancelledError:
            raise

    async def _event_loop(self):
        """Poll for DTMF, call state, and lifecycle events.

        If an event_queue is provided (by the server), reads from it.
        Otherwise falls back to polling endpoint directly (standalone usage).
        """
        if self._event_queue:
            await self._event_loop_from_queue()
        else:
            await self._event_loop_from_endpoint()

    async def _event_loop_from_queue(self):
        """Read events from per-session queue (dispatched by server)."""
        while self._started:
            try:
                event = await asyncio.wait_for(self._event_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.debug("AudioStreamInputTransport event_loop error: {}", e)
                break
            try:
                await self._handle_event(event)
            except Exception:
                logger.exception("AudioStreamInputTransport handler failed for event %r", event.get("type") if isinstance(event, dict) else event)

    async def _event_loop_from_endpoint(self):
        """Poll events directly from endpoint (standalone, no server)."""
        loop = asyncio.get_running_loop()
        while self._started:
            try:
                event = await loop.run_in_executor(
                    None, lambda: self._ep.wait_for_event(timeout_ms=100)
                )
            except Exception as e:
                logger.debug("AudioStreamInputTransport endpoint event_loop error: {}", e)
                break
            if event is None:
                continue
            try:
                await self._handle_event(event)
            except Exception:
                logger.exception("AudioStreamInputTransport handler failed for event %r", event.get("type") if isinstance(event, dict) else event)

    async def _handle_event(self, event):
        """Process a single event."""
        event_type = event.get("type", "")
        if event_type == "dtmf_received":
            await self.push_frame(InputDTMFFrame(button=KeypadEntry(event["digit"])))
        elif event_type == "call_terminated":
            self._started = False
            await self._transport._call_event_handler("on_client_disconnected")
            await self.push_frame(EndFrame())
        elif event_type == "beep_detected":
            await self._transport._call_event_handler(
                "on_beep_detected",
                event.get("frequency_hz"), event.get("duration_ms"),
            )
        elif event_type == "beep_timeout":
            await self._transport._call_event_handler("on_beep_timeout")


# ─── Output Transport ───────────────────────────────────────────────────────


class AudioStreamOutputTransport(BaseOutputTransport):
    """Sends audio to Plivo audio stream.

    Uses Pipecat's MediaSender infrastructure (via set_transport_ready) for:
    - Audio chunking to transport chunk size
    - BotStartedSpeaking/BotStoppedSpeaking state management
    - Proper interruption handling (task cancellation + restart)

    Audio is sent to the Rust endpoint which handles:
    - Backpressure-aware buffering (200ms threshold)
    - 20ms paced send loop (tokio::time::interval)
    - Encoding negotiation (mulaw/L16 8k/16k)
    - Background audio mixing
    """

    def __init__(self, endpoint, session_id: str, transport: "AudioStreamTransport",
                 params: Optional[TransportParams] = None, **kwargs):
        if params is None:
            params = TransportParams(
                audio_out_enabled=True,
            )
        # Match Rust endpoint's outbound sample rate so pipecat's MediaSender
        # chunks/paces frames against the same rate the Rust RTP/WS send loop
        # emits. Mismatch causes the chunker to over- or under-feed the wire.
        _ep_out = endpoint.output_sample_rate
        if params.audio_out_sample_rate is None:
            params.audio_out_sample_rate = _ep_out
        elif params.audio_out_sample_rate != _ep_out:
            logger.warning(
                "AudioStreamOutputTransport: params.audio_out_sample_rate={} does "
                "not match endpoint.output_sample_rate={}. Reconfigure the endpoint "
                "at construction to match.",
                params.audio_out_sample_rate, _ep_out,
            )
        super().__init__(params, **kwargs)
        self._ep = endpoint
        self._sid = session_id
        self._transport = transport
        self._started = False

    async def start(self, frame: StartFrame):
        if self._started:
            return
        self._started = True
        await super().start(frame)
        self._loop = asyncio.get_running_loop()
        await self.set_transport_ready(frame)

    def _supports_native_dtmf(self) -> bool:
        return True

    async def _write_dtmf_native(self, frame):
        digit = str(frame.button.value)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: self._ep.send_dtmf(self._sid, digit))

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        """Send audio frame to audio stream with Rust backpressure.

        Audio pacing is handled entirely in Rust (20ms tokio::time::interval).
        Python pushes frames as fast as Rust can accept them:
        1. Push audio to Rust buffer via send_audio_notify with callback
        2. If buffer below threshold, callback fires immediately → return
        3. If above threshold, callback deferred until send loop drains
        4. Callback resolves Future via call_soon_threadsafe → return
        No asyncio.sleep — no jitter, no underruns.
        """
        capture_fut = self._loop.create_future()

        def _on_complete():
            def _resolve():
                if not capture_fut.done():
                    capture_fut.set_result(None)
            try:
                self._loop.call_soon_threadsafe(_resolve)
            except RuntimeError:
                if not capture_fut.done():
                    try:
                        capture_fut.set_result(None)
                    except Exception:
                        pass

        try:
            self._ep.send_audio_notify(
                self._sid,
                frame.audio,
                frame.sample_rate,
                frame.num_channels,
                _on_complete,
            )
        except Exception:
            return False

        await capture_fut
        return True

    def queued_frames(self) -> int:
        """Number of 20ms audio frames buffered in the Rust outgoing queue.

        Multiply by 0.02 to get seconds of buffered audio.
        """
        try:
            return self._ep.queued_frames(self._sid)
        except Exception:
            return 0

    async def send_message(self, frame):
        """Send OutputTransportMessageFrame as raw JSON over the WebSocket.

        Filters RTVI internal messages (matching PlivoFrameSerializer.should_ignore_frame).
        Non-dict messages are serialized to JSON string.
        """
        if isinstance(frame.message, dict) and frame.message.get("label") == "rtvi-ai":
            return
        try:
            msg = json.dumps(frame.message) if not isinstance(frame.message, str) else frame.message
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, lambda: self._ep.send_raw_message(self._sid, msg))
        except Exception as e:
            logger.warning("send_message failed: {}", e)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Handle InterruptionFrame → clearAudio before base class processing.

        Sends clearAudio to Plivo (clears server-side buffer) and clears the
        local AudioBuffer (fires pending completion callbacks). The base class
        then cancels and restarts the MediaSender audio task.
        """
        if isinstance(frame, InterruptionFrame):
            try:
                self._ep.clear_buffer(self._sid)
            except Exception as e:
                logger.debug("clear_buffer on interruption failed: {}", e)

        await super().process_frame(frame, direction)

    async def stop(self, frame: EndFrame):
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, lambda: self._ep.hangup(self._sid))
        except Exception as e:
            logger.debug("hangup on stop failed: {}", e)
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame):
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, lambda: self._ep.hangup(self._sid))
        except Exception as e:
            logger.debug("hangup on cancel failed: {}", e)
        await super().cancel(frame)


# ─── Composite Transport ────────────────────────────────────────────────────


class AudioStreamTransport(BaseTransport):
    """Pipecat transport for Plivo audio streaming via agent-transport.

    This is the main entry point. It creates lazily-initialized input and
    output transport processors that are wired into a Pipecat pipeline.

    Architecture:
        AudioStreamEndpoint (Rust) handles the WebSocket server, audio codec
        negotiation, 20ms paced sending, and all Plivo protocol messages.
        This transport bridges Pipecat frames to the Rust endpoint.

    Event handlers (register via @transport.event_handler("name")):
        on_client_connected  — Plivo WebSocket connected, stream started
        on_client_disconnected — stream stopped or WebSocket disconnected
        on_beep_detected — voicemail beep detected (if beep detection active)
        on_beep_timeout — beep detection timed out

    Session metadata (from Plivo start event):
        transport.session_id    — internal session ID
        transport.call_uuid     — Plivo call UUID
        transport.stream_id     — Plivo stream UUID
        transport.extra_headers — custom headers from <Stream> element

    Plivo feature methods:
        mute() / unmute()           — mute/unmute outgoing audio
        pause_playback() / resume_playback() — pause/resume audio send loop
        start_recording() / stop_recording() — OGG/Opus call recording
        clear_buffer()              — clear audio + send clearAudio to Plivo
        send_background_audio()     — mix background audio with agent voice
        checkpoint() / wait_for_playout() — track playback completion
    """

    def __init__(
        self,
        endpoint,
        session_id: str,
        *,
        name: Optional[str] = None,
        params: Optional[TransportParams] = None,
        session_data: Optional[Dict[str, Any]] = None,
        _event_queue: Optional[asyncio.Queue] = None,
        **kwargs,
    ):
        super().__init__(name=name or "AudioStreamTransport", **kwargs)
        self._ep = endpoint
        self._sid = session_id
        self._params = params
        self._session_data = session_data or {}
        self._event_queue = _event_queue  # per-session queue from server
        self._input: Optional[AudioStreamInputTransport] = None
        self._output: Optional[AudioStreamOutputTransport] = None

        # Register Pipecat event handlers (same pattern as WebsocketServerTransport)
        self._register_event_handler("on_client_connected")
        self._register_event_handler("on_client_disconnected")
        self._register_event_handler("on_beep_detected")
        self._register_event_handler("on_beep_timeout")

    # ── Pipeline processors ──────────────────────────────────────────────

    def input(self) -> AudioStreamInputTransport:
        if self._input is None:
            self._input = AudioStreamInputTransport(
                self._ep, self._sid, transport=self,
                params=self._params, event_queue=self._event_queue,
                name=f"{self._name}-input",
            )
        return self._input

    def output(self) -> AudioStreamOutputTransport:
        if self._output is None:
            self._output = AudioStreamOutputTransport(
                self._ep, self._sid, transport=self,
                params=self._params, name=f"{self._name}-output",
            )
        return self._output

    # ── Session metadata (from Plivo start event) ────────────────────────

    @property
    def session_id(self) -> str:
        """Internal session ID used to address this stream."""
        return self._sid

    @property
    def call_uuid(self) -> str:
        """Plivo call UUID (from start event). Used for REST API operations."""
        return self._session_data.get("call_uuid", "")

    @property
    def stream_id(self) -> str:
        """Plivo stream UUID (from start event)."""
        return self._session_data.get("local_uri", "")

    @property
    def extra_headers(self) -> Dict[str, str]:
        """Custom headers from <Stream> element's extraHeaders attribute."""
        return self._session_data.get("extra_headers", {})

    # ── Plivo audio control ──────────────────────────────────────────────

    def mute(self) -> None:
        """Mute outgoing audio. Send loop outputs silence, queue preserved."""
        self._ep.mute(self._sid)

    def unmute(self) -> None:
        """Unmute outgoing audio."""
        self._ep.unmute(self._sid)

    def pause_playback(self) -> None:
        """Pause the audio send loop. Queue accumulates, nothing sent to Plivo."""
        self._ep.pause(self._sid)

    def resume_playback(self) -> None:
        """Resume the audio send loop."""
        self._ep.resume(self._sid)

    def clear_buffer(self) -> None:
        """Clear local audio buffer AND send clearAudio to Plivo.

        Two-level clear:
        1. Local AudioBuffer cleared (fires pending completion callbacks)
        2. clearAudio sent to Plivo (clears server-side playback buffer)
        """
        self._ep.clear_buffer(self._sid)

    # ── Background audio ─────────────────────────────────────────────────

    def send_background_audio(self, audio: bytes, sample_rate: int, num_channels: int) -> None:
        """Send background audio to be mixed with agent voice.

        Background audio is mixed in the Rust send loop at 20ms intervals.
        No backpressure — background audio is best-effort.

        Use cases: hold music, ambient sounds, notification tones.
        """
        self._ep.send_background_audio(self._sid, audio, sample_rate, num_channels)

    # ── Checkpoint / playout tracking ────────────────────────────────────

    def checkpoint(self, name: Optional[str] = None) -> str:
        """Send a checkpoint to Plivo.

        Plivo responds with a playedStream event when all audio queued
        before this checkpoint has been played to the caller.

        Args:
            name: Optional checkpoint name. Auto-generated if not provided.

        Returns:
            The checkpoint name (for tracking).
        """
        return self._ep.checkpoint(self._sid, name)

    async def wait_for_playout(self, timeout_ms: int = 5000) -> bool:
        """Wait for the last checkpoint to be confirmed by Plivo.

        Blocks until Plivo sends a playedStream event confirming that all
        audio up to the last checkpoint has been played to the caller.

        Args:
            timeout_ms: Maximum time to wait in milliseconds.

        Returns:
            True if playout confirmed, False if timed out.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self._ep.wait_for_playout(self._sid, timeout_ms)
        )

    # ── Recording ────────────────────────────────────────────────────────

    def start_recording(self, path: str, stereo: bool = True) -> None:
        """Start recording the call to an OGG/Opus file.

        Args:
            path: Output file path (.ogg).
            stereo: If True, record stereo (L=user, R=agent). Otherwise mono.
        """
        self._ep.start_recording(self._sid, path, stereo)

    def stop_recording(self) -> None:
        """Stop recording the call."""
        self._ep.stop_recording(self._sid)

    # ── DTMF ─────────────────────────────────────────────────────────────

    def send_dtmf(self, digits: str) -> None:
        """Send DTMF digits to the caller via Plivo.

        Args:
            digits: One or more DTMF digits (0-9, *, #, A-D).
        """
        self._ep.send_dtmf(self._sid, digits)

    # ── Beep detection ───────────────────────────────────────────────────

    def detect_beep(self, timeout_ms: int = 30000, min_duration_ms: int = 80,
                    max_duration_ms: int = 5000) -> None:
        """Start voicemail beep detection on incoming audio.

        Fires on_beep_detected or on_beep_timeout event when done.
        """
        self._ep.detect_beep(self._sid, timeout_ms, min_duration_ms, max_duration_ms)

    def cancel_beep_detection(self) -> None:
        """Cancel ongoing beep detection."""
        self._ep.cancel_beep_detection(self._sid)

    # ── Raw WebSocket access ─────────────────────────────────────────────

    def send_raw_message(self, message: str) -> None:
        """Send a raw JSON message over the Plivo WebSocket.

        For advanced use cases where you need to send custom messages.
        """
        self._ep.send_raw_message(self._sid, message)
