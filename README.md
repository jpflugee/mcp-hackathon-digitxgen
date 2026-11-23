# GSA MCP Hackathon — Server Template

A ready-to-run starter for building a [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server in Python, plus deployment kits for **IBM Cloud (watsonx Orchestrate)** and **Databricks**.

Built with [FastMCP](https://github.com/jlowin/fastmcp) and [uv](https://docs.astral.sh/uv/). If you have never built an MCP server before, start with **[QUICKSTART.md](QUICKSTART.md)**.

---

## What is an MCP server?

An MCP server exposes **tools** (functions the model can call), **prompts** (reusable conversation starters), and **resources** (data the model can read) to an AI client such as Claude Desktop, Claude Code, or an agent platform like watsonx Orchestrate. You write the tools; the client's model decides when to call them.

This template gives you a working server with one example of each, so you can replace the examples with your own service and deploy.

---

## Repo structure

```
mcp-hackathon-template/
├── README.md                  # This file
├── QUICKSTART.md              # 5-minute clone → run → connect walkthrough
├── SECURITY.md                # Vulnerability disclosure policy + hackathon security notes
├── LICENSE                    # MIT
├── main.py                    # Local entry point (uv run python main.py)
├── pyproject.toml             # Package + dependencies (uv)
├── requirements.txt           # Mirror of runtime deps (for buildpack hosts)
├── uv.lock                    # Locked dependency versions
├── .python-version            # Python version pin (used by uv)
├── Dockerfile                 # Container image (streamable-HTTP, port 8080)
├── manifest.yaml              # cloud.gov (Cloud Foundry) deploy manifest
├── server.json                # MCP registry metadata
├── .env.example               # Copy to .env for local dev
├── .mcp.json                  # MCP client config (Claude Desktop / Claude Code)
├── workspace_config.yaml      # Bob agent workspace configuration
├── .github/
│   └── workflows/
│       └── ci.yml             # Lint + test on every push / PR
├── docs/
│   └── README.md              # Vendor-contributed reference materials notice
├── agents/
│   ├── gsa_jdp_agent.yaml     # wxO agent definition for the JDP toolkit
│   └── gsa_mar_agent.yaml     # wxO agent definition for the MAR toolkit
├── src/
│   └── example_server/        # ← rename to your service
│       ├── app.py             # Thin entry point: builds FastMCP, picks transport
│       ├── config.py          # Settings from env vars / .env
│       ├── models.py          # Pydantic models & enums for tool params
│       ├── utils.py           # Shared helpers (HTTP client, pagination)
│       ├── routes.py          # HTTP-only routes (/health, /version)
│       ├── tools/
│       │   ├── __init__.py    #   register_tools(mcp) aggregator
│       │   └── example_tool.py
│       ├── prompts/
│       │   ├── __init__.py    #   register_prompts(mcp) aggregator
│       │   └── example.py
│       └── resources/
│           ├── __init__.py    #   register_resources(mcp) aggregator
│           └── example.py
├── tools/
│   └── gsa_jdp_mcp_toolkit/   # GSA AutoNOC Polaris toolkit servers
│       ├── README.md          # Toolkit docs (servers, tools, testing, eval)
│       ├── requirements.txt   # Standalone pip install (mcp, requests, matplotlib, seaborn)
│       ├── server.py          # JDP core: locations, weather, alerts, ServiceNow (5 tools)
│       ├── server_weather_forecast.py  # MAR: forecast text table + PNG chart (2 tools)
│       ├── server_analytics.py         # MAR: comms outage scan + building (2 tools)
│       ├── server_prompts.py           # MAR: 7 reusable prompt starters
│       ├── server_combined.py          # All 9 tools in one stdio process (default eval target)
│       ├── toolkit.yaml                # wxO import config → server.py
│       ├── toolkit_weather_forecast.yaml  # wxO import config → server_weather_forecast.py
│       ├── toolkit_analytics.yaml         # wxO import config → server_analytics.py
│       ├── toolkit_prompts.yaml           # wxO import config → server_prompts.py
│       └── toolkit_combined.yaml          # wxO import config → server_combined.py
├── tests/
│   ├── __init__.py
│   ├── test_server.py         # Example server import + registration smoke tests
│   ├── test_toolkit_tools.py  # 25 direct-call tests — individual toolkit servers
│   └── test_agent_tools.py    # 17 agent-style tests — all 9 tools via server_combined.py
├── eval/
│   ├── README.md              # Why evaluate, what's built, quick-start
│   └── phoenix/               # Arize Phoenix eval harness
│       ├── pyproject.toml     # Separate uv project (mcp<2 constraint)
│       ├── uv.lock
│       ├── agent.py           # MCPToolkitAgent — targets: jdp, weather-forecast, combined
│       ├── create_dataset.py  # CLI: upload CSV datasets to Phoenix
│       ├── run_experiment.py  # CLI: run agent + LLM-as-judge against a dataset
│       ├── datasets.yaml      # 3 registered datasets
│       ├── judges/            # Correctness + per-target scope LLM judges
│       ├── prompts/
│       │   ├── jdp/           #   system_prompt_v1.txt, v2.txt
│       │   ├── weather-forecast/  # system_prompt_v1.txt, v2.txt
│       │   └── combined/      #   system_prompt_v1.txt (all 9 tools)
│       └── datasets/
│           ├── gsa-eval-0/            # 10 gold Q/A — locations, weather, alerts, ServiceNow
│           ├── gsa-weather-eval-0/    # 10 gold Q/A — weather + alerts at GSA sites
│           └── gsa-weather-forecast-eval-0/  # 12 Q/A — forecast tables, unit invariants
└── deploy/
    ├── README.md              # Deployment kit chooser
    ├── ibm/                   # watsonx Orchestrate — 3 kits
    │   ├── README.md
    │   ├── local-mcp-toolkit/ #   stdio inside Orchestrate (lowest overhead)
    │   ├── code-engine-git-build/  # build from Git → Code Engine
    │   └── prebuilt-image/    #   deploy a prebuilt image to Code Engine
    └── databricks/            # Databricks Apps kit
```

---

## Getting started

### Prerequisites
- [uv](https://docs.astral.sh/uv/) — `pip install uv` or `brew install uv`

### Install and run

```bash
cp .env.example .env
uv sync
uv run python main.py
```

The server starts in **stdio** mode — it talks JSON-RPC over stdin/stdout, which is how local clients (Claude Desktop, Claude Code) launch it. See [QUICKSTART.md](QUICKSTART.md) to connect a client.

### Verify

```bash
uv sync --group dev
uv run pytest tests/ -v      # tests
uv run ruff check .          # lint
```

---

## The one-tool-per-file pattern

Each tool lives in its own file under `src/example_server/tools/` and exposes a `register(mcp)` function. `tools/__init__.py` calls each one from a single `register_tools(mcp)`. This keeps the tool list scannable and lets you add or remove an integration by touching two files.

**Step 1 — create `src/example_server/tools/my_tool.py`:**

```python
from typing import Annotated
from urllib.parse import quote
from fastmcp import FastMCP

from example_server.utils import fetch_json


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="example_get_thing",
        annotations={
            "title": "Get a thing",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def get_thing(thing_id: Annotated[str, "The ID to fetch."]) -> dict:
        """One-line summary. Document the data source, its update cadence,
        and the return shape here — the model reads this docstring."""
        safe_id = quote(thing_id, safe="")
        return await fetch_json(f"things/{safe_id}")
```

**Step 2 — wire it up in `tools/__init__.py`:**

```python
from example_server.tools import example_tool, my_tool

def register_tools(mcp) -> None:
    example_tool.register(mcp)
    my_tool.register(mcp)  # ← add this line
```

**Step 3 — add any API key** as a typed field in `config.py` and document the env var in `.env.example`.

Prompts (`prompts/`) and resources (`resources/`) follow the exact same `register(mcp)` + aggregator pattern.

---

## Rename the package

Before publishing your server, rename `example_server` to your service (e.g. `census_mcp`):

1. Rename the folder `src/example_server/` → `src/<your_name>/`.
2. Update `pyproject.toml`: the `[project].name`, `[project.scripts]`, and `[tool.hatch.build.targets.wheel].packages`.
3. Find-and-replace `example_server` across `src/`, `tests/`, `main.py`, `Dockerfile`, and `manifest.yaml`.

---

## Tool design tips (federal data)

- **Return structured data, not prose.** Return dicts/lists with consistent keys and let the model narrate.
- **Document freshness.** Federal datasets lag; state the update frequency and "as-of" date in the docstring.
- **Expose pagination.** Use `PaginationParams` / `paginate()` from `utils.py`, and return `has_more` / `next_offset`.
- **Use explicit timeouts.** `utils.fetch_json` defaults to 30s.
- **Actionable errors.** Return an error dict with a `hint`, not a raw stack trace.
- **Constrain outbound requests.** Keep API origins in operator-controlled code or configuration, pass only validated relative paths to `fetch_json`, and encode path segments. Never pass a tool-supplied URL directly to an HTTP client.
- **Treat redirects and DNS as security boundaries.** Redirects are disabled by default. A server that must fetch caller-supplied URLs needs connection-time IP validation on every hop plus network egress controls; a one-time DNS check is not sufficient.
- **Keep sensitive data out of errors and logs.** Do not expose or log upstream bodies, headers, full URLs, query strings, credentials, stack traces, SSNs, dates of birth, or addresses.

---

## Deploying

Local development uses stdio. To share your server with an agent platform, deploy it and register it. See **[deploy/README.md](deploy/README.md)** for a chooser, then:

- **IBM watsonx Orchestrate** — [deploy/ibm/](deploy/ibm/) (three kits: local stdio toolkit, Code Engine build-from-Git, and prebuilt image).
- **Databricks Apps** — [deploy/databricks/](deploy/databricks/).

Both read the same server code; `app.py` automatically serves HTTP when the platform injects a port.

---

## Partner reference materials

The [`docs/`](docs/) directory contains educational reference materials that
IBM and Databricks may contribute for hackathon participants. Vendor-contributed
materials do not represent the views, positions, or policies of GSA or the
United States Government.

GSA does not endorse any commercial product, service, or enterprise referenced
in these materials. Reference to a commercial product, service, or enterprise
does not constitute or imply endorsement, recommendation, or favoring by GSA or
the United States Government. These materials are provided for educational
purposes only. See [`docs/README.md`](docs/README.md) for the complete notice and
contribution guidance.

---

## Evaluations

An Arize Phoenix eval harness is fully built in [`eval/phoenix/`](eval/phoenix/). It measures how well an LLM agent can use the GSA toolkit tools to answer realistic questions, scoring each answer with two LLM-as-judge evaluators (correctness + scope). Three targets are supported: `jdp`, `weather-forecast`, and `combined` (all 9 tools). See [eval/README.md](eval/README.md) for the quick-start and [eval/phoenix/README.md](eval/phoenix/README.md) for the full setup.

---

## License

[MIT](LICENSE). See [SECURITY.md](SECURITY.md) for the vulnerability disclosure policy and hackathon security notes.
