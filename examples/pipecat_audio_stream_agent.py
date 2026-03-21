#!/usr/bin/env python3
"""Example: Pipecat Agent with Plivo audio streaming transport.

Starts a WebSocket server that Plivo connects to for audio streaming.
No SIP registration needed — Plivo initiates the call and streams audio.

Prerequisites:
    cd crates/agent-transport-python && maturin develop --features audio-stream
    pip install agent-transport-adapters[pipecat]

Setup:
    1. Configure Plivo to send audio stream to ws://your-server:8080/ws
    2. Set Plivo XML response with <Stream bidirectional="true"> pointing here

Usage:
    PLIVO_AUTH_ID=xxx PLIVO_AUTH_TOKEN=yyy python examples/pipecat_audio_stream_agent.py
"""

import asyncio
import os

# Note: AudioStreamEndpoint would need to be exposed in the Python binding
# This example shows the intended usage pattern

print("""
Plivo Audio Streaming + Pipecat Example
========================================

This example demonstrates the intended usage:

1. AudioStreamEndpoint starts a WebSocket server on port 8080
2. Configure Plivo to stream audio to ws://your-server:8080
3. When Plivo connects, a session is created
4. Audio flows through the Pipecat pipeline:
   Plivo -> WebSocket -> AudioStreamInput -> STT -> LLM -> TTS -> AudioStreamOutput -> WebSocket -> Plivo

Usage pattern:

    from agent_transport import AudioStreamEndpoint, AudioStreamConfig
    from agent_transport_adapters.pipecat import AudioStreamTransport

    config = AudioStreamConfig(
        listen_addr="0.0.0.0:8080",
        plivo_auth_id=os.environ["PLIVO_AUTH_ID"],
        plivo_auth_token=os.environ["PLIVO_AUTH_TOKEN"],
    )
    ep = AudioStreamEndpoint(config)

    # Wait for Plivo to connect
    event = ep.wait_for_event(timeout_ms=30000)
    session_id = event["session"].call_id

    # Create Pipecat transport
    transport = AudioStreamTransport(ep, session_id)
    pipeline = Pipeline([transport.input(), stt, llm, tts, transport.output()])
    await runner.run(pipeline)
""")
