# Phoenix Eval Harness — GSA MCP toolkit

Measures how well an LLM agent, given ONLY one GSA MCP server's tools, can
answer realistic questions — i.e. whether the tool names, schemas, docstrings
and return shapes are good enough to use without any outside knowledge. Built
per the `mcp-eval` skill (`.agents/skills/mcp-eval/`).

One harness, three servers under test (a **target**):

| Target | Server | Tools | Datasets |
|---|---|---|---|
| `jdp` | `tools/gsa_jdp_mcp_toolkit/server.py` | 5 — IOLP locations, current weather, hazard alerts, ServiceNow | `gsa-eval-0`, `gsa-weather-eval-0` |
| `weather-forecast` | `tools/gsa_jdp_mcp_toolkit/server_weather_forecast.py` | 2 — historical + NWS forecast text tables / charts | `gsa-weather-forecast-eval-0` |
| `combined` *(default)* | `tools/gsa_jdp_mcp_toolkit/server_combined.py` | **9** — all JDP + MAR tools in one process | any dataset |

Each dataset in `datasets.yaml` names its `target`, so
`run_experiment.py --dataset-name X` picks the right server, system prompt and
scope judge automatically (override with `--target`).

## How it works

1. `agent.py` launches the target's server over stdio (with the root venv's
   python) and binds its tools to a LangChain agent driven by `AGENT_MODEL`
   through the USAi gateway.
2. `run_experiment.py` feeds it the registered dataset and scores each answer
   with two LLM-as-judge evaluators (`JUDGE_MODEL`, keep it distinct from
   `AGENT_MODEL`):
   - `match_expected_response` — correct vs. ground truth (format-tolerant)
   - `check_answer_scope` — answered in scope vs. fabricated/out-of-scope
     (the scope definition is per target)
3. Traces + scores land in Phoenix (http://localhost:6006).

## Setup

This harness is a **separate uv project** from the repo root:
`langchain-mcp-adapters` requires `mcp<2` while the toolkit servers require
`mcp>=2`, so they get different venvs. The agent launches the server
subprocess with the root venv's python.

```bash
# 1. repo root — creates the venv the MCP servers run in
uv sync --group dev

# 2. eval/phoenix — creates the harness venv
cd eval/phoenix
uv sync

# 3. copy and fill in .env at the repo root
cp ../../.env.example ../../.env   # then fill in USAI_API_KEY / USAI_BASE_URL
```

On a machine with corporate TLS interception, add `--native-tls` (or
`--system-certs` on newer uv) to the `uv sync` commands.

Required `.env` variables:

| Variable | Purpose |
|---|---|
| `USAI_API_KEY` | Model gateway API key |
| `USAI_BASE_URL` | Gateway base URL (harness appends `/api/v1`) |
| `AGENT_MODEL` | Model the agent uses |
| `JUDGE_MODEL` | Model the judges use — keep distinct from `AGENT_MODEL` |

Both servers' read-only tools need **no API keys** (public IOLP / Open-Meteo /
NWS / USGS / NOAA SWPC / JMA / DWD / MeteoAlarm APIs). Testing ServiceNow
posting end-to-end on the `jdp` or `combined` target needs the `SERVICENOW_*`
variables.

## Run

All commands from `eval/phoenix/`:

```bash
# 1. Start Phoenix (UI at :6006) — separate terminal
uvx arize-phoenix serve
# If uvx is not available, use one of these alternatives:
#   uv tool run arize-phoenix serve
#   pip install arize-phoenix && python -m phoenix.server.main serve

# 2. List, validate, then upload a dataset (one-time per dataset)
uv run create_dataset.py --list
uv run create_dataset.py --dataset-name gsa-eval-0 --dry-run
uv run create_dataset.py --dataset-name gsa-eval-0
uv run create_dataset.py --dataset-name gsa-weather-eval-0
uv run create_dataset.py --dataset-name gsa-weather-forecast-eval-0

# 3. Run experiments (target comes from datasets.yaml, or override with --target)
uv run run_experiment.py --dataset-name gsa-eval-0
uv run run_experiment.py --dataset-name gsa-eval-0 --system-prompt-version v2
uv run run_experiment.py --dataset-name gsa-eval-0 --target combined
uv run run_experiment.py --dataset-name gsa-weather-eval-0
uv run run_experiment.py --dataset-name gsa-weather-forecast-eval-0

# Optional: one-off smoke test of the agent (no dataset, no judges)
uv run agent.py --target combined
uv run agent.py --target jdp "How many GSA-owned buildings over 50k RSF are in Texas?"
uv run agent.py --target weather-forecast "What is the location code of 'Whitten'?"
```

Review failing cases in the Phoenix UI (each shows `correct`/`incorrect`,
`within_scope`/`out_of_scope`, and the full tool-call trace) and decide whether
the fix belongs in the tool schema/docstring, the system prompt, or the
question itself.

## Layout

```
eval/phoenix/
├── agent.py            # MCPToolkitAgent + TARGETS (jdp, weather-forecast, combined)
├── create_dataset.py   # CLI: validate/upload CSV datasets to Phoenix
├── run_experiment.py   # CLI: agent + judges against a Phoenix dataset
├── datasets.yaml       # dataset registry (each entry names its target)
├── judges/             # correctness judge + per-target scope judge
├── prompts/
│   ├── jdp/                # system_prompt_v1 (basic), v2 (+ scope boundaries)
│   ├── weather-forecast/   # system_prompt_v1 (basic), v2 (+ scope boundaries)
│   └── combined/           # system_prompt_v1 — covers all 9 tools
└── datasets/
    ├── gsa-eval-0/              # examples/gsa-eval-0.csv
    ├── gsa-weather-eval-0/      # examples/gsa-weather-eval-0.csv
    └── gsa-weather-forecast-eval-0/  # evaluation.xml + examples/*.csv
```

## Datasets

- **`gsa-eval-0`** (`jdp`) — 10 gold questions: location counts/addresses/region
  lookups, current weather, hazard alerts, and ServiceNow severity validation.
  Gold answers describe what a correct answer must contain, since live data
  changes.
- **`gsa-weather-eval-0`** (`jdp`) — 10 questions on current weather, hazard
  alerts, and weather-related ServiceNow actions at GSA sites.
- **`gsa-weather-forecast-eval-0`** (`weather-forecast`) — source
  `evaluation.xml` (10 gold Q/A) plus the generated CSV (`query,responses`),
  which adds 2 out-of-scope probes for the scope judge. Because live weather
  changes daily, gold answers anchor to stable facts: IOLP building metadata
  surfaced through the tools, server clamping/validation behavior,
  unit-conversion invariants, and climatologically certain comparisons
  (Anchorage vs. Miami). Building facts verified against the live IOLP ArcGIS
  dataset on 2026-11-19.

### Adding questions or a new server

1. Add rows to a dataset's CSV, then re-upload with `create_dataset.py`.
2. For a new dataset, add an entry to `datasets.yaml` (with its `target`).
3. For a new MCP server, add a `TARGETS` entry in `agent.py`, a
   `prompts/<target>/` folder, and a scope template in
   `judges/relevance_judge.py`.

## Tool tests (no gateway required)

All 9 tools in `server_combined.py` are tested directly (no USAi gateway
needed) in `tests/test_agent_tools.py` at the repo root:

```bash
# from the repo root
uv run pytest tests/test_agent_tools.py -v   # 17 tests, all 9 tools
```

These simulate the exact calls the agent makes and verify each tool returns
correct output against the live public APIs.

## Interpreting failures

| Failure pattern | Likely root cause | Fix |
|---|---|---|
| Agent calls wrong tool | Tool name or description is unclear | Improve tool docstring/description |
| Agent passes wrong parameter | Parameter schema or description is ambiguous | Tighten parameter description |
| Agent returns correct facts but judge marks incorrect | Judge template too strict | Relax correctness template |
| Agent answers out-of-scope questions | System prompt scope section too weak | Strengthen `system_prompt_v2.txt` |
| Agent never calls tools | System prompt missing tool-use instruction | Add explicit "always use tools" instruction |
| `Connection error` on run | USAi gateway unreachable | Check VPN / confirm `USAI_BASE_URL` in `.env` |
