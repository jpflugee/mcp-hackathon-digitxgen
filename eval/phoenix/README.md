# GSA JDP MCP Toolkit — Phoenix Evaluation Harness

Tests whether the `gsa-jdp-toolkit` MCP server's tools (schemas, docstrings, return shapes) let an LLM agent answer realistic GSA property and hazard questions correctly — without any knowledge outside what the tools provide.

## Layout

```
eval/phoenix/
├── agent.py                          # GSAJDPAgent — launches server.py over stdio
├── create_dataset.py                 # CLI: upload a CSV dataset to Phoenix
├── run_experiment.py                 # CLI: run agent + judges against a dataset
├── datasets.yaml                     # Dataset registry
├── judges/
│   ├── __init__.py
│   ├── correctness_judge.py          # match_expected_response (vs. ground truth)
│   └── relevance_judge.py            # check_answer_scope (in-scope / out-of-scope)
├── prompts/
│   ├── system_prompt_v1.txt          # Basic GSA analyst
│   └── system_prompt_v2.txt          # + explicit scope boundaries
└── datasets/
    └── gsa-eval-0/
        └── examples/gsa-eval-0.csv   # 10 gold questions
```

## Prerequisites

### 1. Install dev dependencies

```bash
# From repo root
uv sync --group dev
```

### 2. Configure environment

Copy `.env.example` to `.env` and fill in the Phoenix eval section:

```bash
cp .env.example .env
```

Required variables:

| Variable | Purpose |
|---|---|
| `USAI_API_KEY` | Model gateway API key |
| `USAI_BASE_URL` | Gateway base URL (harness appends `/api/v1`) |
| `AGENT_MODEL` | Model the agent uses (e.g. `gpt-4o`) |
| `JUDGE_MODEL` | Model the judges use — keep distinct from `AGENT_MODEL` |

The MCP server itself (Open-Meteo, NWS, USGS, NOAA SWPC, JMA, DWD, MeteoAlarm) requires **no API keys** for read-only tools. ServiceNow posting requires the `SERVICENOW_*` variables if you want to test that tool end-to-end.

## Running an evaluation

All commands run from `eval/phoenix/`.

### Step 1 — Start Phoenix

```bash
uv run arize-phoenix serve
# UI at http://localhost:6006  |  OTLP at :4317
```

### Step 2 — Upload the dataset (one-time)

```bash
cd eval/phoenix

# Validate without uploading
uv run create_dataset.py --dataset-name gsa-eval-0 --dry-run

# Upload
uv run create_dataset.py --dataset-name gsa-eval-0
```

### Step 3 — Run the experiment

```bash
# Default (system prompt v1)
uv run run_experiment.py --dataset-name gsa-eval-0

# Try scope-bounded system prompt
uv run run_experiment.py --dataset-name gsa-eval-0 --system-prompt-version v2
```

### Step 4 — Review results

Open **http://localhost:6006** and navigate to the experiment. Each question shows:
- `correct` / `incorrect` from `match_expected_response`
- `within_scope` / `out_of_scope` from `check_answer_scope`
- Full tool-call traces (what tools were called, with what args, what they returned)

## Smoke testing the agent alone

```bash
cd eval/phoenix
uv run agent.py
```

Runs one question against the live MCP server and prints the answer — no Phoenix required.

## Dataset questions

`gsa-eval-0` covers:

| # | Topic | Tools exercised |
|---|---|---|
| 1 | Count GSA-owned buildings in Texas | `gsa_jdp_get_gsa_locations` |
| 2 | Street address of DC0001 | `gsa_jdp_get_gsa_location_detail` |
| 3 | Current temperature at NY0276 | `get_gsa_location_detail` → `gsa_jdp_current_weather` |
| 4 | Region of IL0001 | `gsa_jdp_get_gsa_location_detail` |
| 5 | Active NWS alerts in Florida | `gsa_jdp_get_gsa_weather_alerts` |
| 6 | What hazard sources does the alerts tool check? | Tool schema / description knowledge |
| 7 | Count leased properties in Region 11 | `gsa_jdp_get_gsa_locations` |
| 8 | Current wind speed at TX0001 | `get_gsa_location_detail` → `gsa_jdp_current_weather` |
| 9 | Year built for CA0001 | `gsa_jdp_get_gsa_location_detail` |
| 10 | Reject severity 5 ServiceNow post | `gsa_jdp_post_servicenow_record` (rejection path) |

## Adding more questions

1. Add rows to `datasets/gsa-eval-0/examples/gsa-eval-0.csv`.
2. Re-upload: `uv run create_dataset.py --dataset-name gsa-eval-0`.
3. Or create a new dataset: add an entry to `datasets.yaml` and upload it.

## Interpreting failures

| Failure pattern | Likely root cause | Fix |
|---|---|---|
| Agent calls wrong tool | Tool name or description is unclear | Improve tool docstring/description |
| Agent passes wrong parameter | Parameter schema or description is ambiguous | Tighten parameter description |
| Agent returns correct facts but judge marks incorrect | Judge template too strict | Relax correctness template |
| Agent answers out-of-scope questions | System prompt scope section too weak | Strengthen `system_prompt_v2.txt` |
| Agent never calls tools | System prompt missing tool-use instruction | Add explicit "always use tools" instruction |
