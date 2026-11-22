"""Smoke tests for all GSA JDP MCP toolkit tools.

Each test calls the tool function directly (no MCP transport) and asserts that:
  - the call returns a non-empty string (not an exception)
  - the returned text contains a key sentinel that proves the right API was hit

Tests that require live external APIs are marked @pytest.mark.network so they
can be skipped in environments without internet access:
  pytest -m "not network"

Run all (including network) with:
  uv run pytest tests/test_toolkit_tools.py -v
"""

from __future__ import annotations

import sys
import os

# Allow importing the toolkit modules even though they live outside src/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "gsa_jdp_mcp_toolkit"))

import pytest

# ---------------------------------------------------------------------------
# Mark all tests in this file as network tests (they hit public APIs)
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.network


# ---------------------------------------------------------------------------
# server.py — 5 tools
# ---------------------------------------------------------------------------

class TestServerPy:
    """Tests for tools/gsa_jdp_mcp_toolkit/server.py"""

    @pytest.fixture(scope="class")
    @classmethod
    def server(cls):
        import importlib
        return importlib.import_module("server")

    def test_import(self, server):
        """server.py must import without error and expose an mcp object."""
        assert server.mcp is not None

    def test_current_weather_washington_dc(self, server):
        """gsa_jdp_current_weather — hits Open-Meteo for Washington D.C."""
        result = server.gsa_jdp_current_weather(
            latitude=38.9072,
            longitude=-77.0369,
            location_name="Washington D.C.",
        )
        assert isinstance(result, str), "tool must return a string"
        assert "Washington D.C." in result
        assert "Temperature" in result
        assert "Wind speed" in result
        assert "WMO code" in result

    def test_get_gsa_locations_dc_state(self, server):
        """gsa_jdp_get_gsa_locations — queries IOLP for DC properties."""
        result = server.gsa_jdp_get_gsa_locations(state="DC", max_results=5)
        assert isinstance(result, str)
        assert "GSA Properties" in result
        # At minimum one result should be returned for DC
        assert "DC" in result

    def test_get_gsa_locations_owned_filter(self, server):
        """gsa_jdp_get_gsa_locations — ownership filter: owned."""
        result = server.gsa_jdp_get_gsa_locations(state="DC", ownership="owned", max_results=5)
        assert isinstance(result, str)
        assert "GSA Properties" in result

    def test_get_gsa_locations_bad_ownership(self, server):
        """gsa_jdp_get_gsa_locations — invalid ownership returns a helpful error."""
        result = server.gsa_jdp_get_gsa_locations(ownership="banana")
        assert isinstance(result, str)
        assert "Unrecognised" in result or "not" in result.lower()

    def test_get_gsa_locations_no_filter(self, server):
        """gsa_jdp_get_gsa_locations — no filters, capped at 10 results."""
        result = server.gsa_jdp_get_gsa_locations(max_results=10)
        assert isinstance(result, str)
        assert "GSA Properties" in result

    def test_get_gsa_location_detail_whitten(self, server):
        """gsa_jdp_get_gsa_location_detail — full detail for a known building."""
        result = server.gsa_jdp_get_gsa_location_detail("DC0003")
        assert isinstance(result, str)
        # Should contain the location code header and core fields
        assert "DC0003" in result
        assert "Address" in result
        assert "Region" in result

    def test_get_gsa_location_detail_missing(self, server):
        """gsa_jdp_get_gsa_location_detail — nonexistent code returns a clear message."""
        result = server.gsa_jdp_get_gsa_location_detail("ZZZZZZ")
        assert isinstance(result, str)
        assert "No GSA location found" in result or "ZZZZZZ" in result

    def test_get_gsa_location_detail_empty(self, server):
        """gsa_jdp_get_gsa_location_detail — empty code returns a helpful error."""
        result = server.gsa_jdp_get_gsa_location_detail("")
        assert isinstance(result, str)
        assert "required" in result.lower() or "code" in result.lower()

    def test_get_gsa_weather_alerts_dc(self, server):
        """gsa_jdp_get_gsa_weather_alerts — runs alert check for DC sites."""
        result = server.gsa_jdp_get_gsa_weather_alerts(state="DC", max_sites=10)
        assert isinstance(result, str)
        # Either alerts or a "no alerts found" summary — both are valid
        assert (
            "No active weather" in result
            or "GSA Sites Under Active Alerts" in result
            or "NOAA SPACE WEATHER" in result
        )

    def test_post_servicenow_bad_prompt(self, server):
        """gsa_jdp_post_servicenow_record — prompt gate rejects missing keywords."""
        result = server.gsa_jdp_post_servicenow_record(
            prompt="please update the record",
            site_code="DC0003",
            severity=1,
        )
        assert isinstance(result, str)
        assert "ServiceNow" in result or "servicenow" in result.lower()
        assert "post" in result.lower()

    def test_post_servicenow_bad_severity(self, server):
        """gsa_jdp_post_servicenow_record — invalid severity returns a clear error."""
        result = server.gsa_jdp_post_servicenow_record(
            prompt="post to servicenow weather alert",
            site_code="DC0003",
            severity=99,
        )
        assert isinstance(result, str)
        assert "not valid" in result.lower() or "Severity" in result


# ---------------------------------------------------------------------------
# server_weather_forecast.py — 2 tools
# ---------------------------------------------------------------------------

class TestServerWeatherForecast:
    """Tests for tools/gsa_jdp_mcp_toolkit/server_weather_forecast.py"""

    @pytest.fixture(scope="class")
    @classmethod
    def server(cls):
        import importlib
        return importlib.import_module("server_weather_forecast")

    def test_import(self, server):
        """server_weather_forecast.py must import and expose an mcp object."""
        assert server.mcp is not None

    def test_show_weather_forecasted_data_whitten(self, server):
        """gsa_mar_show_weather_forecasted_data — text table for WHITTEN building."""
        result = server.gsa_mar_show_weather_forecasted_data(
            building_names="WHITTEN",
            weather_parameter="temperature",
            history_days=3,
            forecast_days=3,
            temperature_unit="F",
        )
        assert isinstance(result, str)
        # Should have both historical and forecast sections
        assert "HISTORICAL" in result or "GSA Weather Data" in result or "WHITTEN" in result.upper()

    def test_show_weather_forecasted_data_precipitation(self, server):
        """gsa_mar_show_weather_forecasted_data — precipitation parameter."""
        result = server.gsa_mar_show_weather_forecasted_data(
            building_names="DC0003",
            weather_parameter="precipitation",
            history_days=3,
            forecast_days=3,
        )
        assert isinstance(result, str)
        assert "Precipitation" in result or "DC0003" in result or "No GSA buildings found" in result

    def test_show_weather_forecasted_data_bad_param(self, server):
        """gsa_mar_show_weather_forecasted_data — unknown parameter returns helpful error."""
        result = server.gsa_mar_show_weather_forecasted_data(
            building_names="WHITTEN",
            weather_parameter="flurbosity",
        )
        assert isinstance(result, str)
        assert "Unknown parameter" in result or "Choose from" in result

    def test_show_weather_forecasted_data_not_found(self, server):
        """gsa_mar_show_weather_forecasted_data — nonexistent building returns error."""
        result = server.gsa_mar_show_weather_forecasted_data(
            building_names="ZZZZZZ_NONEXISTENT_BUILDING_XYZZY",
        )
        assert isinstance(result, str)
        assert "No GSA buildings found" in result or "not found" in result.lower()

    def test_plot_weather_forecasted_data_whitten(self, server):
        """gsa_mar_plot_weather_forecasted_data — returns a PNG or error string."""
        result = server.gsa_mar_plot_weather_forecasted_data(
            building_names="WHITTEN",
            weather_parameter="temperature",
            history_days=3,
            forecast_days=3,
            temperature_unit="F",
        )
        assert result is not None
        # Accepts either an image content list (FastMCP) or a plain string fallback
        if isinstance(result, str):
            # String means the tool reported an error or fallback message
            assert len(result) > 0
        else:
            # Should be an image content block from FastMCP / mcp
            assert result is not None

    def test_plot_weather_forecasted_data_bad_param(self, server):
        """gsa_mar_plot_weather_forecasted_data — bad parameter returns error."""
        result = server.gsa_mar_plot_weather_forecasted_data(
            building_names="WHITTEN",
            weather_parameter="nonsense",
        )
        assert result is not None
        if isinstance(result, str):
            assert "Unknown parameter" in result or "Choose from" in result


# ---------------------------------------------------------------------------
# server_analytics.py — 2 tools
# ---------------------------------------------------------------------------

class TestServerAnalytics:
    """Tests for tools/gsa_jdp_mcp_toolkit/server_analytics.py"""

    @pytest.fixture(scope="class")
    @classmethod
    def server(cls):
        import importlib
        return importlib.import_module("server_analytics")

    def test_import(self, server):
        """server_analytics.py must import and expose an mcp object."""
        assert server.mcp is not None

    def test_comms_outage_scan_dc(self, server):
        """gsa_mar_comms_outage_scan — runs area scan for DC (small)."""
        result = server.gsa_mar_comms_outage_scan(
            state="DC",
            min_probability=0.0,  # return all sites regardless of probability
            top_n=5,
            max_sites=10,
        )
        assert isinstance(result, str)
        assert "GSA Comms Outage Scan" in result
        assert "DC" in result

    def test_comms_outage_scan_bad_state(self, server):
        """gsa_mar_comms_outage_scan — nonexistent state returns a clear message."""
        result = server.gsa_mar_comms_outage_scan(
            state="ZZ",
            min_probability=0.0,
            top_n=5,
            max_sites=5,
        )
        assert isinstance(result, str)
        # Either "No GSA sites found" or the scan header with 0 results
        assert "No GSA sites found" in result or "ZZ" in result or "GSA Comms Outage Scan" in result

    def test_comms_outage_building_whitten(self, server):
        """gsa_mar_comms_outage_building — scores a known building by name."""
        result = server.gsa_mar_comms_outage_building(
            building_name="WHITTEN",
            include_model_inputs=True,
        )
        assert isinstance(result, str)
        # Should return either a scored result or "no alerts" for the site
        assert (
            "WHITTEN" in result.upper()
            or "Comms Outage" in result
            or "probability" in result.lower()
        )

    def test_comms_outage_building_by_code(self, server):
        """gsa_mar_comms_outage_building — also accepts location codes."""
        result = server.gsa_mar_comms_outage_building(
            building_name="DC0003",
            include_model_inputs=False,
        )
        assert isinstance(result, str)
        assert "DC0003" in result or "Comms Outage" in result or "probability" in result.lower()

    def test_comms_outage_building_not_found(self, server):
        """gsa_mar_comms_outage_building — nonexistent building returns a clear error."""
        result = server.gsa_mar_comms_outage_building(
            building_name="ZZZZZZ_NONEXISTENT_XYZZY",
        )
        assert isinstance(result, str)
        assert "No GSA building found" in result or "not found" in result.lower()
