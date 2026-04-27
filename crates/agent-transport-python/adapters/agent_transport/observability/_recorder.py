"""SessionRecorder — pipecat ``BaseObserver`` that collects session-report data.

User adds it to the pipeline once (``task.add_observer(transport.session_recorder)``);
everything else is automatic. As a ``BaseObserver`` it sees every frame that any
processor pushes — including ``TranscriptionFrame``s consumed upstream by
``user_aggregator`` before they could reach ``transport.output()``.

The output of :meth:`to_report_dict` matches the livekit ``chat_history.items``
shape so the agent-observability dashboard renders pipecat sessions identically:
``[{type: "message", role, content, metrics: {...}, id}, ...]``.
"""

from __future__ import annotations

import time
import uuid
import json
from datetime import datetime, timezone
from typing import Any

try:
    from pipecat.observers.base_observer import BaseObserver, FramePushed
    from pipecat.frames.frames import (
        BotStartedSpeakingFrame,
        BotStoppedSpeakingFrame,
        FunctionCallCancelFrame,
        FunctionCallInProgressFrame,
        FunctionCallResultFrame,
        InterimTranscriptionFrame,
        InterruptionFrame,
        MetricsFrame,
        TranscriptionFrame,
        TTSTextFrame,
        UserStartedSpeakingFrame,
        UserStoppedSpeakingFrame,
    )
    from pipecat.metrics.metrics import (
        LLMUsageMetricsData,
        ProcessingMetricsData,
        SmartTurnMetricsData,
        TTFBMetricsData,
        TextAggregationMetricsData,
        TurnMetricsData,
        TTSUsageMetricsData,
    )
except ImportError:
    raise ImportError("pipecat-ai is required: pip install pipecat-ai")


def _ts(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _unix_seconds(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if hasattr(value, "timestamp"):
        try:
            return float(value.timestamp())
        except (TypeError, ValueError, OSError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    return None


def _event_time(event: dict) -> float | None:
    if not isinstance(event, dict):
        return None
    return _unix_seconds(event.get("created_at"))


def _get_field(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _extract_confidence_value(obj: Any, *, depth: int = 0) -> float | None:
    if obj is None or depth > 5 or isinstance(obj, (str, bytes)):
        return None
    direct = _get_field(obj, "confidence")
    if isinstance(direct, (int, float)):
        return float(direct)
    for name in ("channel", "channels", "alternatives", "results", "result"):
        child = _get_field(obj, name)
        if child is None:
            continue
        if isinstance(child, (list, tuple)):
            for entry in child:
                value = _extract_confidence_value(entry, depth=depth + 1)
                if value is not None:
                    return value
        else:
            value = _extract_confidence_value(child, depth=depth + 1)
            if value is not None:
                return value
    return None


def _transcript_confidence(frame: Any) -> float | None:
    for attr in ("transcript_confidence", "confidence"):
        value = getattr(frame, attr, None)
        if isinstance(value, (int, float)):
            return float(value)
    return _extract_confidence_value(getattr(frame, "result", None))


def _stringify_jsonish(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return str(value)


def _classify_processor(processor: str) -> str:
    """Best-effort classification of a pipecat processor name.

    Used to attribute LLM/TTS/STT metrics to the right per-turn fields.
    """
    p = (processor or "").lower()
    if "stt" in p or "transcrib" in p or "deepgram" in p:
        return "stt"
    if "llm" in p:
        return "llm"
    if "tts" in p:
        return "tts"
    return "other"


def _provider_from_processor(processor: str) -> str | None:
    """Convert a Pipecat processor name into a compact provider label."""
    if not processor:
        return None
    name = processor.split("#", 1)[0]
    for suffix in ("STTService", "LLMService", "TTSService", "Service"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name or processor


def _model_metadata(metric: Any, kind: str) -> dict | None:
    provider = _provider_from_processor(getattr(metric, "processor", ""))
    model = getattr(metric, "model", None)
    if not provider and not model:
        return None
    out: dict = {}
    if model:
        out["model_name"] = model
    if provider:
        out["model_provider"] = provider
    return out or None


class SessionRecorder(BaseObserver):
    """Pipecat observer that captures the data needed for an obs session report.

    Attach to the pipeline task once::

        task = PipelineTask(pipeline, params=PipelineParams(...))
        task.add_observer(transport.session_recorder)

    The framework reads :meth:`to_report_dict` at session end to build the
    multipart upload.
    """

    def __init__(
        self,
        *,
        options: dict | None = None,
    ) -> None:
        super().__init__()
        self._items: list[dict] = []
        self._events: list[dict] = []
        self._usage: list[dict] = []
        self._timing: list[dict] = []
        self._options: dict = dict(options or {})

        # Current turn under construction. We aggregate consecutive same-role
        # text fragments into a single "message" item; switching role flushes.
        self._cur_role: str | None = None
        self._cur_parts: list[str] = []
        self._cur_first_ts: Any = None
        self._cur_created_at: float | None = None
        self._cur_transcript_confidence: float | None = None
        self._cur_metrics: dict = {}
        self._pending_metrics: dict[str, dict] = {
            "user": {},
            "assistant": {},
        }
        self._last_user_stopped_at: float | None = None
        self._user_state = "listening"
        self._agent_state = "listening"
        self._function_calls: dict[str, dict] = {}

        # Pipecat's ``MetricsFrame`` is a ``SystemFrame`` that propagates
        # through every processor — ``BaseObserver.on_push_frame`` fires once
        # per processor on the same frame object. Track the ones we've
        # already counted so each metric lands in the report exactly once.
        self._seen_metrics_frames: list[Any] = []
        self._seen_metric_payloads: set[tuple] = set()
        self._seen_text_frames: list[Any] = []

    @property
    def options(self) -> dict:
        return self._options

    # ── User-facing event hook ──────────────────────────────────────────

    def record_event(self, event: dict) -> None:
        """Append a free-form event (e.g. lifecycle markers) to the report."""
        self._events.append(event)

    # ── BaseObserver: see every push_frame in the pipeline ──────────────

    async def on_push_frame(self, data: FramePushed) -> None:  # noqa: D401
        frame = data.frame

        if isinstance(frame, TranscriptionFrame) and not isinstance(
            frame, InterimTranscriptionFrame
        ):
            if self._text_frame_seen(frame):
                return
            text = getattr(frame, "text", None)
            timestamp = getattr(frame, "timestamp", None)
            created_at = (
                _unix_seconds(timestamp)
                or self._last_user_stopped_at
                or self._created_at_for("user", timestamp)
            )
            self._append_role(
                "user",
                text,
                timestamp,
                transcript_confidence=_transcript_confidence(frame),
            )
            if text:
                self._events.append({
                    "type": "user_input_transcribed",
                    "transcript": text,
                    "is_final": True,
                    "speaker_id": getattr(frame, "user_id", None),
                    "language": str(getattr(frame, "language", None))
                    if getattr(frame, "language", None) is not None
                    else None,
                    "created_at": created_at,
                })
            if self._agent_state == "listening":
                self._set_agent_state("thinking", created_at)
            return

        if isinstance(frame, TTSTextFrame):
            if self._text_frame_seen(frame):
                return
            self._append_role(
                "assistant",
                getattr(frame, "text", None),
                getattr(frame, "timestamp", None),
            )
            return

        if isinstance(frame, MetricsFrame):
            if any(seen is frame for seen in self._seen_metrics_frames):
                return
            self._seen_metrics_frames.append(frame)
            for m in getattr(frame, "data", []) or []:
                self._on_metric(m, data)
            return

        if isinstance(frame, UserStartedSpeakingFrame):
            started_at = self._mark_speaking("user", "started_speaking_at")
            self._set_user_state("speaking", started_at)
            return

        if isinstance(frame, UserStoppedSpeakingFrame):
            stopped_at = self._mark_speaking("user", "stopped_speaking_at")
            self._last_user_stopped_at = stopped_at
            self._set_user_state("listening", stopped_at)
            return

        if isinstance(frame, BotStartedSpeakingFrame):
            started_at = self._mark_speaking("assistant", "started_speaking_at")
            target = self._target_for("assistant")
            if (
                self._last_user_stopped_at is not None
                and started_at >= self._last_user_stopped_at
            ):
                target.setdefault("e2e_latency", started_at - self._last_user_stopped_at)
            self._set_agent_state("speaking", started_at)
            return

        if isinstance(frame, BotStoppedSpeakingFrame):
            stopped_at = self._mark_speaking("assistant", "stopped_speaking_at")
            self._set_agent_state("listening", stopped_at)
            return

        if isinstance(frame, FunctionCallInProgressFrame):
            self._append_function_call(frame)
            return

        if isinstance(frame, FunctionCallResultFrame):
            self._append_function_result(frame)
            return

        if isinstance(frame, FunctionCallCancelFrame):
            self._append_function_cancel(frame)
            return

        if isinstance(frame, InterruptionFrame):
            # Mark the most recent assistant turn as interrupted (livekit shape).
            # If the assistant turn is still being assembled, flag it on the
            # in-progress metrics so _flush_current carries the bit through.
            if self._cur_role == "assistant":
                self._cur_metrics["interrupted"] = True
                return
            for item in reversed(self._items):
                if item.get("role") == "assistant":
                    item["interrupted"] = True
                    break
            return

    # ── Internals ───────────────────────────────────────────────────────

    def _append_role(
        self,
        role: str,
        text: str | None,
        timestamp: Any,
        *,
        transcript_confidence: float | None = None,
    ) -> None:
        if not text:
            return
        text = text.strip() if role == "user" else text
        if not text:
            return
        if self._cur_role != role:
            self._flush_current()
            self._cur_role = role
            # TranscriptionFrame carries a timestamp; TTSTextFrame does not,
            # so for assistant turns fall back to "now" so the dashboard has
            # a sensible chronological anchor.
            self._cur_first_ts = timestamp or datetime.now(timezone.utc)
            self._cur_created_at = self._created_at_for(role, timestamp)
            self._cur_transcript_confidence = None
            self._cur_metrics = self._consume_pending_metrics(role)
        elif self._pending_metrics.get(role):
            self._cur_metrics.update(self._consume_pending_metrics(role))
        if role == "user" and transcript_confidence is not None:
            self._cur_transcript_confidence = transcript_confidence
        self._cur_parts.append(text)

    def _metric_seen(self, metric: Any, data: FramePushed) -> bool:
        value = getattr(metric, "value", None)
        if hasattr(value, "model_dump"):
            value_key = tuple(sorted(value.model_dump().items()))
        elif hasattr(value, "__dict__"):
            value_key = tuple(sorted(value.__dict__.items()))
        else:
            value_key = value
        key = (
            type(metric).__name__,
            getattr(metric, "processor", None),
            getattr(metric, "model", None),
            value_key,
            getattr(data, "timestamp", None),
        )
        if key in self._seen_metric_payloads:
            return True
        self._seen_metric_payloads.add(key)
        return False

    def _text_frame_seen(self, frame: Any) -> bool:
        if any(seen is frame for seen in self._seen_text_frames):
            return True
        self._seen_text_frames.append(frame)
        return False

    def _on_metric(self, metric: Any, data: FramePushed) -> None:
        if self._metric_seen(metric, data):
            return

        kind = _classify_processor(getattr(metric, "processor", ""))

        # Pipecat MetricsFrames are emitted asynchronously from the
        # subsystem they describe — TTS/LLM metrics for an assistant turn
        # often arrive AFTER the user has started speaking the next turn,
        # so ``self._cur_role`` may already be "user" when the metric lands.
        # Attribute by metric kind (LLM/TTS → assistant, STT → user) and
        # walk back to the most recent matching turn.
        if kind in ("llm", "tts"):
            target = (
                self._pending_metrics["assistant"]
                if self._cur_role == "user"
                else self._target_for("assistant")
            )
        elif kind == "stt":
            target = (
                self._pending_metrics["user"]
                if self._cur_role == "assistant"
                else self._target_for("user")
            )
        else:
            target = self._cur_metrics

        metadata = _model_metadata(metric, kind)
        if metadata and kind in ("stt", "llm", "tts"):
            existing = target.setdefault(f"{kind}_metadata", {})
            for key, value in metadata.items():
                if value not in (None, ""):
                    existing.setdefault(key, value)

        if isinstance(metric, LLMUsageMetricsData):
            value = metric.value
            target["llm_prompt_tokens"] = (
                target.get("llm_prompt_tokens", 0) + value.prompt_tokens
            )
            target["llm_completion_tokens"] = (
                target.get("llm_completion_tokens", 0) + value.completion_tokens
            )
            target["llm_total_tokens"] = (
                target.get("llm_total_tokens", 0) + value.total_tokens
            )
            target.setdefault("llm_provider", metric.processor)
            if metric.model:
                target.setdefault("llm_model", metric.model)
            self._usage.append({
                "type": "llm_usage",
                "provider": _provider_from_processor(metric.processor),
                "model": metric.model,
                "input_tokens": value.prompt_tokens,
                "output_tokens": value.completion_tokens,
                "total_tokens": value.total_tokens,
                "cache_read_input_tokens": value.cache_read_input_tokens,
                "cache_creation_input_tokens": value.cache_creation_input_tokens,
                "reasoning_tokens": value.reasoning_tokens,
            })
        elif isinstance(metric, TTSUsageMetricsData):
            target["tts_characters"] = (
                target.get("tts_characters", 0) + metric.value
            )
            target.setdefault("tts_provider", metric.processor)
            if metric.model:
                target.setdefault("tts_model", metric.model)
            self._usage.append({
                "type": "tts_usage",
                "provider": _provider_from_processor(metric.processor),
                "model": metric.model,
                "characters_count": metric.value,
            })
        elif isinstance(metric, TTFBMetricsData):
            # Pipecat emits zero-valued TTFB frames at lifecycle points; skip
            # them so the per-turn metrics aren't overwritten with zeros and
            # the timing_metrics event isn't full of noise.
            if metric.value <= 0:
                return
            # Map TTFB onto livekit-style turn metric keys so the dashboard
            # (llm_node_ttft, tts_node_ttfb) populates.
            if kind == "llm":
                target.setdefault("llm_node_ttft", metric.value)
                target.setdefault("llm_ttft_ms", round(metric.value * 1000))
            elif kind == "tts":
                target.setdefault("tts_node_ttfb", metric.value)
                target.setdefault("tts_ttfb_ms", round(metric.value * 1000))
            elif kind == "stt":
                target.setdefault("transcription_delay", metric.value)
                target.setdefault("stt_delay_ms", round(metric.value * 1000))
            self._timing.append({
                "kind": "ttfb",
                "processor": metric.processor,
                "model": metric.model,
                "value": metric.value,
            })
        elif isinstance(metric, ProcessingMetricsData):
            if metric.value <= 0:
                return
            if kind == "stt":
                target.setdefault("transcription_delay", metric.value)
                target.setdefault("stt_delay_ms", round(metric.value * 1000))
            self._timing.append({
                "kind": "processing",
                "processor": metric.processor,
                "model": metric.model,
                "value": metric.value,
            })
        elif isinstance(metric, TextAggregationMetricsData):
            if metric.value <= 0:
                return
            if kind == "tts":
                target.setdefault("tts_text_aggregation", metric.value)
            self._timing.append({
                "kind": "text_aggregation",
                "processor": metric.processor,
                "model": metric.model,
                "value": metric.value,
            })
        elif isinstance(metric, (TurnMetricsData, SmartTurnMetricsData)):
            value = getattr(metric, "e2e_processing_time_ms", None)
            if value is not None and value > 0:
                user_target = self._target_for("user")
                user_target.setdefault("end_of_turn_delay", value / 1000)
                user_target.setdefault("turn_decision_ms", round(value))
                user_target.setdefault("on_user_turn_completed_delay", value / 1000)
            self._timing.append({
                "kind": "turn",
                "processor": metric.processor,
                "model": metric.model,
                "is_complete": getattr(metric, "is_complete", None),
                "probability": getattr(metric, "probability", None),
                "e2e_processing_time_ms": value,
            })

    def _consume_pending_metrics(self, role: str) -> dict:
        metrics = dict(self._pending_metrics.get(role, {}))
        self._pending_metrics[role] = {}
        return metrics

    def _mark_speaking(self, role: str, field: str) -> float:
        ts = time.time()
        target = self._target_for(role, prefer_pending=True)
        target.setdefault(field, ts)
        return ts

    def _created_at_for(self, role: str, timestamp: Any) -> float:
        parsed = _unix_seconds(timestamp)
        if parsed is not None:
            return parsed
        if self._cur_role == role:
            value = self._cur_metrics.get("started_speaking_at")
            if isinstance(value, (int, float)):
                return float(value)
        pending = self._pending_metrics.get(role) or {}
        value = pending.get("started_speaking_at")
        if isinstance(value, (int, float)):
            return float(value)
        return time.time()

    def _set_user_state(self, new_state: str, created_at: float) -> None:
        old_state = self._user_state
        if old_state == new_state:
            return
        self._user_state = new_state
        self._events.append({
            "type": "user_state_changed",
            "old_state": old_state,
            "new_state": new_state,
            "created_at": created_at,
        })

    def _set_agent_state(self, new_state: str, created_at: float) -> None:
        old_state = self._agent_state
        if old_state == new_state:
            return
        self._agent_state = new_state
        self._events.append({
            "type": "agent_state_changed",
            "old_state": old_state,
            "new_state": new_state,
            "created_at": created_at,
        })

    def _append_item(self, item: dict, created_at: float) -> None:
        self._items.append(item)
        event_item = dict(item)
        if isinstance(event_item.get("metrics"), dict):
            event_item["metrics"] = dict(event_item["metrics"])
        self._events.append({
            "type": "conversation_item_added",
            "item": event_item,
            "created_at": created_at,
        })

    def _append_function_call(self, frame: FunctionCallInProgressFrame) -> None:
        self._flush_current()
        created_at = time.time()
        call_id = getattr(frame, "tool_call_id", None) or f"call_{uuid.uuid4().hex[:12]}"
        item = {
            "id": f"item_{uuid.uuid4().hex[:12]}/fnc_0",
            "type": "function_call",
            "call_id": call_id,
            "arguments": _stringify_jsonish(getattr(frame, "arguments", {})),
            "name": getattr(frame, "function_name", None) or "unknown",
            "created_at": created_at,
            "extra": {},
        }
        group_id = getattr(frame, "group_id", None)
        if group_id is not None:
            item["group_id"] = group_id
        self._function_calls[call_id] = item
        self._append_item(item, created_at)

    def _append_function_result(self, frame: FunctionCallResultFrame) -> None:
        self._flush_current()
        created_at = time.time()
        call_id = getattr(frame, "tool_call_id", None) or f"call_{uuid.uuid4().hex[:12]}"
        item = {
            "id": f"item_{uuid.uuid4().hex[:12]}",
            "type": "function_call_output",
            "name": getattr(frame, "function_name", None) or "unknown",
            "call_id": call_id,
            "output": _stringify_jsonish(getattr(frame, "result", None)),
            "is_error": False,
            "created_at": created_at,
        }
        self._append_item(item, created_at)
        call = self._function_calls.get(call_id)
        if call:
            self._events.append({
                "type": "function_tools_executed",
                "function_calls": [call],
                "function_call_outputs": [item],
                "created_at": created_at,
            })

    def _append_function_cancel(self, frame: FunctionCallCancelFrame) -> None:
        self._flush_current()
        created_at = time.time()
        call_id = getattr(frame, "tool_call_id", None) or f"call_{uuid.uuid4().hex[:12]}"
        item = {
            "id": f"item_{uuid.uuid4().hex[:12]}",
            "type": "function_call_output",
            "name": getattr(frame, "function_name", None) or "unknown",
            "call_id": call_id,
            "output": "cancelled",
            "is_error": True,
            "created_at": created_at,
        }
        self._append_item(item, created_at)

    def _target_for(self, role: str, *, prefer_pending: bool = False) -> dict:
        """Pick the metrics dict for a given role.

        Prefers the in-progress turn if it matches; otherwise walks the
        already-flushed items backward to find the most recent same-role
        turn. Falls back to pending metrics so nothing is dropped before a
        matching transcript or TTS text frame opens the turn.
        """
        if self._cur_role == role:
            return self._cur_metrics
        pending = self._pending_metrics.setdefault(role, {})
        if prefer_pending or pending:
            return pending
        for item in reversed(self._items):
            if item.get("role") == role:
                return item["metrics"]
        return pending

    def _flush_current(self) -> None:
        if self._cur_role is None or not self._cur_parts:
            self._cur_role = None
            self._cur_parts = []
            self._cur_first_ts = None
            self._cur_created_at = None
            self._cur_transcript_confidence = None
            self._cur_metrics = {}
            return
        text = " ".join(p.strip() for p in self._cur_parts if p and p.strip())
        if text:
            metrics = dict(self._cur_metrics)
            interrupted = bool(metrics.pop("interrupted", False))
            created_at = self._cur_created_at or self._created_at_for(
                self._cur_role, self._cur_first_ts
            )
            item = {
                "id": f"item_{uuid.uuid4().hex[:12]}",
                "type": "message",
                "role": self._cur_role,
                "content": text,
                "interrupted": interrupted,
                "extra": {},
                "timestamp": _ts(self._cur_first_ts),
                "metrics": metrics,
                "created_at": created_at,
            }
            if self._cur_role == "user" and self._cur_transcript_confidence is not None:
                item["transcript_confidence"] = self._cur_transcript_confidence
            self._append_item(item, created_at)
        self._cur_role = None
        self._cur_parts = []
        self._cur_first_ts = None
        self._cur_created_at = None
        self._cur_transcript_confidence = None
        self._cur_metrics = {}

    # ── Report assembly ─────────────────────────────────────────────────

    def to_report_dict(self, *, session_id: str) -> dict:
        """Assemble the chat-history JSON payload for the obs upload.

        Matches the livekit shape — see
        ``sip/livekit/observability.py:_build_report_dict``.
        """
        self._flush_current()

        usage = [
            {k: v for k, v in entry.items() if v not in (0, None, "")}
            for entry in self._usage
        ] or None

        events = list(self._events)
        if self._timing:
            events.append({
                "type": "timing_metrics",
                "data": list(self._timing),
                "created_at": time.time(),
            })
        events = [
            event for _, event in sorted(
                enumerate(events),
                key=lambda indexed: (
                    _event_time(indexed[1]) is None,
                    _event_time(indexed[1]) or 0.0,
                    indexed[0],
                ),
            )
        ]

        return {
            "job_id": str(session_id),
            "room_id": str(session_id),
            "room": str(session_id),
            "events": events,
            "chat_history": {"items": list(self._items)},
            "options": dict(self._options),
            "timestamp": time.time(),
            "usage": usage,
        }
