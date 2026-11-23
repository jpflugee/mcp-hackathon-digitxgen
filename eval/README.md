# Evaluations

A fully built Arize Phoenix evaluation harness lives in
[`phoenix/`](phoenix/README.md). It measures how well an LLM agent, given
**only** the GSA MCP toolkit's tools, can answer realistic questions — i.e.
whether the tool names, schemas, docstrings, and return shapes are good
enough to use without any outside knowledge. Built with the **`mcp-eval`
skill** (`.agents/skills/mcp-eval/`).

## Why evaluate?

The quality of an MCP server is **not** how many tools it has — it's how
well an LLM with no other context can use those tools (their names, schemas,
docstrings, and return shapes) to answer realistic, difficult questions. An
eval harness measures exactly that and shows you *where* to fix things (tool
schema, docstring, or system prompt).

## What's here

| Path | Purpose |
|---|---|
| [`phoenix/`](phoenix/) | Arize Phoenix harness — datasets, agent, judges, prompts |
| [`phoenix/agent.py`](phoenix/agent.py) | `MCPToolkitAgent` — 3 targets: `jdp`, `weather-forecast`, `combined` |
| [`phoenix/datasets.yaml`](phoenix/datasets.yaml) | 3 registered datasets |
| [`phoenix/prompts/`](phoenix/prompts/) | Versioned system prompts per target (`v1` basic, `v2` + scope) |
| [`phoenix/judges/`](phoenix/judges/) | LLM-as-judge: correctness + per-target scope |

## Targets and datasets

| Target | Server | Datasets |
|---|---|---|
| `jdp` | `tools/gsa_jdp_mcp_toolkit/server.py` | `gsa-eval-0`, `gsa-weather-eval-0` |
| `weather-forecast` | `tools/gsa_jdp_mcp_toolkit/server_weather_forecast.py` | `gsa-weather-forecast-eval-0` |
| `combined` | `tools/gsa_jdp_mcp_toolkit/server_combined.py` | any (all 9 tools) |

## Quick start

```bash
# 1. Set up venvs (repo root + eval/phoenix/)
uv sync --group dev
cd eval/phoenix && uv sync

# 2. Fill in .env at the repo root
cp .env.example .env   # then set USAI_API_KEY, USAI_BASE_URL, AGENT_MODEL, JUDGE_MODEL

# 3. Start Phoenix — separate terminal
uvx arize-phoenix serve          # or: uv tool run arize-phoenix serve

# 4. Upload datasets (one-time)
cd eval/phoenix
uv run create_dataset.py --dataset-name gsa-eval-0
uv run create_dataset.py --dataset-name gsa-weather-eval-0
uv run create_dataset.py --dataset-name gsa-weather-forecast-eval-0

# 5. Run an experiment
uv run run_experiment.py --dataset-name gsa-eval-0
uv run run_experiment.py --dataset-name gsa-eval-0 --target combined
```

See [`phoenix/README.md`](phoenix/README.md) for the full setup, run
options, dataset details, and failure interpretation guide.

## How to build your own harness

Use the **`mcp-eval` skill** (`.agents/skills/mcp-eval/`). It walks through
a reusable [Arize Phoenix](https://docs.arize.com/phoenix) harness that:

1. Launches your MCP server over stdio and connects a LangChain agent to it.
2. Feeds the agent a dataset of natural-language questions with known answers.
3. Scores each answer with LLM-as-judge evaluators and logs traces to Phoenix.

The companion **`mcp-builder` skill** (`.agents/skills/mcp-builder/`) covers
Phase 4 — writing the 10 gold questions (`evaluation.xml`) that become the
dataset.
