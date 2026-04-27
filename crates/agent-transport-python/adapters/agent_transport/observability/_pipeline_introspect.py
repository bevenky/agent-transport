"""Walk a pipecat pipeline and capture a JSON-friendly config snapshot.

Called from ``transport.input().start(StartFrame)`` — by that point every
processor has been linked into the pipeline, so following ``_next`` from
the input transport yields the full chain (until the PipelineSink).
"""

from __future__ import annotations

from typing import Any


_BLOCKLIST = {
    "api_key", "auth_token", "secret", "password", "credential", "token",
}


def _safe_value(v: Any) -> Any:
    """JSON-friendly scalars only."""
    if isinstance(v, (str, int, float, bool, type(None))):
        return v
    return None


def _safe_attr_name(name: str) -> bool:
    if name.startswith("_"):
        return False
    low = name.lower()
    return not any(b in low for b in _BLOCKLIST)


def _processor_config(processor: Any) -> dict:
    """JSON snapshot of a FrameProcessor's public scalar config."""
    cfg: dict = {"class": type(processor).__name__}
    name = getattr(processor, "_name", None) or getattr(processor, "name", None)
    if isinstance(name, str):
        cfg["name"] = name
    # Keep this simple: dump scalar attributes from the processor's __dict__
    # only (no dir() walk — properties on base classes often raise when
    # accessed pre-start). Then surface ``_settings`` / ``settings`` if present.
    try:
        items = list(processor.__dict__.items())
    except Exception:
        items = []
    for k, v in items:
        if not _safe_attr_name(k):
            continue
        scalar = _safe_value(v)
        if scalar is not None:
            cfg.setdefault(k, scalar)
    settings = getattr(processor, "_settings", None) or getattr(processor, "settings", None)
    if settings is not None and not callable(settings):
        sdict: dict = {}
        try:
            sitems = list(settings.__dict__.items())
        except Exception:
            try:
                sitems = list(vars(settings).items())
            except TypeError:
                sitems = []
        for k, v in sitems:
            if not _safe_attr_name(k):
                continue
            scalar = _safe_value(v)
            if scalar is not None:
                sdict[k] = scalar
        if sdict:
            cfg["settings"] = sdict
    return cfg


def _walk_processors(start: Any) -> list:
    """Walk the linked-list of FrameProcessors starting at ``start``."""
    out = []
    seen = set()
    cur = start
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        out.append(cur)
        cur = getattr(cur, "_next", None)
    return out


def populate_options_from_pipeline(recorder, transport_input, frame) -> None:
    """Populate the recorder's ``options`` with PipelineParams + pipeline shape."""
    options = recorder._options
    for attr in (
        "audio_in_sample_rate",
        "audio_out_sample_rate",
        "enable_metrics",
        "enable_tracing",
        "enable_usage_metrics",
        "report_only_initial_ttfb",
        "allow_interruptions",
    ):
        v = getattr(frame, attr, None)
        if v is None:
            continue
        scalar = _safe_value(v)
        if scalar is None and v is not None:
            continue
        options.setdefault(attr, scalar)
    processors_cfg = []
    for p in _walk_processors(transport_input):
        try:
            processors_cfg.append(_processor_config(p))
        except Exception:
            processors_cfg.append({"class": type(p).__name__, "error": "introspection_failed"})
    if processors_cfg:
        options["pipeline"] = processors_cfg


def enable_start_frame_metrics(frame) -> None:
    """Enable Pipecat metrics required for the observability report.

    Pipecat only emits ``MetricsFrame`` payloads when these flags are true on
    the ``StartFrame`` generated from ``PipelineParams``. The transport input is
    the first pipeline processor, so mutating the frame here reaches all
    downstream STT/LLM/TTS processors without requiring user-side wiring.
    """
    if hasattr(frame, "enable_metrics"):
        frame.enable_metrics = True
    if hasattr(frame, "enable_usage_metrics"):
        frame.enable_usage_metrics = True
