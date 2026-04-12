"""Regression test: _TransportLocalParticipant.perform_rpc raises.

Previously returned an empty string silently, which caused callers that
expect a typed JSON response (avatar plugins, warm-transfer protocols)
to get garbage without knowing RPC wasn't actually supported.
"""

import pytest

from agent_transport.sip.livekit._room_facade import _TransportLocalParticipant


class FakeEndpoint:
    input_sample_rate = 8000


@pytest.mark.asyncio
async def test_perform_rpc_raises_not_implemented():
    lp = _TransportLocalParticipant(FakeEndpoint(), "call-1", "agent")
    with pytest.raises(NotImplementedError) as excinfo:
        await lp.perform_rpc(
            destination_identity="other-agent",
            method="greet",
            payload='{"name": "world"}',
        )
    # Error message must mention SIP INFO / HTTP alternatives.
    msg = str(excinfo.value)
    assert "SIP INFO" in msg or "send_info" in msg or "HTTP" in msg
