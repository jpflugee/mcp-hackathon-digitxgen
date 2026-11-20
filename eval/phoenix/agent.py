"""WeatherForecastAgent — LangChain agent wired to the GSA MAR weather MCP server.

Launches ``tools/gsa_jdp_mcp_toolkit/server_weather_forecast.py`` over stdio,
instruments everything with Phoenix/OpenInference, and exposes a synchronous
task factory for Phoenix experiments.

Standalone smoke test:
    uv run agent.py            # asks one sample question end-to-end
"""

from __future__ import annotations

import argparse
import asyncio
import os

from dotenv import load_dotenv

load_dotenv()  # walks up from CWD to the repo-root .env

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")

SERVER_SCRIPT = os.path.join(
    REPO_ROOT, "tools", "gsa_jdp_mcp_toolkit", "server_weather_forecast.py"
)


def _server_python() -> str:
    """Python used to launch the MCP server subprocess.

    The server needs mcp>=2 (+ requests/matplotlib/seaborn), which lives in the
    ROOT venv — this harness's own venv has mcp<2 (langchain-mcp-adapters
    constraint). Run `uv sync --group dev` at the repo root to create it.
    """
    candidates = [
        os.path.join(REPO_ROOT, ".venv", "Scripts", "python.exe"),  # Windows
        os.path.join(REPO_ROOT, ".venv", "bin", "python"),  # POSIX
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    raise RuntimeError(
        "Root venv not found — run `uv sync --group dev` at the repo root "
        "so the MCP server's dependencies are available."
    )

DEFAULT_PROJECT_NAME = "gsa-weather-forecast-eval"
DEFAULT_PHOENIX_ENDPOINT = "http://localhost:6006"


def _usai_base_url() -> str:
    base = os.getenv("USAI_BASE_URL", "").rstrip("/")
    if not base:
        raise RuntimeError("USAI_BASE_URL is not set (see .env.example)")
    if not base.endswith("/api/v1"):
        base = f"{base}/api/v1"
    return base


class WeatherForecastAgent:
    """Owns Phoenix instrumentation, the MCP client, and the LangChain agent."""

    def __init__(
        self,
        system_prompt_version: str = "v1",
        project_name: str = DEFAULT_PROJECT_NAME,
        phoenix_endpoint: str = DEFAULT_PHOENIX_ENDPOINT,
    ) -> None:
        self.system_prompt_version = system_prompt_version
        self.project_name = project_name
        self.phoenix_endpoint = phoenix_endpoint
        self.client = None
        self.agent = None

    def _load_system_prompt(self) -> str:
        path = os.path.join(
            PROMPTS_DIR, f"system_prompt_{self.system_prompt_version}.txt"
        )
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    async def initialize(self) -> None:
        # 1. Instrument once: Phoenix OTEL + LangChain auto-instrumentation.
        from openinference.instrumentation.langchain import LangChainInstrumentor
        from phoenix.otel import register

        tracer_provider = register(
            project_name=self.project_name,
            endpoint=f"{self.phoenix_endpoint.rstrip('/')}/v1/traces",
        )
        LangChainInstrumentor().instrument(tracer_provider=tracer_provider)

        # 2. Launch the MCP server over stdio with the ROOT venv's python,
        #    which has the server's deps (mcp>=2, requests, matplotlib,
        #    seaborn) installed.
        from langchain_mcp_adapters.client import MultiServerMCPClient

        self.client = MultiServerMCPClient(
            {
                "gsa_mar_weather_forecast": {
                    "transport": "stdio",
                    "command": _server_python(),
                    "args": [SERVER_SCRIPT],
                    "cwd": REPO_ROOT,
                }
            }
        )
        tools = await self.client.get_tools()
        if not tools:
            raise RuntimeError(
                "MCP server exposed no tools "
                f"(launch: {_server_python()} {SERVER_SCRIPT})"
            )

        # 3. Build the LangChain agent against the USAi gateway.
        from langchain.agents import create_agent
        from langchain_openai import ChatOpenAI

        model = ChatOpenAI(
            model=os.getenv("AGENT_MODEL", "claude_4_6_sonnet"),
            api_key=os.environ["USAI_API_KEY"],
            base_url=_usai_base_url(),
            temperature=0,
        )
        self.agent = create_agent(
            model=model,
            tools=tools,
            system_prompt=self._load_system_prompt(),
        )

    async def run(self, query: str) -> str:
        if self.agent is None:
            await self.initialize()
        result = await self.agent.ainvoke(
            {"messages": [{"role": "user", "content": query}]}
        )
        final = result["messages"][-1]
        content = final.content
        if isinstance(content, list):  # content-block style responses
            content = "".join(
                b.get("text", "") if isinstance(b, dict) else str(b) for b in content
            )
        return content

    def create_experiment_task(self):
        """Phoenix experiments expect a *synchronous* task(example) function."""

        def task(example) -> str:
            inp = example["input"] if isinstance(example, dict) else example.input
            return asyncio.run(self.run(inp["query"]))

        return task


async def _amain(question: str, prompt_version: str) -> None:
    agent = WeatherForecastAgent(system_prompt_version=prompt_version)
    await agent.initialize()
    print(f"Q: {question}\n")
    answer = await agent.run(question)
    print(f"A: {answer}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test the WeatherForecastAgent")
    parser.add_argument(
        "question",
        nargs="?",
        default=(
            "What is the GSA location code of the federal building matched by "
            "the partial name 'Whitten'?"
        ),
    )
    parser.add_argument("--system-prompt-version", default="v1")
    args = parser.parse_args()
    asyncio.run(_amain(args.question, args.system_prompt_version))


if __name__ == "__main__":
    main()
