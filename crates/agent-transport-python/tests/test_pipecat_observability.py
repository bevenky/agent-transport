"""Tests for the pipecat observability module.

Covers:
- Env-var gating (AGENT_OBSERVABILITY_URL)
- Basic-auth header construction
- SessionRecorder (BaseObserver) capture of transcripts + usage + timing
- to_report_dict shape (livekit-compatible chat_history.items)
- Multipart upload assembly
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from unittest.mock import patch

import pytest

from agent_transport.observability import (
    SessionRecorder,
    _build_auth_header,
    _get_observability_url,
    enable_start_frame_metrics,
    upload_session_report,
)
from agent_transport.observability._env import _recording_dir

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    MetricsFrame,
    StartFrame,
    TranscriptionFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import (
    LLMTokenUsage,
    LLMUsageMetricsData,
    ProcessingMetricsData,
    TTFBMetricsData,
    TurnMetricsData,
    TTSUsageMetricsData,
)


@dataclass
class _FramePushed:
    """Stand-in for pipecat.observers.base_observer.FramePushed."""
    frame: Any
    timestamp: int = 0


# ── Env gating ──────────────────────────────────────────────────────────────


def test_get_observability_url_unset(monkeypatch):
    monkeypatch.delenv("AGENT_OBSERVABILITY_URL", raising=False)
    assert _get_observability_url() is None


def test_get_observability_url_empty_string(monkeypatch):
    monkeypatch.setenv("AGENT_OBSERVABILITY_URL", "")
    assert _get_observability_url() is None


def test_get_observability_url_set(monkeypatch):
    monkeypatch.setenv("AGENT_OBSERVABILITY_URL", "https://obs.example.com")
    assert _get_observability_url() == "https://obs.example.com"


def test_recording_dir_default(monkeypatch):
    monkeypatch.delenv("RECORDING_DIR", raising=False)
    assert _recording_dir() == "/tmp/agent-sessions"


def test_recording_dir_override(monkeypatch):
    monkeypatch.setenv("RECORDING_DIR", "/var/tmp/recordings")
    assert _recording_dir() == "/var/tmp/recordings"


def test_build_auth_header_unset(monkeypatch):
    monkeypatch.delenv("AGENT_OBSERVABILITY_USER", raising=False)
    monkeypatch.delenv("AGENT_OBSERVABILITY_PASS", raising=False)
    assert _build_auth_header() == {}


def test_build_auth_header_partial(monkeypatch):
    monkeypatch.setenv("AGENT_OBSERVABILITY_USER", "alice")
    monkeypatch.delenv("AGENT_OBSERVABILITY_PASS", raising=False)
    assert _build_auth_header() == {}


def test_build_auth_header_set(monkeypatch):
    monkeypatch.setenv("AGENT_OBSERVABILITY_USER", "alice")
    monkeypatch.setenv("AGENT_OBSERVABILITY_PASS", "s3cret")
    expected = "Basic " + base64.b64encode(b"alice:s3cret").decode()
    assert _build_auth_header() == {"Authorization": expected}


# ── SessionRecorder via on_push_frame ───────────────────────────────────────


@pytest.mark.asyncio
async def test_recorder_captures_transcription_and_tts_text():
    """Real-world flow: STT pushes TranscriptionFrame, TTS pushes TTSTextFrame.
    Both should land as livekit-shape ``items`` with ``type=message``."""
    rec = SessionRecorder()

    user_ts = datetime(2026, 4, 27, 12, 0, 0).isoformat()
    await rec.on_push_frame(_FramePushed(
        frame=TranscriptionFrame(text="tell me a joke", user_id="u1", timestamp=user_ts)
    ))
    await rec.on_push_frame(_FramePushed(
        frame=TTSTextFrame(text="Why did the chicken", aggregated_by="word")
    ))
    await rec.on_push_frame(_FramePushed(
        frame=TTSTextFrame(text=" cross the road?", aggregated_by="word")
    ))

    report = rec.to_report_dict(session_id="sess-1")
    items = report["chat_history"]["items"]
    assert len(items) == 2
    assert items[0]["type"] == "message"
    assert items[0]["role"] == "user"
    assert items[0]["content"] == "tell me a joke"
    assert items[1]["type"] == "message"
    assert items[1]["role"] == "assistant"
    assert items[1]["content"] == "Why did the chicken cross the road?"
    # Both items must have an id so livekit-style consumers can reference them
    assert items[0]["id"] and items[1]["id"]


@pytest.mark.asyncio
async def test_recorder_deduplicates_same_text_frame_seen_multiple_times():
    """Pipecat observers see one frame as it passes multiple processors."""
    rec = SessionRecorder()
    user_frame = TranscriptionFrame(
        text="hello",
        user_id="u1",
        timestamp=datetime.utcnow().isoformat(),
    )
    assistant_frame = TTSTextFrame(text="Hi there", aggregated_by="word")

    await rec.on_push_frame(_FramePushed(frame=user_frame))
    await rec.on_push_frame(_FramePushed(frame=user_frame))
    await rec.on_push_frame(_FramePushed(frame=assistant_frame))
    await rec.on_push_frame(_FramePushed(frame=assistant_frame))

    items = rec.to_report_dict(session_id="sess-1")["chat_history"]["items"]
    assert [i["content"] for i in items] == ["hello", "Hi there"]


@pytest.mark.asyncio
async def test_recorder_skips_interim_transcriptions():
    """Interim STT results inherit from TranscriptionFrame but should be ignored."""
    rec = SessionRecorder()
    await rec.on_push_frame(_FramePushed(
        frame=InterimTranscriptionFrame(text="tell me a", user_id="u1", timestamp=datetime.utcnow().isoformat())
    ))
    report = rec.to_report_dict(session_id="sess-1")
    assert report["chat_history"]["items"] == []


@pytest.mark.asyncio
async def test_recorder_attaches_metrics_to_current_turn():
    """LLM/TTS usage and TTFB should land on the in-progress assistant turn."""
    rec = SessionRecorder()
    # User turn first
    await rec.on_push_frame(_FramePushed(
        frame=TranscriptionFrame(text="hello", user_id="u1", timestamp=datetime.utcnow().isoformat())
    ))
    # Assistant turn opens; metrics arrive while assembling fragments
    await rec.on_push_frame(_FramePushed(frame=TTSTextFrame(text="Hi there", aggregated_by="word")))
    await rec.on_push_frame(_FramePushed(frame=MetricsFrame(data=[
        TTFBMetricsData(processor="OpenAILLMService", model="gpt-4", value=0.5),
        TTFBMetricsData(processor="OpenAITTSService", model="tts-1", value=0.2),
        LLMUsageMetricsData(
            processor="OpenAILLMService", model="gpt-4",
            value=LLMTokenUsage(prompt_tokens=10, completion_tokens=4, total_tokens=14),
        ),
        TTSUsageMetricsData(processor="OpenAITTSService", model="tts-1", value=8),
    ])))

    report = rec.to_report_dict(session_id="sess-1")
    items = report["chat_history"]["items"]
    assert len(items) == 2
    assistant = items[1]
    metrics = assistant["metrics"]
    assert metrics["llm_prompt_tokens"] == 10
    assert metrics["llm_completion_tokens"] == 4
    assert metrics["llm_total_tokens"] == 14
    assert metrics["tts_characters"] == 8
    assert metrics["llm_metadata"] == {
        "model_name": "gpt-4",
        "model_provider": "OpenAI",
    }
    assert metrics["tts_metadata"] == {
        "model_name": "tts-1",
        "model_provider": "OpenAI",
    }
    # TTFB metrics get the livekit-compatible names so the dashboard renders them
    assert metrics["llm_node_ttft"] == 0.5
    assert metrics["tts_node_ttfb"] == 0.2
    assert metrics["llm_ttft_ms"] == 500
    assert metrics["tts_ttfb_ms"] == 200

    usage = report["usage"]
    assert usage == [
        {
            "type": "llm_usage",
            "provider": "OpenAI",
            "model": "gpt-4",
            "input_tokens": 10,
            "output_tokens": 4,
            "total_tokens": 14,
        },
        {
            "type": "tts_usage",
            "provider": "OpenAI",
            "model": "tts-1",
            "characters_count": 8,
        },
    ]


@pytest.mark.asyncio
async def test_recorder_classifies_stt_before_tts():
    """DeepgramSTTService contains the substring 'tts' across STTService."""
    rec = SessionRecorder()

    await rec.on_push_frame(_FramePushed(frame=UserStartedSpeakingFrame()))
    await rec.on_push_frame(_FramePushed(frame=UserStoppedSpeakingFrame()))
    await rec.on_push_frame(_FramePushed(
        frame=TranscriptionFrame(text="hello", user_id="u1", timestamp=datetime.utcnow().isoformat())
    ))
    await rec.on_push_frame(_FramePushed(frame=MetricsFrame(data=[
        TTFBMetricsData(processor="DeepgramSTTService#0", model="nova-3", value=0.4),
        TTFBMetricsData(processor="OpenAITTSService#0", model="tts-1", value=0.2),
    ])))
    await rec.on_push_frame(_FramePushed(frame=TTSTextFrame(text="Hi", aggregated_by="word")))

    items = rec.to_report_dict(session_id="sess-1")["chat_history"]["items"]
    user, assistant = items
    assert user["metrics"]["transcription_delay"] == 0.4
    assert user["metrics"]["stt_metadata"] == {
        "model_name": "nova-3",
        "model_provider": "Deepgram",
    }
    assert "tts_node_ttfb" not in user["metrics"]
    assert assistant["metrics"]["tts_node_ttfb"] == 0.2


@pytest.mark.asyncio
async def test_recorder_uses_stt_processing_as_delay_fallback_and_merges_model():
    rec = SessionRecorder()

    await rec.on_push_frame(_FramePushed(
        frame=TranscriptionFrame(text="hello", user_id="u1", timestamp=datetime.utcnow().isoformat())
    ))
    await rec.on_push_frame(_FramePushed(frame=MetricsFrame(data=[
        TTFBMetricsData(processor="DeepgramSTTService#0", model=None, value=0),
        ProcessingMetricsData(
            processor="DeepgramSTTService#0",
            model="nova-3-general",
            value=0.0012,
        ),
    ])))

    user = rec.to_report_dict(session_id="sess-1")["chat_history"]["items"][0]
    assert user["metrics"]["transcription_delay"] == 0.0012
    assert user["metrics"]["stt_delay_ms"] == 1
    assert user["metrics"]["stt_metadata"] == {
        "model_name": "nova-3-general",
        "model_provider": "Deepgram",
    }


@pytest.mark.asyncio
async def test_recorder_captures_speaking_timestamps_and_e2e_latency():
    rec = SessionRecorder()

    with patch("agent_transport.observability._recorder.time.time") as time_mock:
        time_mock.side_effect = [100.0, 101.25, 103.75, 106.0]
        await rec.on_push_frame(_FramePushed(frame=UserStartedSpeakingFrame()))
        await rec.on_push_frame(_FramePushed(frame=UserStoppedSpeakingFrame()))
        await rec.on_push_frame(_FramePushed(
            frame=TranscriptionFrame(text="hello", user_id="u1", timestamp=datetime.utcnow().isoformat())
        ))
        await rec.on_push_frame(_FramePushed(frame=BotStartedSpeakingFrame()))
        await rec.on_push_frame(_FramePushed(frame=TTSTextFrame(text="Hi", aggregated_by="word")))
        await rec.on_push_frame(_FramePushed(frame=BotStoppedSpeakingFrame()))

    user, assistant = rec.to_report_dict(session_id="sess-1")["chat_history"]["items"]
    assert user["metrics"]["started_speaking_at"] == 100.0
    assert user["metrics"]["stopped_speaking_at"] == 101.25
    assert assistant["metrics"]["started_speaking_at"] == 103.75
    assert assistant["metrics"]["stopped_speaking_at"] == 106.0
    assert assistant["metrics"]["e2e_latency"] == 2.5


@pytest.mark.asyncio
async def test_recorder_emits_livekit_style_state_and_conversation_events():
    rec = SessionRecorder()

    with patch("agent_transport.observability._recorder.time.time") as time_mock:
        time_mock.side_effect = [100.0, 101.0, 103.0, 104.0]
        await rec.on_push_frame(_FramePushed(frame=UserStartedSpeakingFrame()))
        await rec.on_push_frame(_FramePushed(frame=UserStoppedSpeakingFrame()))
        await rec.on_push_frame(_FramePushed(
            frame=TranscriptionFrame(
                text="hello",
                user_id="caller",
                timestamp="1970-01-01T00:01:41+00:00",
            )
        ))
        await rec.on_push_frame(_FramePushed(frame=BotStartedSpeakingFrame()))
        await rec.on_push_frame(_FramePushed(frame=TTSTextFrame(text="Hi", aggregated_by="word")))
        await rec.on_push_frame(_FramePushed(frame=BotStoppedSpeakingFrame()))

    events = rec.to_report_dict(session_id="sess-1")["events"]
    event_types = [e["type"] for e in events]
    assert "user_state_changed" in event_types
    assert "agent_state_changed" in event_types
    assert "user_input_transcribed" in event_types
    assert event_types.count("conversation_item_added") == 2

    conversation_events = [
        e for e in events if e["type"] == "conversation_item_added"
    ]
    assert conversation_events[0]["item"]["role"] == "user"
    assert conversation_events[1]["item"]["role"] == "assistant"


@pytest.mark.asyncio
async def test_recorder_captures_transcript_confidence_from_stt_result():
    rec = SessionRecorder()

    await rec.on_push_frame(_FramePushed(
        frame=TranscriptionFrame(
            text="hello",
            user_id="u1",
            timestamp=datetime.utcnow().isoformat(),
            result={
                "channel": {
                    "alternatives": [
                        {"transcript": "hello", "confidence": 0.86}
                    ]
                }
            },
        )
    ))

    user = rec.to_report_dict(session_id="sess-1")["chat_history"]["items"][0]
    assert user["transcript_confidence"] == 0.86


@pytest.mark.asyncio
async def test_recorder_maps_turn_metrics_to_user_turn_delay():
    rec = SessionRecorder()
    await rec.on_push_frame(_FramePushed(
        frame=TranscriptionFrame(text="hello", user_id="u1", timestamp=datetime.utcnow().isoformat())
    ))
    await rec.on_push_frame(_FramePushed(frame=MetricsFrame(data=[
        TurnMetricsData(
            processor="SmartTurnAnalyzer#0",
            model="smart-turn",
            is_complete=True,
            probability=0.9,
            e2e_processing_time_ms=125.5,
        )
    ])))
    await rec.on_push_frame(_FramePushed(frame=TTSTextFrame(text="Hi", aggregated_by="word")))

    user = rec.to_report_dict(session_id="sess-1")["chat_history"]["items"][0]
    assert user["metrics"]["end_of_turn_delay"] == 0.1255
    assert user["metrics"]["turn_decision_ms"] == 126
    assert user["metrics"]["on_user_turn_completed_delay"] == 0.1255


@pytest.mark.asyncio
async def test_recorder_marks_assistant_interrupted():
    rec = SessionRecorder()
    await rec.on_push_frame(_FramePushed(frame=TTSTextFrame(text="Hi there", aggregated_by="word")))
    await rec.on_push_frame(_FramePushed(frame=InterruptionFrame()))
    report = rec.to_report_dict(session_id="sess-1")
    items = report["chat_history"]["items"]
    assert len(items) == 1
    assert items[0]["interrupted"] is True


@pytest.mark.asyncio
async def test_recorder_ignores_unrelated_frames():
    @dataclass
    class _Other:
        name: str = "other"

    rec = SessionRecorder()
    await rec.on_push_frame(_FramePushed(frame=_Other()))
    report = rec.to_report_dict(session_id="sess-1")
    assert report["chat_history"]["items"] == []
    assert report["usage"] is None


def test_recorder_record_event_appended():
    rec = SessionRecorder()
    rec.record_event({"type": "custom", "payload": {"k": "v"}})
    report = rec.to_report_dict(session_id="sess-1")
    assert {"type": "custom", "payload": {"k": "v"}} in report["events"]


def test_recorder_sorts_events_by_created_at():
    rec = SessionRecorder()
    rec.record_event({"type": "late", "created_at": 3.0})
    rec.record_event({"type": "untimed"})
    rec.record_event({"type": "early", "created_at": 1.0})
    rec.record_event({"type": "middle", "created_at": "1970-01-01T00:00:02Z"})

    events = rec.to_report_dict(session_id="sess-1")["events"]

    assert [event["type"] for event in events] == [
        "early",
        "middle",
        "late",
        "untimed",
    ]


@pytest.mark.asyncio
async def test_recorder_timing_metrics_event_has_created_at():
    rec = SessionRecorder()
    await rec.on_push_frame(_FramePushed(frame=MetricsFrame(data=[
        ProcessingMetricsData(
            processor="OpenAILLMService#0",
            model="gpt-4.1",
            value=0.2,
        ),
    ])))

    events = rec.to_report_dict(session_id="sess-1")["events"]
    timing = [event for event in events if event["type"] == "timing_metrics"]
    assert timing
    assert isinstance(timing[0]["created_at"], float)


@pytest.mark.asyncio
async def test_recorder_captures_function_call_items_for_tool_turns():
    rec = SessionRecorder()

    await rec.on_push_frame(_FramePushed(
        frame=TranscriptionFrame(
            text="weather in Chennai",
            user_id="u1",
            timestamp=datetime.utcnow().isoformat(),
        )
    ))
    await rec.on_push_frame(_FramePushed(
        frame=FunctionCallInProgressFrame(
            function_name="lookup_weather",
            tool_call_id="call-1",
            arguments={"location": "Chennai"},
        )
    ))
    await rec.on_push_frame(_FramePushed(
        frame=FunctionCallResultFrame(
            function_name="lookup_weather",
            tool_call_id="call-1",
            arguments={"location": "Chennai"},
            result={"temperature": 72},
        )
    ))
    await rec.on_push_frame(_FramePushed(
        frame=TTSTextFrame(text="It is sunny.", aggregated_by="word")
    ))

    report = rec.to_report_dict(session_id="sess-1")
    items = report["chat_history"]["items"]
    assert [item["type"] for item in items] == [
        "message",
        "function_call",
        "function_call_output",
        "message",
    ]
    assert items[1]["name"] == "lookup_weather"
    assert json.loads(items[1]["arguments"]) == {"location": "Chennai"}
    assert items[2]["call_id"] == "call-1"
    assert json.loads(items[2]["output"]) == {"temperature": 72}
    assert any(e["type"] == "function_tools_executed" for e in report["events"])


def test_recorder_to_report_dict_shape():
    """Top-level keys mirror the livekit version exactly."""
    rec = SessionRecorder(options={"vad_threshold": 0.5})
    report = rec.to_report_dict(session_id="sess-42")
    assert set(report.keys()) == {
        "job_id", "room_id", "room",
        "events", "chat_history", "options",
        "timestamp", "usage",
    }
    assert report["job_id"] == "sess-42"
    assert report["chat_history"] == {"items": []}
    assert report["usage"] is None
    assert isinstance(report["timestamp"], float)

def test_enable_start_frame_metrics_turns_on_pipecat_metric_flags():
    frame = StartFrame(enable_metrics=False, enable_usage_metrics=False)

    enable_start_frame_metrics(frame)

    assert frame.enable_metrics is True
    assert frame.enable_usage_metrics is True


# ── upload_session_report ───────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, status: int = 200):
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeClientSession:
    last_url: str | None = None
    last_headers: dict | None = None
    last_data = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, *, data, headers):
        type(self).last_url = url
        type(self).last_headers = headers
        type(self).last_data = data
        return _FakeResp(status=200)


@pytest.mark.asyncio
async def test_upload_posts_to_correct_endpoint(monkeypatch):
    monkeypatch.delenv("AGENT_OBSERVABILITY_USER", raising=False)
    monkeypatch.delenv("AGENT_OBSERVABILITY_PASS", raising=False)

    rec = SessionRecorder()
    rec.record_event({"type": "lifecycle", "name": "started"})

    with patch("aiohttp.ClientSession", _FakeClientSession):
        await upload_session_report(
            recorder=rec,
            session_id="sess-99",
            obs_url="https://obs.example.com",
            recording_path=None,
            recording_started_at=None,
            transport="sip",
        )

    assert _FakeClientSession.last_url == "https://obs.example.com/observability/recordings/v0"
    headers = _FakeClientSession.last_headers
    assert "Content-Type" in headers
    assert headers["Content-Type"].startswith("multipart/form-data")
    assert "Authorization" not in headers


@pytest.mark.asyncio
async def test_upload_includes_basic_auth_when_configured(monkeypatch):
    monkeypatch.setenv("AGENT_OBSERVABILITY_USER", "alice")
    monkeypatch.setenv("AGENT_OBSERVABILITY_PASS", "s3cret")

    rec = SessionRecorder()

    with patch("aiohttp.ClientSession", _FakeClientSession):
        await upload_session_report(
            recorder=rec,
            session_id="sess-99",
            obs_url="https://obs.example.com",
            transport="audio_stream",
        )

    headers = _FakeClientSession.last_headers
    expected = "Basic " + base64.b64encode(b"alice:s3cret").decode()
    assert headers["Authorization"] == expected


@pytest.mark.asyncio
async def test_upload_skips_audio_when_no_recording():
    rec = SessionRecorder()

    captured = {}

    class _Capturing(_FakeClientSession):
        def post(self, url, *, data, headers):
            captured["data"] = data
            return _FakeResp()

    with patch("aiohttp.ClientSession", _Capturing):
        await upload_session_report(
            recorder=rec,
            session_id="sess-99",
            obs_url="https://obs.example.com",
            recording_path=None,
            transport="sip",
        )

    parts = [t[0] for t in captured["data"]]
    assert len(parts) == 2
    dispositions = [p.headers.get("Content-Disposition", "") for p in parts]
    assert any('name="header"' in d for d in dispositions)
    assert any('name="chat_history"' in d for d in dispositions)
    assert not any('name="audio"' in d for d in dispositions)


@pytest.mark.asyncio
async def test_upload_includes_audio_when_recording_present(tmp_path):
    rec = SessionRecorder()
    rec_path = tmp_path / "recording.ogg"
    rec_path.write_bytes(b"OggS\x00\x02fakeoggdata")

    captured = {}

    class _Capturing(_FakeClientSession):
        def post(self, url, *, data, headers):
            captured["data"] = data
            return _FakeResp()

    with patch("aiohttp.ClientSession", _Capturing):
        await upload_session_report(
            recorder=rec,
            session_id="sess-99",
            obs_url="https://obs.example.com",
            recording_path=str(rec_path),
            recording_started_at=1714000000.0,
            transport="sip",
        )

    parts = [t[0] for t in captured["data"]]
    assert len(parts) == 3
    audio_part = next(
        p for p in parts
        if 'name="audio"' in p.headers.get("Content-Disposition", "")
    )
    assert audio_part.headers["Content-Type"] == "audio/ogg"


@pytest.mark.asyncio
@pytest.mark.parametrize("account_id", [None, ""])
async def test_upload_omits_account_id_when_unset(account_id):
    rec = SessionRecorder()

    captured = {}

    class _Capturing(_FakeClientSession):
        def post(self, url, *, data, headers):
            parts = [t[0] for t in data]
            for p in parts:
                if 'name="header"' in p.headers.get("Content-Disposition", ""):
                    captured["header_part"] = p
            return _FakeResp()

    with patch("aiohttp.ClientSession", _Capturing):
        await upload_session_report(
            recorder=rec,
            session_id="sess-99",
            obs_url="https://obs.example.com",
            account_id=account_id,
            transport="sip",
        )

    payload = captured["header_part"]
    body = payload._value if hasattr(payload, "_value") else None
    assert body is not None
    decoded = json.loads(body if isinstance(body, str) else body.decode())
    assert decoded["room_tags"] == {}
    assert decoded["transport"] == "sip"
    assert decoded["session_id"] == "sess-99"


@pytest.mark.asyncio
async def test_upload_includes_explicit_account_id():
    rec = SessionRecorder()

    captured = {}

    class _Capturing(_FakeClientSession):
        def post(self, url, *, data, headers):
            parts = [t[0] for t in data]
            for p in parts:
                if 'name="header"' in p.headers.get("Content-Disposition", ""):
                    captured["header_part"] = p
            return _FakeResp()

    with patch("aiohttp.ClientSession", _Capturing):
        await upload_session_report(
            recorder=rec,
            session_id="sess-99",
            obs_url="https://obs.example.com",
            account_id="from-handler",
            transport="sip",
        )

    payload = captured["header_part"]
    body = payload._value if hasattr(payload, "_value") else None
    assert body is not None
    decoded = json.loads(body if isinstance(body, str) else body.decode())
    assert decoded["room_tags"] == {"account_id": "from-handler"}
    assert decoded["transport"] == "sip"
    assert decoded["session_id"] == "sess-99"
