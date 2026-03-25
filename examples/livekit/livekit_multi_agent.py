"""SIP multi-agent with handoff and tool calling.

Demonstrates multiple agents that can hand off to each other:
- GreeterAgent: greets the caller, gathers intent, hands off
- SalesAgent: handles product inquiries with tool calling
- SupportAgent: handles support issues with tool calling

Usage:
    python examples/livekit/livekit_multi_agent.py start
    python examples/livekit/livekit_multi_agent.py dev
"""

import logging
import os
from dataclasses import dataclass

from dotenv import load_dotenv

from agent_transport.sip.livekit import AgentServer, CallContext

from livekit.agents import Agent, AgentSession, RunContext
from livekit.agents.llm import function_tool
from livekit.plugins import deepgram, openai, silero

load_dotenv()

logger = logging.getLogger("multi-agent")


@dataclass
class CallData:
    caller_name: str | None = None
    intent: str | None = None


class GreeterAgent(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "You are a friendly receptionist. Your job is to greet the caller, "
                "ask for their name, and determine if they need sales or support. "
                "Keep responses brief and natural. "
                "Do not use emojis, markdown, or special formatting."
            ),
        )

    async def on_enter(self) -> None:
        self.session.generate_reply(
            instructions="Greet the caller and ask how you can help them today."
        )

    @function_tool
    async def route_to_sales(
        self, context: RunContext[CallData], caller_name: str
    ) -> Agent:
        """Route the caller to the sales team when they are interested in
        purchasing a product or learning about pricing.

        Args:
            caller_name: The caller's name
        """
        context.userdata.caller_name = caller_name
        context.userdata.intent = "sales"
        logger.info("Routing %s to sales", caller_name)
        return SalesAgent(caller_name)

    @function_tool
    async def route_to_support(
        self, context: RunContext[CallData], caller_name: str
    ) -> Agent:
        """Route the caller to the support team when they have an issue
        or need help with an existing product.

        Args:
            caller_name: The caller's name
        """
        context.userdata.caller_name = caller_name
        context.userdata.intent = "support"
        logger.info("Routing %s to support", caller_name)
        return SupportAgent(caller_name)


class SalesAgent(Agent):
    def __init__(self, caller_name: str) -> None:
        super().__init__(
            instructions=(
                f"You are a sales agent speaking with {caller_name}. "
                "Help them learn about products and pricing. "
                "Be enthusiastic but not pushy. Keep responses concise. "
                "Do not use emojis, markdown, or special formatting."
            ),
        )
        self._caller_name = caller_name

    async def on_enter(self) -> None:
        self.session.generate_reply(
            instructions=f"Introduce yourself as a sales specialist to {self._caller_name} "
            "and ask what product they're interested in."
        )

    @function_tool
    async def check_pricing(
        self, context: RunContext[CallData], product: str
    ) -> str:
        """Look up pricing for a product.

        Args:
            product: The product name to check pricing for
        """
        logger.info("Checking pricing for %s", product)
        return f"{product} starts at $49/month for the basic plan and $99/month for premium."

    @function_tool
    async def check_availability(
        self, context: RunContext[CallData], product: str
    ) -> str:
        """Check if a product is available.

        Args:
            product: The product to check availability for
        """
        logger.info("Checking availability for %s", product)
        return f"{product} is available and can be set up within 24 hours."

    @function_tool
    async def transfer_to_support(self, context: RunContext[CallData]) -> Agent:
        """Transfer to support if the caller has an existing issue instead of a sales inquiry."""
        logger.info("Sales transferring %s to support", self._caller_name)
        return SupportAgent(self._caller_name)


class SupportAgent(Agent):
    def __init__(self, caller_name: str) -> None:
        super().__init__(
            instructions=(
                f"You are a support agent speaking with {caller_name}. "
                "Help them resolve their issue. Be empathetic and solution-oriented. "
                "Keep responses concise. "
                "Do not use emojis, markdown, or special formatting."
            ),
        )
        self._caller_name = caller_name

    async def on_enter(self) -> None:
        self.session.generate_reply(
            instructions=f"Introduce yourself as a support specialist to {self._caller_name} "
            "and ask them to describe their issue."
        )

    @function_tool
    async def lookup_account(
        self, context: RunContext[CallData], account_id: str
    ) -> str:
        """Look up a customer's account details.

        Args:
            account_id: The customer's account ID or email
        """
        logger.info("Looking up account %s", account_id)
        return f"Account {account_id} is active, on the premium plan, last payment was 15 days ago."

    @function_tool
    async def create_ticket(
        self, context: RunContext[CallData], issue_description: str
    ) -> str:
        """Create a support ticket for the caller's issue.

        Args:
            issue_description: Description of the issue
        """
        logger.info("Creating ticket: %s", issue_description)
        return "Support ticket #12345 has been created. Our team will follow up within 2 hours."

    @function_tool
    async def transfer_to_sales(self, context: RunContext[CallData]) -> Agent:
        """Transfer to sales if the caller wants to upgrade or purchase."""
        logger.info("Support transferring %s to sales", self._caller_name)
        return SalesAgent(self._caller_name)


server = AgentServer(
    sip_username=os.environ["SIP_USERNAME"],
    sip_password=os.environ["SIP_PASSWORD"],
    sip_server=os.environ.get("SIP_DOMAIN", "phone.plivo.com"),
)


@server.sip_session()
async def entrypoint(ctx: CallContext):
    session = AgentSession[CallData](
        vad=silero.VAD.load(),
        stt=deepgram.STT(model="nova-3"),
        llm=openai.LLM(model="gpt-4.1-mini"),
        tts=openai.TTS(voice="alloy"),
        userdata=CallData(),
    )
    await ctx.start(session, agent=GreeterAgent())


if __name__ == "__main__":
    server.run()
