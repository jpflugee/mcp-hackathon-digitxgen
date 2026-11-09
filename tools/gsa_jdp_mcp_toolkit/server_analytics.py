#!/usr/bin/env python3
"""GSA JDP Analytics MCP Server — plotting and analysis tools served over stdio.

Completely separate from server.py (James's code). Shares no state with it.
Fetches GSA building coordinates from the IOLP ArcGIS API and historical
weather data from the Open-Meteo archive API (no API key required).

Tools
-----
gsa_jdp_plot_weather_trend  — historical weather trend chart (seaborn PNG) for
                               one or more GSA buildings
"""
import sys
import os
import datetime
import math
import requests
from concurrent.futures import ThreadPoolExecutor
from mcp.server.mcpserver import MCPServer

mcp = MCPServer(name="gsa-jdp-analytics", version="0.1.0")

# ---------------------------------------------------------------------------
# IOLP helpers (self-contained — no imports from server.py)
# ---------------------------------------------------------------------------

_GSA_FEATURE_URL = (
    "https://services1.arcgis.com/eBupDfPlEJK3mdAm/ArcGIS/rest/services/"
    "IOLP_NEW/FeatureServer/0/query"
)
_GSA_FIELDS = (
    "LOCATION_CODE,Real_Property_Asset_Name,STREET_ADDRESS,CITY,STATE_CD,"
    "ZIPCODE5,Latitude,Longitude"
)


def _query_iolp(where: str, max_records: int = 5) -> list[dict]:
    """Page through IOLP and return attribute dicts."""
    results: list[dict] = []
    offset = 0
    page_size = min(2000, max_records)
    while True:
        resp = requests.get(
            _GSA_FEATURE_URL,
            params={
                "where": where,
                "outFields": _GSA_FIELDS,
                "returnGeometry": "false",
                "resultOffset": offset,
                "resultRecordCount": page_size,
                "f": "json",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"ArcGIS API error: {data['error']}")
        features = data.get("features", [])
        for feat in features:
            results.append(feat["attributes"])
        if len(features) < page_size or len(results) >= max_records:
            break
        offset += page_size
    return results[:max_records]


def _find_buildings_by_name(name_query: str) -> list[dict]:
    """Return up to 5 IOLP buildings whose name contains name_query."""
    safe = name_query.replace("'", "''")
    where = f"UPPER(Real_Property_Asset_Name) LIKE UPPER('%{safe}%')"
    return _query_iolp(where, max_records=5)


# ---------------------------------------------------------------------------
# Open-Meteo archive helpers
# ---------------------------------------------------------------------------

_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# Maps user-friendly names → (open-meteo daily variable, secondary variable or None, unit label)
_WEATHER_PARAMS: dict[str, tuple[str, str | None, str]] = {
    "temperature":   ("temperature_2m_max", "temperature_2m_min", "°C"),
    "temp":          ("temperature_2m_max", "temperature_2m_min", "°C"),
    "precipitation": ("precipitation_sum",  None,                  "mm"),
    "rainfall":      ("precipitation_sum",  None,                  "mm"),
    "rain":          ("precipitation_sum",  None,                  "mm"),
    "wind":          ("windspeed_10m_max",  None,                  "km/h"),
    "windspeed":     ("windspeed_10m_max",  None,                  "km/h"),
    "humidity":      ("relative_humidity_2m_max", None,            "%"),
}


def _fetch_historical_weather(
    lat: float,
    lon: float,
    var_main: str,
    var_secondary: str | None,
    days: int,
) -> dict:
    """Fetch daily historical weather from the Open-Meteo archive API."""
    end_date = datetime.date.today() - datetime.timedelta(days=1)
    start_date = end_date - datetime.timedelta(days=days - 1)
    daily_vars = [var_main] + ([var_secondary] if var_secondary else [])
    resp = requests.get(
        _ARCHIVE_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "daily": ",".join(daily_vars),
            "timezone": "auto",
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Tool: gsa_jdp_plot_weather_trend
# ---------------------------------------------------------------------------

@mcp.tool(
    name="gsa_jdp_plot_weather_trend",
    description=(
        "Fetch historical daily weather data for one or more GSA buildings and "
        "produce a seaborn line-chart PNG saved to disk. Buildings are looked up "
        "by partial name match in the GSA IOLP dataset. "
        "Supported weather parameters: temperature, precipitation (rainfall), "
        "wind (windspeed), humidity. "
        "Returns the full path of the saved PNG file and a data summary."
    ),
)
def gsa_jdp_plot_weather_trend(
    building_names: str,
    weather_parameter: str = "temperature",
    days: int = 10,
    output_dir: str = "",
) -> str:
    """Plot a historical weather trend for one or more GSA buildings.

    Args:
        building_names: Comma-separated GSA building names or partial names
                        (e.g. 'WHITTEN, AGRI SOUTH'). Each is matched against
                        the IOLP Real_Property_Asset_Name field.
        weather_parameter: Variable to plot. One of: temperature, precipitation,
                           rainfall, wind, windspeed, humidity. Default: temperature.
        days: Number of past days to include (3–30). Default: 10.
        output_dir: Directory to save the PNG. Defaults to the system temp directory.

    Returns:
        Full file path of the saved PNG chart plus a data summary.
    """
    import matplotlib
    matplotlib.use("Agg")  # non-interactive backend — no display required
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    import seaborn as sns

    # ── 1. Validate inputs ───────────────────────────────────────────────────
    param_key = weather_parameter.strip().lower()
    if param_key not in _WEATHER_PARAMS:
        valid = ", ".join(sorted(set(_WEATHER_PARAMS.keys())))
        return f"Unknown weather_parameter '{weather_parameter}'. Choose from: {valid}."

    var_main, var_secondary, unit = _WEATHER_PARAMS[param_key]
    days = max(3, min(int(days), 30))

    names = [n.strip() for n in building_names.split(",") if n.strip()]
    if not names:
        return "Please provide at least one building name in building_names."

    # ── 2. Look up each building in IOLP ────────────────────────────────────
    building_records: list[dict] = []
    not_found: list[str] = []

    for query in names:
        hits = _find_buildings_by_name(query)
        chosen = next(
            (h for h in hits if h.get("Latitude") and h.get("Longitude")),
            None,
        )
        if not chosen:
            not_found.append(query)
            continue
        building_records.append({
            "name":  chosen.get("Real_Property_Asset_Name", query),
            "lat":   float(chosen["Latitude"]),
            "lon":   float(chosen["Longitude"]),
            "city":  chosen.get("CITY", ""),
            "state": chosen.get("STATE_CD", ""),
        })

    if not building_records:
        return "No buildings with coordinates found for: " + ", ".join(not_found)

    # ── 3. Fetch weather data concurrently ───────────────────────────────────
    def _load(rec: dict) -> tuple[dict, dict]:
        return rec, _fetch_historical_weather(
            rec["lat"], rec["lon"], var_main, var_secondary, days
        )

    weather_data: list[tuple[dict, dict]] = []
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_load, rec): rec for rec in building_records}
        for fut in futures:
            try:
                weather_data.append(fut.result())
            except Exception as exc:
                errors.append(f"{futures[fut]['name']}: {exc}")

    if not weather_data:
        return "Failed to fetch weather data. Errors: " + "; ".join(errors)

    # ── 4. Build seaborn plot ────────────────────────────────────────────────
    sns.set_theme(style="whitegrid", font_scale=1.0)
    fig, ax = plt.subplots(figsize=(12, 5))
    n_series = len(weather_data) * (2 if var_secondary else 1)
    palette = sns.color_palette("tab10", n_colors=max(n_series, 1))
    colour_idx = 0
    summary_lines: list[str] = []

    for rec, data in weather_data:
        daily = data.get("daily", {})
        raw_dates = daily.get("time", [])
        values_main = daily.get(var_main, [])

        if not raw_dates or not values_main:
            errors.append(f"No data returned for {rec['name']}")
            continue

        # Use proper date objects so matplotlib renders a real time axis
        date_objs = [datetime.date.fromisoformat(d) for d in raw_dates]
        vals_m = [v if v is not None else float("nan") for v in values_main]
        label_max = f"{rec['name']} ({rec['city']}, {rec['state']})"
        label_max += " — max" if var_secondary else ""

        ax.plot(date_objs, vals_m, marker="o", label=label_max,
                color=palette[colour_idx], linewidth=2)
        colour_idx += 1

        if var_secondary:
            values_sec = daily.get(var_secondary, [])
            vals_s = [v if v is not None else float("nan") for v in values_sec]
            label_min = f"{rec['name']} ({rec['city']}, {rec['state']}) — min"
            ax.plot(date_objs, vals_s, marker="o", linestyle="--", label=label_min,
                    color=palette[colour_idx], linewidth=1.5, alpha=0.7)
            colour_idx += 1
            valid_m = [v for v in vals_m if not math.isnan(v)]
            valid_s = [v for v in vals_s if not math.isnan(v)]
            if valid_m and valid_s:
                summary_lines.append(
                    f"{rec['name']}: max {max(valid_m):.1f}{unit}, "
                    f"min {min(valid_s):.1f}{unit} over {days} days"
                )
        else:
            valid_m = [v for v in vals_m if not math.isnan(v)]
            if valid_m:
                summary_lines.append(
                    f"{rec['name']}: avg {sum(valid_m)/len(valid_m):.1f}{unit}, "
                    f"max {max(valid_m):.1f}{unit}, min {min(valid_m):.1f}{unit} "
                    f"over {days} days"
                )

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.xaxis.set_major_locator(mdates.DayLocator())
    plt.xticks(rotation=45, ha="right")
    ax.set_xlabel("Date")
    ax.set_ylabel(f"{weather_parameter.capitalize()} ({unit})")
    ax.set_title(
        f"GSA Buildings — {weather_parameter.capitalize()} trend (last {days} days)"
    )
    ax.legend(loc="best", fontsize=8)
    plt.tight_layout()

    # ── 5. Save PNG ──────────────────────────────────────────────────────────
    import tempfile
    save_dir = output_dir.strip() if output_dir.strip() else tempfile.gettempdir()
    os.makedirs(save_dir, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(save_dir, f"gsa_weather_{param_key}_{timestamp}.png")
    fig.savefig(filepath, dpi=150, bbox_inches="tight")
    plt.close(fig)

    # ── 6. Return summary ────────────────────────────────────────────────────
    result_lines = [
        f"Chart saved: {filepath}",
        f"Parameter  : {weather_parameter} ({unit})",
        f"Period     : last {days} days",
        f"Buildings  : {len(weather_data)} plotted",
    ]
    if summary_lines:
        result_lines += [""] + summary_lines
    if not_found:
        result_lines += ["", "Not found in IOLP: " + ", ".join(not_found)]
    if errors:
        result_lines += ["", "Errors: " + "; ".join(errors)]

    return "\n".join(result_lines)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Starting gsa-jdp-analytics on stdio...", file=sys.stderr)
    mcp.run(transport="stdio")
