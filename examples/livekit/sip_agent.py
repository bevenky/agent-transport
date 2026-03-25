"""SIP voice agent with tool calling — handles inbound and outbound calls.

Inbound:  SIP INVITE arrives → agent answers and starts conversation
Outbound: POST /call {"to": "sip:+15551234567@provider.com"}

Usage:
    python examples/livekit/livekit_sip_agent.py start       # production
    python examples/livekit/livekit_sip_agent.py dev         # dev mode
    python examples/livekit/livekit_sip_agent.py debug       # full debug
"""

import logging
import os

from dotenv import load_dotenv

from agent_transport.sip.livekit import AgentServer, CallContext

from livekit.agents import Agent, AgentSession, RunContext, TurnHandlingOptions
from livekit.agents.llm import function_tool
from livekit.plugins import deepgram, openai, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

load_dotenv()

logger = logging.getLogger("sip-agent")

server = AgentServer(
    sip_username=os.environ["SIP_USERNAME"],
    sip_password=os.environ["SIP_PASSWORD"],
    sip_server=os.environ.get("SIP_DOMAIN", "phone.plivo.com"),
)


@server.setup()
def prewarm():
    return {
        "vad": silero.VAD.load(),
        "turn_detector": MultilingualModel(),
    }


class Assistant(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "You are a helpful phone assistant. "
                "Keep responses concise and conversational. "
                "Do not use emojis, asterisks, markdown, or special formatting."
            ),
        )

    async def on_enter(self) -> None:
        self.session.generate_reply(
            instructions="Greet the user and ask how you can help."
        )

    @function_tool
    async def lookup_weather(
        self, context: RunContext, location: str, latitude: str, longitude: str
    ) -> str:
        """Called when the user asks for weather related information.
        Ensure the user's location (city or region) is provided.
        When given a location, please estimate the latitude and longitude
        and do not ask the user for them.

        Args:
            location: The location they are asking for
            latitude: The latitude of the location, do not ask user for it
            longitude: The longitude of the location, do not ask user for it
        """
        logger.info("Looking up weather for %s", location)
        return f"The weather in {location} is sunny with a temperature of 72 degrees."

    @function_tool
    async def end_call(self, context: RunContext) -> str:
        """Ends the current call and disconnects.

        Call when:
        - The user clearly indicates they are done (e.g., "that's all, bye")
        - The conversation is complete and should end

        Do not call when:
        - The user asks to pause, hold, or transfer
        - Intent is unclear
        """
        logger.info("End call requested")
        context.session.shutdown()
        return "Say goodbye to the user."

    @function_tool
    async def transfer_call(self, context: RunContext, destination: str) -> str:
        """Transfer the call to another phone number or agent.

        Args:
            destination: The phone number or SIP URI to transfer to
        """
        logger.info("Transfer requested to %s", destination)
        # TODO: implement SIP REFER or blind transfer
        return f"I'm transferring you to {destination} now. Please hold."


@server.sip_session()
async def entrypoint(ctx: CallContext):
    session = AgentSession(
        vad=ctx.userdata["vad"],
        stt=deepgram.STT(model="nova-3"),
        llm=openai.LLM(model="gpt-4.1-mini"),
        tts=openai.TTS(voice="alloy"),
        turn_handling=TurnHandlingOptions(
            turn_detection=ctx.userdata["turn_detector"],
        ),
        preemptive_generation=True,
        aec_warmup_duration=3.0,
        tts_text_transforms=["filter_emoji", "filter_markdown"],
    )
    await ctx.start(session, agent=Assistant())


if __name__ == "__main__":
    server.run()
