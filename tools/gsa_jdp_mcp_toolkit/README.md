# GSA JDP MCP Toolkit

All GSA MCP servers for the AutoNOC Polaris project. Each server is a
self-contained Python script that speaks MCP over **stdio** and can be
imported into watsonx Orchestrate as a local toolkit or run directly from
the command line.

No API keys are required for any read-only tool. Only
`gsa_jdp_post_servicenow_record` needs `SERVICENOW_*` credentials.

---

## Servers at a glance

| Server file | Toolkit YAML | MCP server name | Tools / Prompts |
|---|---|---|---|
| `server.py` | `toolkit.yaml` | `gsa-jdp-toolkit` | 5 tools — locations, weather, alerts, ServiceNow |
| `server_weather_forecast.py` | `toolkit_weather_forecast.yaml` | `gsa-mar-weather-forecast` | 2 tools — forecast text table + PNG chart |
| `server_analytics.py` | `toolkit_analytics.yaml` | `gsa-mar-analytics` | 2 tools — comms outage scan + building |
| `server_prompts.py` | `toolkit_prompts.yaml` | `gsa-mar-prompts` | 7 prompt starters |
| `server_combined.py` | `toolkit_combined.yaml` | `gsa-combined-toolkit` | **9 tools** — all of the above in one process |

---

## Tools reference

### `server.py` — JDP core tools

| Tool | Description |
|---|---|
| `gsa_jdp_current_weather` | Current temperature, wind speed, WMO code at any lat/lon via Open-Meteo |
| `gsa_jdp_get_gsa_locations` | List GSA IOLP properties (BUILDING_RSF > 50k); filter by state, ownership, region |
| `gsa_jdp_get_gsa_location_detail` | Full detail for one GSA property by `LOCATION_CODE` (e.g. `DC0003`) |
| `gsa_jdp_get_gsa_weather_alerts` | Active weather/hazard alerts cross-referenced with GSA sites — NWS, USGS M4.5+, NOAA SWPC, JMA (Japan), DWD (Germany), MeteoAlarm (Italy) |
| `gsa_jdp_post_servicenow_record` | Apply a Weather Alert tag to a `cmn_location` record via ServiceNow OAuth. Severity 1 (Critical), 2 (Urgent), 3 (Advisory) only. Requires `SERVICENOW_*` env vars. |

### `server_weather_forecast.py` — MAR forecast tools

| Tool | Description |
|---|---|
| `gsa_mar_show_weather_forecasted_data` | Text table: historical (Open-Meteo) + NWS 7-day forecast for one or more GSA buildings. Parameters: temperature, precipitation, humidity, wind. |
| `gsa_mar_plot_weather_forecasted_data` | PNG seaborn chart of the same data. Temperature unit: °F (default) or °C. |

### `server_analytics.py` — MAR comms-outage tools

| Tool | Description |
|---|---|
| `gsa_mar_comms_outage_scan` | Rank all GSA sites in a state by communications outage probability using a logistic model (NWS alert severity, duration, compound overlap, destructive hazard). |
| `gsa_mar_comms_outage_building` | Score comms outage probability for a single GSA building by name, partial name, or location code. Returns per-alert model breakdown. |

### `server_combined.py` — all 9 tools in one process

Combines every tool from `server.py`, `server_weather_forecast.py`, and
`server_analytics.py` with no duplicates. Use this for agents that need the
full toolkit in a single MCP connection. This is the **default target** for
the Phoenix eval harness (`--target combined`).

### `server_prompts.py` — prompt starters

Seven reusable conversation starters (MCP prompts, not tools) for Claude /
wxO chat interfaces: list sites, check alerts, current weather, forecast
table, building comparison, comms outage scan, single-building outage.

---

## Dependencies

```
mcp>=2.0.0
requests>=2.31.0
matplotlib>=3.7.0
seaborn>=0.13.0
```

Install via the repo root venv (recommended — the eval harness uses it):

```bash
# from the repo root
uv sync --group dev
```

Or standalone:

```bash
cd tools/gsa_jdp_mcp_toolkit
pip install -r requirements.txt
```

---

## Running a server directly

```bash
# from the repo root
uv run python tools/gsa_jdp_mcp_toolkit/server.py
uv run python tools/gsa_jdp_mcp_toolkit/server_combined.py
```

Servers start in **stdio** mode — they wait for a client to connect over
stdin/stdout. Press `Ctrl-C` to stop.

---

## Importing into watsonx Orchestrate

Each server has a corresponding `toolkit_*.yaml`. Import from **this directory**
so `package_root: .` resolves correctly:

```bash
cd tools/gsa_jdp_mcp_toolkit

# Recommended: import the combined server (all 9 tools, one connection)
orchestrate toolkits import -f toolkit_combined.yaml

# Or import individual servers
orchestrate toolkits import -f toolkit.yaml                   # JDP core (5 tools)
orchestrate toolkits import -f toolkit_weather_forecast.yaml  # MAR forecast (2 tools)
orchestrate toolkits import -f toolkit_analytics.yaml         # MAR analytics (2 tools)
orchestrate toolkits import -f toolkit_prompts.yaml           # Prompt starters
```

Then import the agent:

```bash
orchestrate agents import -f ../../agents/gsa_jdp_agent.yaml
```

---

## Environment variables

### ServiceNow (required only for `gsa_jdp_post_servicenow_record`)

| Variable | Example | Purpose |
|---|---|---|
| `SERVICENOW_INSTANCE` | `dev12345.service-now.com` | Instance hostname |
| `SERVICENOW_CLIENT_ID` | *(OAuth client ID)* | OAuth password-grant client |
| `SERVICENOW_CLIENT_SECRET` | *(secret)* | OAuth password-grant secret |
| `SERVICENOW_USER` | `admin` | Service account username |
| `SERVICENOW_PASSWORD` | *(password)* | Service account password |

Set these in the repo root `.env` file (copy from `.env.example`). All other
tools use **public APIs** — no credentials needed.

---

## Testing

All 9 tools in `server_combined.py` have direct-call smoke tests in
`tests/test_agent_tools.py`. The original per-server tests are in
`tests/test_toolkit_tools.py`.

```bash
# from the repo root
uv run pytest tests/test_agent_tools.py -v     # 17 tests, all 9 combined tools
uv run pytest tests/test_toolkit_tools.py -v   # 25 tests, individual servers
uv run pytest tests/ -v                        # everything
```

Tests make live calls to public APIs (IOLP ArcGIS, Open-Meteo, NWS, USGS,
NOAA SWPC). Skip them on machines without internet access:

```bash
uv run pytest tests/ -v -m "not network"
```

---

## Evaluation (Arize Phoenix)

The Phoenix eval harness in `eval/phoenix/` measures how well an LLM agent
using only these tools can answer realistic questions. The `combined` target
uses `server_combined.py` with all 9 tools.

```bash
# 1. Start Phoenix (separate terminal)
uvx arize-phoenix serve          # or: uv tool run arize-phoenix serve

# 2. Upload datasets (one-time, from eval/phoenix/)
cd eval/phoenix
uv run create_dataset.py --dataset-name gsa-eval-0
uv run create_dataset.py --dataset-name gsa-weather-eval-0
uv run create_dataset.py --dataset-name gsa-weather-forecast-eval-0

# 3. Run an experiment
uv run run_experiment.py --dataset-name gsa-eval-0
uv run run_experiment.py --dataset-name gsa-eval-0 --target combined
```

See [`eval/phoenix/README.md`](../../eval/phoenix/README.md) for the full
setup and dataset details.
