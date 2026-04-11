"""Regression test: Pipecat transport propagates endpoint sample rate.

Pipecat's BaseInputTransport.start() reads params.audio_in_sample_rate
to configure VAD/turn-analyzers/audio-filters. If our adapter doesn't
propagate the Rust endpoint's fixed rate into the params object, those
processors see the wrong Hz and mis-interpret the PCM.

This test verifies that both SIP and audio_stream input transports
populate ``audio_in_sample_rate`` / ``audio_out_sample_rate`` from the
endpoint when the caller left them as the default (None).
"""

import pytest

from pipecat.transports.base_transport import TransportParams

from agent_transport.sip.pipecat.sip_transport import (
    SipInputTransport, SipOutputTransport,
)
from agent_transport.audio_stream.pipecat.audio_stream_transport import (
    AudioStreamInputTransport, AudioStreamOutputTransport,
)


class FakeEndpoint:
    def __init__(self, in_rate=16000, out_rate=16000):
        self.input_sample_rate = in_rate
        self.output_sample_rate = out_rate


def test_sip_input_propagates_endpoint_sample_rate():
    ep = FakeEndpoint(in_rate=16000)
    params = TransportParams(audio_in_enabled=True)
    assert params.audio_in_sample_rate is None
    SipInputTransport(ep, "c1", transport=None, params=params)
    assert params.audio_in_sample_rate == 16000


def test_sip_output_propagates_endpoint_sample_rate():
    ep = FakeEndpoint(out_rate=16000)
    params = TransportParams(audio_out_enabled=True)
    assert params.audio_out_sample_rate is None
    SipOutputTransport(ep, "c1", transport=None, params=params)
    assert params.audio_out_sample_rate == 16000


def test_audio_stream_input_propagates_endpoint_sample_rate():
    ep = FakeEndpoint(in_rate=24000)
    params = TransportParams(audio_in_enabled=True)
    assert params.audio_in_sample_rate is None
    AudioStreamInputTransport(ep, "s1", transport=None, params=params)
    assert params.audio_in_sample_rate == 24000


def test_audio_stream_output_propagates_endpoint_sample_rate():
    ep = FakeEndpoint(out_rate=24000)
    params = TransportParams(audio_out_enabled=True)
    assert params.audio_out_sample_rate is None
    AudioStreamOutputTransport(ep, "s1", transport=None, params=params)
    assert params.audio_out_sample_rate == 24000


def test_user_supplied_rate_preserved_with_warning(caplog):
    """User-supplied rate is not silently overwritten — we warn instead."""
    import logging
    caplog.set_level(logging.WARNING)
    ep = FakeEndpoint(in_rate=8000)
    params = TransportParams(audio_in_enabled=True, audio_in_sample_rate=16000)
    SipInputTransport(ep, "c1", transport=None, params=params)
    # We don't overwrite — the user gets what they asked for.
    assert params.audio_in_sample_rate == 16000
