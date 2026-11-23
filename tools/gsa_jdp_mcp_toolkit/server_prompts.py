#!/usr/bin/env python3
"""GSA MAR Prompts MCP Server — reusable conversation starters.

Completely separate from server.py (James's code).
Prompts appear as one-click starters in Claude / wxO chat interfaces.

Prompts
-------
list_gsa_sites_in_area          — list GSA sites in a given state/area
list_gsa_sites_with_alerts      — list GSA sites with active weather alerts in an area
current_weather_for_site        — show current weather for a GSA site (James's tool)
forecasted_weather_for_building — show historical + NWS forecast table for a building
compare_buildings_forecast      — compare forecast weather: GSA HQ vs VA Norfolk sites
comms_outage_state_scan         — scan all GSA sites in a state for comms outage risk
comms_outage_single_building    — detailed comms outage scoring for one GSA building
"""
import sys
from mcp.server.mcpserver import MCPServer

mcp = MCPServer(name="gsa-mar-prompts", version="0.3.0")


# ---------------------------------------------------------------------------
# Prompt 1: List GSA sites in a given area
# ---------------------------------------------------------------------------

@mcp.prompt(
    name="list_gsa_sites_in_area",
    description=(
        "List GSA-owned and leased properties in a given US state or GSA region. "
        "Uses James's gsa_jdp_get_gsa_locations tool. "
        "Returns location codes, names, addresses, and coordinates."
    ),
)
def list_gsa_sites_in_area(
    state: str = "VA",
    ownership: str = "",
    max_results: int = 50,
) -> str:
    """Starter prompt for listing GSA sites in an area.

    Args:
        state: Two-letter US state code (e.g. 'VA', 'DC', 'TX').
        ownership: Optional — 'owned', 'leased', or blank for both.
        max_results: Maximum number of results to return (default 50).
    """
    ownership_clause = f" with ownership='{ownership}'" if ownership.strip() else ""
    return (
        f"Use the gsa_jdp_get_gsa_locations tool to list GSA properties in "
        f"state='{state}'{ownership_clause}, max_results={max_results}. "
        f"Present the results as a clear list showing: location code, building name, "
        f"full address, ownership type, region, year built, and size in RSF. "
        f"Count the total number of sites returned and note whether any results were limited."
    )


# ---------------------------------------------------------------------------
# Prompt 2: List GSA sites with active weather alerts in an area
# ---------------------------------------------------------------------------

@mcp.prompt(
    name="list_gsa_sites_with_alerts",
    description=(
        "List all GSA sites in a given US state that currently have active weather "
        "or hazard alerts. Uses James's gsa_jdp_get_gsa_weather_alerts tool. "
        "Covers NWS, USGS earthquakes, NOAA SWPC space weather, JMA, DWD, and MeteoAlarm."
    ),
)
def list_gsa_sites_with_alerts(
    state: str = "VA",
    severity_filter: str = "",
    max_sites: int = 200,
) -> str:
    """Starter prompt for listing GSA sites with active weather alerts.

    Args:
        state: Two-letter US state code (e.g. 'VA', 'TX', 'FL').
        severity_filter: Optional minimum severity — 'Extreme', 'Severe', 'Moderate', 'Minor'.
        max_sites: Maximum number of GSA sites to check (default 200).
    """
    severity_clause = (
        f" Filter to severity='{severity_filter}' and above."
        if severity_filter.strip() else ""
    )
    return (
        f"Use the gsa_jdp_get_gsa_weather_alerts tool with state='{state}', "
        f"max_sites={max_sites}.{severity_clause} "
        f"List every GSA site currently under an active alert. "
        f"For each site show: location code, building name, full address, "
        f"and all active alert types with their source (NWS / USGS / NOAA SWPC / "
        f"JMA / DWD / MeteoAlarm) and severity level. "
        f"Sort results most-severe first. "
        f"If no alerts are found, confirm the total number of sites checked."
    )


# ---------------------------------------------------------------------------
# Prompt 3: Show current weather using James's tool
# ---------------------------------------------------------------------------

@mcp.prompt(
    name="current_weather_for_site",
    description=(
        "Show the current live weather conditions for a specific GSA building. "
        "First looks up the building's coordinates using gsa_jdp_get_gsa_locations, "
        "then fetches current weather using James's gsa_jdp_current_weather tool."
    ),
)
def current_weather_for_site(
    building_name: str = "JAMES L. WHITTEN FB",
    state: str = "DC",
) -> str:
    """Starter prompt for showing current weather at a GSA site using James's tool.

    Args:
        building_name: Full or partial GSA building name (e.g. 'WHITTEN').
        state: Two-letter US state code to narrow the search (e.g. 'DC').
    """
    return (
        f"Step 1: Use gsa_jdp_get_gsa_locations with state='{state}' to find the "
        f"GSA building matching '{building_name}'. Extract its latitude and longitude "
        f"and its full official name.\n\n"
        f"Step 2: Use gsa_jdp_current_weather with those coordinates and "
        f"location_name set to the building's official name. "
        f"Report the current temperature (°C), wind speed (km/h), and WMO "
        f"weather condition code. Convert the WMO code to a plain-English "
        f"description (e.g. 0 = Clear sky, 61 = Rain, 95 = Thunderstorm)."
    )


# ---------------------------------------------------------------------------
# Prompt 4: Forecasted weather data for a building (5 days history, 2 days forecast)
# ---------------------------------------------------------------------------

@mcp.prompt(
    name="forecasted_weather_for_building",
    description=(
        "Show historical and NWS-forecasted weather for a GSA building as a text table. "
        "Defaults to 5 days of historical data and 2 days of NWS forecast. "
        "Uses the gsa_mar_show_weather_forecasted_data tool."
    ),
)
def forecasted_weather_for_building(
    building_name: str = "JAMES L. WHITTEN FB",
    weather_parameter: str = "temperature",
    history_days: int = 5,
    forecast_days: int = 2,
    temperature_unit: str = "F",
) -> str:
    """Starter prompt for showing historical + forecast weather as a text table.

    Args:
        building_name: Full or partial GSA building name, or location code (e.g. 'DC0003').
        weather_parameter: One of: temperature, precipitation, humidity, wind.
        history_days: Past days of historical data (default 5).
        forecast_days: NWS forecast days to show (default 2).
        temperature_unit: 'F' for Fahrenheit (default) or 'C' for Celsius.
    """
    return (
        f"Use the gsa_mar_show_weather_forecasted_data tool to show "
        f"{weather_parameter} data for GSA building '{building_name}'. "
        f"Set history_days={history_days}, forecast_days={forecast_days}, "
        f"temperature_unit='{temperature_unit}'. "
        f"Present the results as a formatted table with historical rows first, "
        f"then the NWS forecast rows. "
        f"Highlight the highest and lowest values in each section and note "
        f"any significant changes between historical and forecast periods."
    )


# ---------------------------------------------------------------------------
# Prompt 5: Compare forecasted weather — GSA HQ (DC) vs VA Norfolk sites
# ---------------------------------------------------------------------------

@mcp.prompt(
    name="compare_buildings_forecast",
    description=(
        "Compare historical and NWS forecast weather between GSA Headquarters "
        "(WHITTEN building, Washington DC) and GSA sites in VA Norfolk. "
        "Uses gsa_mar_show_weather_forecasted_data for both locations side by side. "
        "Default buildings: JAMES L. WHITTEN FB (DC HQ) vs NORFOLK Federal Building (VA)."
    ),
)
def compare_buildings_forecast(
    hq_building: str = "JAMES L. WHITTEN FB",
    field_buildings: str = "NORFOLK",
    weather_parameter: str = "temperature",
    history_days: int = 5,
    forecast_days: int = 2,
    temperature_unit: str = "F",
) -> str:
    """Starter prompt for comparing forecast weather between GSA HQ and VA Norfolk sites.

    Args:
        hq_building: GSA HQ building name or code (default: JAMES L. WHITTEN FB, DC).
        field_buildings: Comma-separated Norfolk-area building names or codes.
        weather_parameter: One of: temperature, precipitation, humidity, wind.
        history_days: Past days of historical data (default 5).
        forecast_days: NWS forecast days to show (default 2).
        temperature_unit: 'F' for Fahrenheit (default) or 'C' for Celsius.
    """
    all_buildings = f"{hq_building}, {field_buildings}"
    return (
        f"Use the gsa_mar_show_weather_forecasted_data tool to compare "
        f"{weather_parameter} between GSA Headquarters and Norfolk-area GSA sites.\n\n"
        f"Call the tool once with building_names='{all_buildings}', "
        f"weather_parameter='{weather_parameter}', "
        f"history_days={history_days}, forecast_days={forecast_days}, "
        f"temperature_unit='{temperature_unit}'.\n\n"
        f"After showing the full table, provide a comparison summary:\n"
        f"- Which location is currently warmer/cooler?\n"
        f"- How does the {forecast_days}-day forecast differ between the two?\n"
        f"- Note any significant weather differences the GSA operations team should be aware of."
    )


# ---------------------------------------------------------------------------
# Prompt 6: State-wide communications outage risk scan
# ---------------------------------------------------------------------------

@mcp.prompt(
    name="comms_outage_state_scan",
    description=(
        "Scan ALL GSA sites in a given US state and rank them by their probability "
        "of a communications outage based on active NWS weather alerts. "
        "Uses the gsa_mar_comms_outage_scan tool. "
        "Returns a ranked table of at-risk sites with probability % and risk label."
    ),
)
def comms_outage_state_scan(
    state: str = "TX",
    min_probability: int = 50,
    top_n: int = 10,
) -> str:
    """Starter prompt for a state-wide communications outage risk scan.

    Args:
        state: Two-letter US state code to scan (e.g. 'TX', 'FL', 'NC').
        min_probability: Minimum outage probability % to include in results (default 50).
        top_n: Maximum number of at-risk sites to return (default 10).
    """
    return (
        f"Use the gsa_mar_comms_outage_scan tool with state='{state}', "
        f"min_probability={min_probability}, top_n={top_n}.\n\n"
        f"Present the results as a ranked table showing: rank, site code, building name, "
        f"full address, outage probability %, and risk label "
        f"(MINIMAL / LOW / MODERATE / HIGH / CRITICAL).\n\n"
        f"After the table, provide a brief summary:\n"
        f"- How many sites are at HIGH or CRITICAL risk?\n"
        f"- What are the dominant alert types driving the highest-risk sites?\n"
        f"- Which single site is most at risk and why?"
    )


# ---------------------------------------------------------------------------
# Prompt 7: Single-building communications outage detail
# ---------------------------------------------------------------------------

@mcp.prompt(
    name="comms_outage_single_building",
    description=(
        "Show a detailed communications outage risk breakdown for one specific GSA building. "
        "Uses the gsa_mar_comms_outage_building tool. "
        "Returns per-alert scoring inputs (S, D, C, H), final probability, and risk label. "
        "If no alerts are active, reports the baseline probability (~5.7%)."
    ),
)
def comms_outage_single_building(
    building_name: str = "JAMES L. WHITTEN FB",
) -> str:
    """Starter prompt for a single-building communications outage risk assessment.

    Args:
        building_name: Full or partial GSA building name, or location code (e.g. 'DC0003').
    """
    return (
        f"Use the gsa_mar_comms_outage_building tool with building_name='{building_name}'.\n\n"
        f"Present the results in two parts:\n\n"
        f"Part 1 — Per-Alert Scoring Table:\n"
        f"Show each active NWS alert with its scoring inputs:\n"
        f"  - Alert event name and severity\n"
        f"  - S (severity score: Minor=1, Moderate=2, Severe=3, Extreme=4)\n"
        f"  - D (duration in hours, capped at 12)\n"
        f"  - C (1 if 2+ alerts overlap at this site, else 0)\n"
        f"  - H (1 if a high-impact alert type is active, else 0)\n"
        f"  - Individual alert probability %\n\n"
        f"Part 2 — Final Assessment:\n"
        f"  - Combined outage probability %\n"
        f"  - Risk label (MINIMAL / LOW / MODERATE / HIGH / CRITICAL)\n"
        f"  - Plain-English recommendation: should GSA operations take precautionary action?\n\n"
        f"If no alerts are active, state the baseline probability and confirm the site is clear."
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Starting gsa-mar-prompts on stdio...", file=sys.stderr)
    mcp.run(transport="stdio")
