"""GSAJDPAgent — LangChain + MCP agent for the GSA JDP MCP Toolkit eval harness.

Launches tools/gsa_jdp_mcp_toolkit/server.py over stdio, instruments all traces
into Arize Phoenix, and exposes a synchronous task factory for run_experiment.py.

Usage (standalone smoke test):
    cd eval/phoenix
    uv run agent.py
"""
import asyncio
import os
import pathlib

from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from openinference.instrumentation.langchain import LangChainInstrumentor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk import trace as trace_sdk
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

load_dotenv()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE = pathlib.Path(__file__).parent                        # eval/phoenix/
REPO_ROOT = str((_HERE / ".." / "..").resolve())             # repo root
_PROMPTS_DIR = _HERE / "prompts"

# ---------------------------------------------------------------------------
# Phoenix OTLP defaults
# ---------------------------------------------------------------------------
_PHOENIX_ENDPOINT = os.getenv(
    "PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006/v1/traces"
)
_PROJECT_NAME = os.getenv("PHOENIX_PROJECT_NAME", "gsa-jdp-toolkit-eval")


def _load_system_prompt(version: str = "v1") -> str:
    path = _PROMPTS_DIR / f"system_prompt_{version}.txt"
    if not path.exists():
        raise FileNotFoundError(f"System prompt not found: {path}")
    return path.read_text()


class GSAJDPAgent:
    """LangChain agent wired to the gsa-jdp-toolkit MCP server via stdio."""

    def __init__(
        self,
        system_prompt_version: str = "v1",
        phoenix_endpoint: str = _PHOENIX_ENDPOINT,
        project_name: str = _PROJECT_NAME,
    ):
        self.system_prompt_version = system_prompt_version
        self.phoenix_endpoint = phoenix_endpoint
        self.project_name = project_name
        self._agent = None
        self._client = None
        self._initialized = False

    async def initialize(self) -> None:
        """Instrument Phoenix, connect to the MCP server, build the agent."""
        if self._initialized:
            return

        # ── Phoenix instrumentation ──────────────────────────────────────────
        tracer_provider = trace_sdk.TracerProvider()
        tracer_provider.add_span_processor(
            SimpleSpanProcessor(OTLPSpanExporter(endpoint=self.phoenix_endpoint))
        )
        LangChainInstrumentor().instrument(tracer_provider=tracer_provider)

        # ── MCP client (stdio) ───────────────────────────────────────────────
        model_name = os.getenv("AGENT_MODEL", "gpt-4o")
        api_key = os.getenv("USAI_API_KEY", "")
        base_url = os.getenv("USAI_BASE_URL", "")
        openai_base = base_url.rstrip('/') if base_url else None

        self._client = MultiServerMCPClient(
            {
                "gsa_jdp_server": {
                    "transport": "stdio",
                    "command": "python",
                    "args": ["tools/gsa_jdp_mcp_toolkit/server.py"],
                    "cwd": REPO_ROOT,
                }
            }
        )
        tools = await self._client.get_tools()

        llm = ChatOpenAI(
            model=model_name,
            temperature=0,
            api_key=api_key,
            base_url=openai_base,
        )

        system_prompt = _load_system_prompt(self.system_prompt_version)
        self._agent = create_react_agent(llm, tools, prompt=system_prompt)
        self._initialized = True

    async def run(self, query: str) -> str:
        """Run the agent on a single query and return the response as a string."""
        if not self._initialized:
            await self.initialize()

        result = await self._agent.ainvoke({"messages": [("human", query)]})
        messages = result.get("messages", [])
        for msg in reversed(messages):
            content = getattr(msg, "content", None)
            if content and isinstance(content, str) and content.strip():
                return content
        return str(result)

    def create_experiment_task(self):
        """Return a synchronous task function for Phoenix run_experiment.

        Each call spins up its own event loop and a fresh agent instance so
        that asyncio.run() never conflicts with an already-running loop in
        the executor thread, and the MCP stdio subprocess stays self-contained.
        """
        system_prompt_version = self.system_prompt_version
        phoenix_endpoint = self.phoenix_endpoint
        project_name = self.project_name

        def task(example) -> dict:
            async def _run():
                fresh_agent = GSAJDPAgent(
                    system_prompt_version=system_prompt_version,
                    phoenix_endpoint=phoenix_endpoint,
                    project_name=project_name,
                )
                try:
                    return await fresh_agent.run(example.input["query"])
                finally:
                    if fresh_agent._client is not None:
                        try:
                            await fresh_agent._client.__aexit__(None, None, None)
                        except Exception:
                            pass

            return {"response": asyncio.run(_run())}

        return task


# ---------------------------------------------------------------------------
# Standalone smoke test
# ---------------------------------------------------------------------------
async def _smoke_test():
    agent = GSAJDPAgent()
    await agent.initialize()
    answer = await agent.run(
        "How many GSA-owned buildings over 50,000 RSF are in Texas?"
    )
    print("Smoke test answer:\n", answer)


if __name__ == "__main__":
    asyncio.run(_smoke_test())
