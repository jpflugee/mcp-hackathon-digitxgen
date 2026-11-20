# Phoenix Eval Harness — gsa-mar-weather-forecast

Measures how well an LLM agent, given ONLY the `gsa-mar-weather-forecast` MCP
server's tools (`gsa_mar_show_weather_forecasted_data`,
`gsa_mar_plot_weather_forecasted_data`), can answer realistic questions about
weather at GSA federal buildings. Built per the `mcp-eval` skill
(`.agents/skills/mcp-eval/`).

## How it works

1. `agent.py` launches `tools/gsa_jdp_mcp_toolkit/server_weather_forecast.py`
   over stdio (with the current venv's python) and binds its tools to a
   LangChain agent driven by `AGENT_MODEL` through the USAi gateway.
2. `run_experiment.py` feeds it the registered dataset and scores each answer
   with two LLM-as-judge evaluators (`JUDGE_MODEL`):
   - `match_expected_response` — correct vs. ground truth (format-tolerant)
   - `check_answer_scope` — answered in scope vs. fabricated/out-of-scope
3. Traces + scores land in Phoenix (http://localhost:6006).

## Setup

This harness is a **separate uv project** from the repo root:
`langchain-mcp-adapters` requires `mcp<2` while the toolkit server requires
`mcp>=2`, so they get different venvs. The agent launches the server
subprocess with the root venv's python.

```bash
# 1. repo root — creates the venv the MCP server runs in
uv sync --group dev

# 2. eval/phoenix — creates the harness venv
cd eval/phoenix
uv sync

cp ../../.env.example ../../.env   # then fill in USAI_API_KEY / USAI_BASE_URL
```

On a machine with corporate TLS interception, add `--native-tls` (or
`--system-certs` on newer uv) to the `uv sync` commands.

The MCP server itself needs no credentials (public IOLP/Open-Meteo/NWS APIs);
the `.env` keys are only for the agent and judges.

## Run

All commands from `eval/phoenix/`:

```bash
# 1. Start Phoenix (UI at :6006) — separate terminal, isolated env via uvx
uvx arize-phoenix serve

# 2. Validate, then upload the dataset (one-time)
uv run create_dataset.py --dataset-name gsa-weather-eval-0 --dry-run
uv run create_dataset.py --dataset-name gsa-weather-eval-0

# 3. Run experiments
uv run run_experiment.py --dataset-name gsa-weather-eval-0
uv run run_experiment.py --dataset-name gsa-weather-eval-0 --system-prompt-version v2

# Optional: one-off smoke test of the agent
uv run agent.py "What is the location code of the building matched by 'Whitten'?"
```

Review failing cases in the Phoenix UI and decide whether the fix belongs in
the tool schema/docstring, the system prompt, or the question itself.

## Dataset

`datasets/gsa-weather-eval-0/` holds the source `evaluation.xml` (10 gold Q/A)
and the generated CSV (`query,responses`), which adds 2 out-of-scope probes
for the scope judge. Because live weather changes daily, gold answers anchor
to stable facts: IOLP building metadata surfaced through the tools, server
clamping/validation behavior, unit-conversion invariants, and
climatologically certain comparisons (Anchorage vs. Miami). Building facts
verified against the live IOLP ArcGIS dataset on 2026-11-19.

## Layout

```
eval/phoenix/
├── agent.py            # WeatherForecastAgent (stdio launch + LangChain agent)
├── create_dataset.py   # CLI: validate/upload CSV datasets to Phoenix
├── run_experiment.py   # CLI: agent + judges against a Phoenix dataset
├── datasets.yaml       # dataset registry
├── judges/             # LLM-as-judge evaluators
├── prompts/            # system_prompt_v1 (basic), v2 (+ scope boundaries)
└── datasets/gsa-weather-eval-0/
    ├── evaluation.xml
    └── examples/gsa-weather-eval-0.csv
```
