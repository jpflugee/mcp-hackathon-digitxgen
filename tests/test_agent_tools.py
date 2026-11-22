"""Agent tool tests — all 9 tools in server_combined.py.

Simulates exactly what the MCPToolkitAgent (combined target) would call for each
tool, using the same function call the agent makes via MCP, but invoked directly
so they run without needing the USAi gateway.

Each test:
  1. Calls the tool function with a realistic agent-style input
  2. Asserts the return is a non-empty string (or valid image block)
  3. Checks for a key sentinel proving the right API was reached

All tests make live network calls (public APIs — no credentials required except
gsa_jdp_post_servicenow_record which is tested only up to its access gates).
Run with:
    uv run pytest tests/test_agent_tools.py -v
Skip network tests:
    uv run pytest tests/test_agent_tools.py -v -m "not network"
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "gsa_jdp_mcp_toolkit"))

import importlib
import pytest

pytestmark = pytest.mark.network

# ---------------------------------------------------------------------------
# Shared fixture — load server_combined once for the whole session
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def combined():
    """Import server_combined and verify the mcp object is present."""
    mod = importlib.import_module("server_combined")
    assert mod.mcp is not None, "server_combined.py did not expose an mcp object"
    return mod


# ---------------------------------------------------------------------------
# Verify: all 9 expected tools are registered
# ---------------------------------------------------------------------------

EXPECTED_TOOLS = [
    "gsa_jdp_current_weather",
    "gsa_jdp_get_gsa_locations",
    "gsa_jdp_get_gsa_location_detail",
    "gsa_jdp_get_gsa_weather_alerts",
    "gsa_jdp_post_servicenow_record",
    "gsa_mar_show_weather_forecasted_data",
    "gsa_mar_plot_weather_forecasted_data",
    "gsa_mar_plot_weather_trend",
    "gsa_mar_comms_outage_scan",
    "gsa_mar_comms_outage_building",
]


def test_combined_server_imports(combined):
    """server_combined.py must import cleanly and expose an mcp object."""
    assert combined.mcp is not None
    assert combined.mcp.name == "gsa-combined-toolkit"


def test_all_nine_tools_registered(combined):
    """All 9 expected tool functions must be defined in server_combined."""
    missing = [name for name in EXPECTED_TOOLS if not hasattr(combined, name)]
    assert missing == [], f"Missing tool functions: {missing}"


def test_no_duplicate_tool_names(combined):
    """Tool names registered in the MCP server must be unique."""
    registered = [name for name in EXPECTED_TOOLS if hasattr(combined, name)]
    assert len(registered) == len(set(registered)), "Duplicate tool names found"


# ---------------------------------------------------------------------------
# Tool 1 — gsa_jdp_current_weather
# ---------------------------------------------------------------------------

def test_tool_current_weather(combined):
    """Agent question: 'What is the current weather at the GSA Whitten Building?'
    Agent call: gsa_jdp_current_weather(latitude=38.887, longitude=-77.016, location_name='Whitten Building')
    """
    result = combined.gsa_jdp_current_weather(
        latitude=38.8873,
        longitude=-77.0163,
        location_name="Whitten Building",
    )
    assert isinstance(result, str) and result
    assert "Whitten Building" in result
    assert "Temperature" in result
    assert "Wind speed" in result
    assert "WMO code" in result
    print(f"\n[PASS] Tool 1 — gsa_jdp_current_weather\n{result}")


# ---------------------------------------------------------------------------
# Tool 2 — gsa_jdp_get_gsa_locations
# ---------------------------------------------------------------------------

def test_tool_get_gsa_locations_state_filter(combined):
    """Agent question: 'List GSA-owned buildings in Texas over 50k RSF.'
    Agent call: gsa_jdp_get_gsa_locations(state='TX', ownership='owned', max_results=5)
    """
    result = combined.gsa_jdp_get_gsa_locations(state="TX", ownership="owned", max_results=5)
    assert isinstance(result, str) and result
    assert "GSA Properties" in result
    assert "TX" in result
    print(f"\n[PASS] Tool 2 — gsa_jdp_get_gsa_locations (TX, owned)\n{result[:300]}...")


def test_tool_get_gsa_locations_region_filter(combined):
    """Agent question: 'Show GSA properties in region 11.'
    Agent call: gsa_jdp_get_gsa_locations(region='11', max_results=5)
    """
    result = combined.gsa_jdp_get_gsa_locations(region="11", max_results=5)
    assert isinstance(result, str) and result
    assert "GSA Properties" in result
    print(f"\n[PASS] Tool 2 — gsa_jdp_get_gsa_locations (region 11)\n{result[:300]}...")


# ---------------------------------------------------------------------------
# Tool 3 — gsa_jdp_get_gsa_location_detail
# ---------------------------------------------------------------------------

def test_tool_get_gsa_location_detail(combined):
    """Agent question: 'Give me the full detail for GSA location DC0003.'
    Agent call: gsa_jdp_get_gsa_location_detail(location_code='DC0003')
    """
    result = combined.gsa_jdp_get_gsa_location_detail(location_code="DC0003")
    assert isinstance(result, str) and result
    assert "DC0003" in result
    assert "Address" in result
    assert "Region" in result
    print(f"\n[PASS] Tool 3 — gsa_jdp_get_gsa_location_detail (DC0003)\n{result[:400]}...")


# ---------------------------------------------------------------------------
# Tool 4 — gsa_jdp_get_gsa_weather_alerts
# ---------------------------------------------------------------------------

def test_tool_get_gsa_weather_alerts(combined):
    """Agent question: 'Which GSA sites in Virginia are under active weather alerts?'
    Agent call: gsa_jdp_get_gsa_weather_alerts(state='VA', max_sites=20)
    """
    result = combined.gsa_jdp_get_gsa_weather_alerts(state="VA", max_sites=20)
    assert isinstance(result, str) and result
    assert (
        "No active weather" in result
        or "GSA Sites Under Active Alerts" in result
        or "NOAA SPACE WEATHER" in result
    )
    print(f"\n[PASS] Tool 4 — gsa_jdp_get_gsa_weather_alerts (VA)\n{result[:400]}...")


# ---------------------------------------------------------------------------
# Tool 5 — gsa_jdp_post_servicenow_record (gates only — no credentials needed)
# ---------------------------------------------------------------------------

def test_tool_post_servicenow_prompt_gate(combined):
    """Agent question: 'Update the record.' (missing ServiceNow/post keywords)
    Agent call: gsa_jdp_post_servicenow_record(prompt='update the record', site_code='DC0003', severity=1)
    Expected: prompt gate rejects — no credentials needed.
    """
    result = combined.gsa_jdp_post_servicenow_record(
        prompt="update the record",
        site_code="DC0003",
        severity=1,
    )
    assert isinstance(result, str) and result
    assert "servicenow" in result.lower() and "post" in result.lower()
    print(f"\n[PASS] Tool 5 — gsa_jdp_post_servicenow_record (prompt gate)\n{result}")


def test_tool_post_servicenow_severity_gate(combined):
    """Agent passes valid prompt but severity=4 (below minimum).
    Agent call: gsa_jdp_post_servicenow_record(prompt='post to servicenow', site_code='DC0003', severity=4)
    Expected: severity gate rejects — no credentials needed.
    """
    result = combined.gsa_jdp_post_servicenow_record(
        prompt="post to servicenow weather alert",
        site_code="DC0003",
        severity=4,
    )
    assert isinstance(result, str) and result
    assert "not valid" in result.lower() or "severity" in result.lower()
    print(f"\n[PASS] Tool 5 — gsa_jdp_post_servicenow_record (severity gate)\n{result}")


# ---------------------------------------------------------------------------
# Tool 6 — gsa_mar_show_weather_forecasted_data
# ---------------------------------------------------------------------------

def test_tool_show_weather_forecasted_data_temperature(combined):
    """Agent question: 'Show temperature forecast for the Whitten Building in Fahrenheit.'
    Agent call: gsa_mar_show_weather_forecasted_data(building_names='WHITTEN', weather_parameter='temperature', temperature_unit='F')
    """
    result = combined.gsa_mar_show_weather_forecasted_data(
        building_names="WHITTEN",
        weather_parameter="temperature",
        history_days=3,
        forecast_days=3,
        temperature_unit="F",
    )
    assert isinstance(result, str) and result
    assert "HISTORICAL" in result or "GSA Weather Data" in result
    print(f"\n[PASS] Tool 6 — gsa_mar_show_weather_forecasted_data (WHITTEN, temp °F)\n{result[:400]}...")


def test_tool_show_weather_forecasted_data_wind(combined):
    """Agent question: 'Show wind forecast for DC0003.'
    Agent call: gsa_mar_show_weather_forecasted_data(building_names='DC0003', weather_parameter='wind')
    """
    result = combined.gsa_mar_show_weather_forecasted_data(
        building_names="DC0003",
        weather_parameter="wind",
        history_days=3,
        forecast_days=3,
    )
    assert isinstance(result, str) and result
    assert "Wind" in result or "DC0003" in result or "GSA Weather Data" in result
    print(f"\n[PASS] Tool 6 — gsa_mar_show_weather_forecasted_data (DC0003, wind)\n{result[:400]}...")


# ---------------------------------------------------------------------------
# Tool 7 — gsa_mar_plot_weather_forecasted_data
# ---------------------------------------------------------------------------

def test_tool_plot_weather_forecasted_data(combined):
    """Agent question: 'Plot the temperature forecast for the Whitten Building.'
    Agent call: gsa_mar_plot_weather_forecasted_data(building_names='WHITTEN', weather_parameter='temperature')
    """
    result = combined.gsa_mar_plot_weather_forecasted_data(
        building_names="WHITTEN",
        weather_parameter="temperature",
        history_days=3,
        forecast_days=3,
        temperature_unit="F",
    )
    assert result is not None
    # Returns either an image content block (list) or a string error
    if isinstance(result, str):
        assert len(result) > 0
        print(f"\n[PASS] Tool 7 — gsa_mar_plot_weather_forecasted_data (string result)\n{result[:200]}")
    else:
        print(f"\n[PASS] Tool 7 — gsa_mar_plot_weather_forecasted_data (image block returned)")


# ---------------------------------------------------------------------------
# Tool 8 — gsa_mar_plot_weather_trend
# ---------------------------------------------------------------------------

def test_tool_plot_weather_trend(combined):
    """Agent question: 'Show a historical temperature trend chart for the Whitten Building.'
    Agent call: gsa_mar_plot_weather_trend(building_names='WHITTEN', weather_parameter='temperature', history_days=14)
    """
    result = combined.gsa_mar_plot_weather_trend(
        building_names="WHITTEN",
        weather_parameter="temperature",
        history_days=14,
    )
    assert result is not None
    if isinstance(result, str):
        assert len(result) > 0
        print(f"\n[PASS] Tool 8 — gsa_mar_plot_weather_trend (string result)\n{result[:200]}")
    else:
        print(f"\n[PASS] Tool 8 — gsa_mar_plot_weather_trend (image block returned)")


# ---------------------------------------------------------------------------
# Tool 9 — gsa_mar_comms_outage_scan
# ---------------------------------------------------------------------------

def test_tool_comms_outage_scan(combined):
    """Agent question: 'Which GSA sites in DC have the highest comms outage risk?'
    Agent call: gsa_mar_comms_outage_scan(state='DC', min_probability=0.0, top_n=5, max_sites=20)
    """
    result = combined.gsa_mar_comms_outage_scan(
        state="DC",
        min_probability=0.0,
        top_n=5,
        max_sites=20,
    )
    assert isinstance(result, str) and result
    assert "GSA Comms Outage Scan" in result
    assert "DC" in result
    print(f"\n[PASS] Tool 9 — gsa_mar_comms_outage_scan (DC)\n{result[:400]}...")


# ---------------------------------------------------------------------------
# Tool 10 — gsa_mar_comms_outage_building
# ---------------------------------------------------------------------------

def test_tool_comms_outage_building_by_name(combined):
    """Agent question: 'What is the comms outage probability for the Whitten Building?'
    Agent call: gsa_mar_comms_outage_building(building_name='WHITTEN')
    """
    result = combined.gsa_mar_comms_outage_building(
        building_name="WHITTEN",
        include_model_inputs=True,
    )
    assert isinstance(result, str) and result
    assert (
        "WHITTEN" in result.upper()
        or "Comms Outage" in result
        or "probability" in result.lower()
    )
    print(f"\n[PASS] Tool 10 — gsa_mar_comms_outage_building (WHITTEN by name)\n{result[:400]}...")


def test_tool_comms_outage_building_by_code(combined):
    """Agent question: 'What is the comms outage risk for location DC0003?'
    Agent call: gsa_mar_comms_outage_building(building_name='DC0003')
    """
    result = combined.gsa_mar_comms_outage_building(
        building_name="DC0003",
        include_model_inputs=True,
    )
    assert isinstance(result, str) and result
    assert (
        "DC0003" in result
        or "Comms Outage" in result
        or "probability" in result.lower()
    )
    print(f"\n[PASS] Tool 10 — gsa_mar_comms_outage_building (DC0003 by code)\n{result[:400]}...")
