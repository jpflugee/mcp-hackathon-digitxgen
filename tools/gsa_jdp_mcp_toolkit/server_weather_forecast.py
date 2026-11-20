#!/usr/bin/env python3
"""GSA MAR Weather Forecast MCP Server — historical + forecast weather over stdio.

Completely independent of server.py (James's code) and server_analytics.py.
Building names are looked up from the same GSA IOLP dataset that James's tools
use, so any building name that works with gsa_jdp_get_gsa_locations will also
work here.

Tools
-----
gsa_mar_plot_weather_forecasted_data  — seaborn line chart (PNG) showing
                                         historical + NWS forecast for one or
                                         more GSA buildings
gsa_mar_show_weather_forecasted_data  — formatted text table of historical +
                                         NWS forecast data, suitable for wxO chat
"""
import sys
import os
import re
import math
import datetime
import requests
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from mcp.server.mcpserver import MCPServer

mcp = MCPServer(name="gsa-mar-weather-forecast", version="0.2.0")

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_NWS_HEADERS = {
    "User-Agent": "GSA-MAR-WeatherForecast/1.0",
    "Accept": "application/geo+json",
}
_NWS_BASE = "https://api.weather.gov"
_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# Supported weather parameters
# key → (open-meteo archive variable(s), display label, unit)
_PARAMS: dict[str, tuple[str, str, str]] = {
    "temperature":   ("temperature_2m_max,temperature_2m_min", "Temperature", "°C"),
    "precipitation": ("precipitation_sum",                      "Precipitation", "mm"),
    "humidity":      ("relative_humidity_2m_max",               "Humidity",    "%"),
    "wind":          ("windspeed_10m_max",                      "Wind Speed",  "km/h"),
}

# ---------------------------------------------------------------------------
# IOLP helpers — same dataset as James's gsa_jdp_get_gsa_locations
# ---------------------------------------------------------------------------

_GSA_FEATURE_URL = (
    "https://services1.arcgis.com/eBupDfPlEJK3mdAm/ArcGIS/rest/services/"
    "IOLP_NEW/FeatureServer/0/query"
)
_GSA_FIELDS = (
    "LOCATION_CODE,Real_Property_Asset_Name,STREET_ADDRESS,CITY,STATE_CD,"
    "ZIPCODE5,Latitude,Longitude,REGION_CODE"
)


def _query_iolp(where: str, max_records: int = 10) -> list[dict]:
    resp = requests.get(
        _GSA_FEATURE_URL,
        params={
            "where": where,
            "outFields": _GSA_FIELDS,
            "returnGeometry": "false",
            "resultRecordCount": max_records,
            "f": "json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"ArcGIS error: {data['error']}")
    return [f["attributes"] for f in data.get("features", [])]


def _find_buildings(query: str) -> list[dict]:
    """Search by partial/full name OR location code (e.g. 'DC0003').

    Compatible with James's IOLP building names and location codes.
    Tries exact LOCATION_CODE match first, then falls back to partial name search.
    """
    safe = query.strip().replace("'", "''")
    # If it looks like a location code (letters + digits, e.g. DC0003, TX0012)
    import re as _re
    if _re.match(r'^[A-Za-z]{2}\d+$', safe):
        hits = _query_iolp(
            f"UPPER(LOCATION_CODE) = UPPER('{safe}')",
            max_records=1,
        )
        if hits:
            return hits
    # Fall back to partial name match
    return _query_iolp(
        f"UPPER(Real_Property_Asset_Name) LIKE UPPER('%{safe}%')",
        max_records=5,
    )


def _resolve_buildings(building_names: str) -> tuple[list[dict], list[str]]:
    """Resolve comma-separated building name queries to IOLP records.

    Returns (found_records, not_found_names).
    Each record is a raw IOLP attribute dict with Latitude/Longitude.
    """
    records: list[dict] = []
    not_found: list[str] = []
    for query in [n.strip() for n in building_names.split(",") if n.strip()]:
        hits = _find_buildings(query)
        chosen = next(
            (h for h in hits if h.get("Latitude") and h.get("Longitude")),
            None,
        )
        if chosen:
            records.append(chosen)
        else:
            not_found.append(query)
    return records, not_found


# ---------------------------------------------------------------------------
# Open-Meteo historical helpers
# ---------------------------------------------------------------------------

def _fetch_historical(lat: float, lon: float, param_key: str, days: int) -> dict:
    end = datetime.date.today() - datetime.timedelta(days=1)
    start = end - datetime.timedelta(days=days - 1)
    var_str, _, _ = _PARAMS[param_key]
    resp = requests.get(
        _ARCHIVE_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "daily": var_str,
            "timezone": "auto",
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# NWS forecast helpers
# ---------------------------------------------------------------------------

def _nws_gridpoint(lat: float, lon: float) -> tuple[str, int, int]:
    url = f"{_NWS_BASE}/points/{round(lat, 4)},{round(lon, 4)}"
    resp = requests.get(url, headers=_NWS_HEADERS, timeout=15)
    if resp.status_code != 200:
        return ("", 0, 0)
    props = resp.json().get("properties", {})
    return (props.get("gridId", ""), props.get("gridX", 0), props.get("gridY", 0))


def _fetch_nws_forecast(office: str, gx: int, gy: int) -> list[dict]:
    url = f"{_NWS_BASE}/gridpoints/{office}/{gx},{gy}/forecast"
    resp = requests.get(url, headers=_NWS_HEADERS, timeout=15)
    if resp.status_code != 200:
        return []
    return resp.json().get("properties", {}).get("periods", [])


def _fetch_nws_griddata(office: str, gx: int, gy: int, quantity: str) -> list[dict]:
    url = f"{_NWS_BASE}/gridpoints/{office}/{gx},{gy}"
    resp = requests.get(url, headers=_NWS_HEADERS, timeout=20)
    if resp.status_code != 200:
        return []
    return resp.json().get("properties", {}).get(quantity, {}).get("values", [])


def _parse_iso_duration_hours(duration: str) -> float:
    m = re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?", duration)
    if not m:
        return 1.0
    return int(m.group(1) or 0) * 24 + int(m.group(2) or 0)


def _f_to_c(f: float) -> float:
    return round((f - 32) * 5 / 9, 1)


def _c_to_f(c: float) -> float:
    return round(c * 9 / 5 + 32, 1)


def _convert_temp(val: float | None, to_unit: str) -> float | None:
    """Convert a °C value to the requested unit ('C' or 'F')."""
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return val
    return _c_to_f(val) if to_unit.upper() == "F" else val


def _griddata_daily_avg(values: list[dict], forecast_days: int) -> list[tuple[str, float | None]]:
    """Collapse NWS hourly griddata into daily averages for the next N days."""
    bucket: dict[str, list[float]] = defaultdict(list)
    today = datetime.date.today()
    cutoff = today + datetime.timedelta(days=forecast_days)
    for entry in values:
        time_str, dur_str = entry["validTime"].split("/")
        dt = datetime.datetime.fromisoformat(time_str).date()
        if dt < today or dt >= cutoff:
            continue
        for _ in range(max(1, int(_parse_iso_duration_hours(dur_str)))):
            bucket[dt.isoformat()].append(entry["value"])
    result = []
    for i in range(forecast_days):
        d = (today + datetime.timedelta(days=i)).isoformat()
        vals = bucket.get(d, [])
        result.append((d, sum(vals) / len(vals) if vals else None))
    return result


def _nws_daily_forecast(
    param_key: str, periods: list[dict], griddata: list[dict], forecast_days: int
) -> list[dict]:
    """Return list of daily forecast dicts: {date, max, min, avg, description, pop}."""
    today = datetime.date.today()
    rows: list[dict] = []

    if param_key == "temperature":
        daily: dict[str, dict] = {}
        for p in periods:
            dt = datetime.datetime.fromisoformat(p["startTime"]).date()
            if dt < today or dt >= today + datetime.timedelta(days=forecast_days):
                continue
            temp_c = _f_to_c(p.get("temperature", 0))
            pop = (p.get("probabilityOfPrecipitation") or {}).get("value") or 0
            if p.get("isDaytime"):
                daily.setdefault(dt.isoformat(), {}).update(
                    {"max": temp_c, "desc": p.get("shortForecast", ""), "pop": pop}
                )
            else:
                daily.setdefault(dt.isoformat(), {})["min"] = temp_c
        for i in range(forecast_days):
            d = (today + datetime.timedelta(days=i)).isoformat()
            info = daily.get(d, {})
            mx = info.get("max")
            mn = info.get("min")
            avg = round((mx + mn) / 2, 1) if mx is not None and mn is not None else None
            rows.append({
                "date": d, "max": mx, "min": mn, "avg": avg,
                "desc": info.get("desc", ""), "pop": info.get("pop"),
            })

    elif param_key == "precipitation":
        pop_map: dict[str, float] = {}
        for p in periods:
            if not p.get("isDaytime"):
                continue
            dt = datetime.datetime.fromisoformat(p["startTime"]).date()
            pop = (p.get("probabilityOfPrecipitation") or {}).get("value") or 0
            pop_map[dt.isoformat()] = float(pop)
        for i in range(forecast_days):
            d = (today + datetime.timedelta(days=i)).isoformat()
            rows.append({"date": d, "avg": pop_map.get(d), "desc": "", "pop": pop_map.get(d)})

    else:
        # humidity / wind — from griddata
        for d, avg in _griddata_daily_avg(griddata, forecast_days):
            rows.append({"date": d, "avg": avg, "desc": ""})

    return rows


# ---------------------------------------------------------------------------
# Shared data-loading helper
# ---------------------------------------------------------------------------

def _load_all_buildings(
    building_names: str,
    param_key: str,
    history_days: int,
    forecast_days: int,
) -> tuple[list[dict], list[str]]:
    """Resolve buildings and fetch historical + forecast data concurrently.

    Returns (all_data, not_found).
    Each item in all_data: {rec, hist, fc_periods, fc_griddata, error}
    """
    records, not_found = _resolve_buildings(building_names)
    if not records:
        return [], not_found

    def _load(rec: dict) -> dict:
        lat, lon = float(rec["Latitude"]), float(rec["Longitude"])
        item: dict = {"rec": rec, "hist": None, "fc_periods": [], "fc_griddata": [], "error": ""}
        try:
            item["hist"] = _fetch_historical(lat, lon, param_key, history_days)
        except Exception as exc:
            item["error"] += f"Historical: {exc}. "
        try:
            office, gx, gy = _nws_gridpoint(lat, lon)
            if office:
                item["fc_periods"] = _fetch_nws_forecast(office, gx, gy)
                if param_key == "humidity":
                    item["fc_griddata"] = _fetch_nws_griddata(office, gx, gy, "relativeHumidity")
                elif param_key == "wind":
                    item["fc_griddata"] = _fetch_nws_griddata(office, gx, gy, "windSpeed")
            else:
                item["error"] += "NWS: outside US coverage. "
        except Exception as exc:
            item["error"] += f"Forecast: {exc}. "
        return item

    with ThreadPoolExecutor(max_workers=5) as ex:
        all_data = list(ex.map(_load, records))

    return all_data, not_found


# ---------------------------------------------------------------------------
# Tool 1: gsa_mar_show_weather_forecasted_data — text table
# ---------------------------------------------------------------------------

@mcp.tool(
    name="gsa_mar_show_weather_forecasted_data",
    description=(
        "Show historical and NWS-forecasted weather data for one or more GSA buildings "
        "as a formatted text table. Building names are matched against the same GSA IOLP "
        "dataset used by gsa_jdp_get_gsa_locations — partial names work (e.g. 'WHITTEN'). "
        "Supported parameters: temperature, precipitation, humidity, wind. "
        "For temperature, choose °C or °F via temperature_unit. "
        "Returns a text table with historical rows followed by forecast rows, "
        "one section per building. Suitable for display in wxO or any chat interface."
    ),
)
def gsa_mar_show_weather_forecasted_data(
    building_names: str,
    weather_parameter: str = "temperature",
    history_days: int = 5,
    forecast_days: int = 7,
    temperature_unit: str = "F",
) -> str:
    """Text table of historical + NWS forecast weather for GSA buildings.

    Args:
        building_names: Comma-separated GSA building names, partial names, or location
                        codes compatible with James's IOLP dataset (e.g. 'DC0003, WHITTEN').
        weather_parameter: One of: temperature, precipitation, humidity, wind. Default: temperature.
        history_days: Past days of historical data (1–14). Default: 5.
        forecast_days: NWS forecast days to show (1–7). Default: 7.
        temperature_unit: 'F' for Fahrenheit (default) or 'C' for Celsius.
                          Only applies when weather_parameter is temperature.

    Returns:
        Formatted text table — historical then forecast — one section per building.
    """
    param_key = weather_parameter.strip().lower()
    if param_key not in _PARAMS:
        return f"Unknown parameter '{weather_parameter}'. Choose from: {', '.join(_PARAMS)}."
    history_days = max(1, min(int(history_days), 14))
    forecast_days = max(1, min(int(forecast_days), 7))
    t_unit = temperature_unit.strip().upper() if temperature_unit.strip().upper() in ("C", "F") else "C"
    t_sym = "°F" if t_unit == "F" else "°C"

    _, label, unit = _PARAMS[param_key]
    # Override unit symbol for temperature display
    disp_unit = t_sym if param_key == "temperature" else unit

    all_data, not_found = _load_all_buildings(building_names, param_key, history_days, forecast_days)

    if not all_data:
        return "No GSA buildings found for: " + ", ".join(not_found or [building_names])

    lines: list[str] = [
        f"GSA Weather Data — {label} ({disp_unit})",
        f"Historical: past {history_days} day(s)  |  Forecast: next {forecast_days} day(s) via NWS",
        "=" * 72,
    ]

    col_w = 12  # date column width

    def _fmt_temp(val):
        v = _convert_temp(val, t_unit)
        return f"{v:.1f}{t_sym}" if v is not None else "N/A"

    for item in all_data:
        rec = item["rec"]
        name  = rec.get("Real_Property_Asset_Name", "Unknown")
        code  = rec.get("LOCATION_CODE", "")
        city  = rec.get("CITY", "")
        state = rec.get("STATE_CD", "")

        lines += ["", f"[{code}] {name}  —  {city}, {state}", "-" * 72]

        if item["error"]:
            lines.append(f"  ⚠  {item['error'].strip()}")

        # ── Historical ───────────────────────────────────────────────────────
        lines.append(f"  ── HISTORICAL (past {history_days} days, Open-Meteo) ──")
        if item["hist"]:
            daily = item["hist"].get("daily", {})
            dates = daily.get("time", [])
            if param_key == "temperature":
                maxv = daily.get("temperature_2m_max", [])
                minv = daily.get("temperature_2m_min", [])
                lines.append(f"  {'Date':<{col_w}}  {'Max':>9}  {'Min':>9}  {'Avg':>9}")
                lines.append(f"  {'-'*col_w}  {'---------':>9}  {'---------':>9}  {'---------':>9}")
                for d, mx, mn in zip(dates, maxv, minv):
                    avg = round((mx + mn) / 2, 1) if mx is not None and mn is not None else None
                    lines.append(
                        f"  {d:<{col_w}}  {_fmt_temp(mx):>9}  {_fmt_temp(mn):>9}  {_fmt_temp(avg):>9}"
                    )
            else:
                var_str, _, _ = _PARAMS[param_key]
                vals = daily.get(var_str, [])
                lines.append(f"  {'Date':<{col_w}}  {label:>12}")
                lines.append(f"  {'-'*col_w}  {'------------':>12}")
                for d, v in zip(dates, vals):
                    v_s = f"{v:.1f} {unit}" if v is not None else "N/A"
                    lines.append(f"  {d:<{col_w}}  {v_s:>12}")
        else:
            lines.append("  No historical data available.")

        # ── Forecast ─────────────────────────────────────────────────────────
        lines.append(f"  ── FORECAST (next {forecast_days} days, NWS) ──")
        fc_rows = _nws_daily_forecast(
            param_key, item["fc_periods"], item["fc_griddata"], forecast_days
        )
        if fc_rows:
            if param_key == "temperature":
                lines.append(f"  {'Date':<{col_w}}  {'Max':>9}  {'Min':>9}  {'Avg':>9}  Conditions")
                lines.append(f"  {'-'*col_w}  {'---------':>9}  {'---------':>9}  {'---------':>9}  ----------")
                for r in fc_rows:
                    desc = r.get("desc", "")[:30]
                    lines.append(
                        f"  {r['date']:<{col_w}}  {_fmt_temp(r.get('max')):>9}  "
                        f"{_fmt_temp(r.get('min')):>9}  {_fmt_temp(r.get('avg')):>9}  {desc}"
                    )
            elif param_key == "precipitation":
                lines.append(f"  {'Date':<{col_w}}  {'Precip Prob':>12}")
                lines.append(f"  {'-'*col_w}  {'------------':>12}")
                for r in fc_rows:
                    pop_s = f"{r['avg']:.0f}%" if r.get("avg") is not None else "N/A"
                    lines.append(f"  {r['date']:<{col_w}}  {pop_s:>12}")
            else:
                lines.append(f"  {'Date':<{col_w}}  {label:>12}")
                lines.append(f"  {'-'*col_w}  {'------------':>12}")
                for r in fc_rows:
                    v_s = f"{r['avg']:.1f} {unit}" if r.get("avg") is not None else "N/A"
                    lines.append(f"  {r['date']:<{col_w}}  {v_s:>12}")
        else:
            lines.append("  No NWS forecast available.")

    lines += ["", "=" * 72]
    if not_found:
        lines.append("Not found in IOLP: " + ", ".join(not_found))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 2: gsa_mar_plot_weather_forecasted_data — seaborn PNG chart
# ---------------------------------------------------------------------------

@mcp.tool(
    name="gsa_mar_plot_weather_forecasted_data",
    description=(
        "Plot historical and NWS-forecasted weather data for one or more GSA buildings "
        "as a seaborn line chart saved to a PNG file. Building names are matched against "
        "the same GSA IOLP dataset used by gsa_jdp_get_gsa_locations — partial names work. "
        "Historical data (solid lines) comes from Open-Meteo archive; "
        "NWS forecast (dashed lines) covers the next 1–7 days. "
        "For temperature, max/avg/min lines are drawn; a tomato-coloured average centerline "
        "runs through both sections. Supported parameters: temperature, precipitation, "
        "humidity, wind. Returns the saved PNG file path and a data summary."
    ),
)
def gsa_mar_plot_weather_forecasted_data(
    building_names: str,
    weather_parameter: str = "temperature",
    history_days: int = 7,
    forecast_days: int = 7,
    temperature_unit: str = "F",
    output_dir: str = "",
) -> str:
    """Seaborn line chart of historical + NWS forecast weather for GSA buildings.

    Args:
        building_names: Comma-separated GSA building names, partial names, or location
                        codes compatible with James's IOLP dataset (e.g. 'DC0003, WHITTEN').
        weather_parameter: One of: temperature, precipitation, humidity, wind. Default: temperature.
        history_days: Past days of historical data (3–30). Default: 7.
        forecast_days: NWS forecast days to plot (1–7). Default: 7.
        temperature_unit: 'F' for Fahrenheit (default) or 'C' for Celsius.
                          Only applies when weather_parameter is temperature.
        output_dir: Directory to save the PNG. Defaults to system temp directory.

    Returns:
        File path of the saved PNG chart and a data summary.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    import seaborn as sns

    param_key = weather_parameter.strip().lower()
    if param_key not in _PARAMS:
        return f"Unknown parameter '{weather_parameter}'. Choose from: {', '.join(_PARAMS)}."
    history_days = max(3, min(int(history_days), 30))
    forecast_days = max(1, min(int(forecast_days), 7))
    t_unit = temperature_unit.strip().upper() if temperature_unit.strip().upper() in ("C", "F") else "C"
    t_sym = "°F" if t_unit == "F" else "°C"

    _, label, unit = _PARAMS[param_key]
    disp_unit = t_sym if param_key == "temperature" else unit
    has_minmax = param_key == "temperature"

    def _ct(val):
        """Convert °C value for plotting."""
        if val is None or (isinstance(val, float) and math.isnan(val)):
            return float("nan")
        return _convert_temp(val, t_unit)

    all_data, not_found = _load_all_buildings(building_names, param_key, history_days, forecast_days)
    if not all_data:
        return "No GSA buildings found for: " + ", ".join(not_found or [building_names])

    sns.set_theme(style="whitegrid", font_scale=1.0)
    fig, ax = plt.subplots(figsize=(14, 5))
    palette = sns.color_palette("tab10", n_colors=max(len(all_data), 1))
    today = datetime.date.today()
    summary_lines: list[str] = []
    errors: list[str] = []

    for idx, item in enumerate(all_data):
        rec = item["rec"]
        colour = palette[idx]
        bname = rec.get("Real_Property_Asset_Name", "?")
        city  = rec.get("CITY", "")
        state = rec.get("STATE_CD", "")
        blabel = f"{bname} ({city}, {state})"

        if item["error"]:
            errors.append(f"{bname}: {item['error'].strip()}")

        # ── Historical ───────────────────────────────────────────────────────
        h_dates: list[datetime.date] = []
        h_max: list[float] = []
        h_min: list[float] = []
        h_avg: list[float] = []

        if item["hist"]:
            daily = item["hist"].get("daily", {})
            raw_dates = daily.get("time", [])
            h_dates = [datetime.date.fromisoformat(d) for d in raw_dates]

            if has_minmax:
                maxv = daily.get("temperature_2m_max", [])
                minv = daily.get("temperature_2m_min", [])
                h_max = [_ct(v) for v in maxv]
                h_min = [_ct(v) for v in minv]
                h_avg = [
                    (a + b) / 2 if not (math.isnan(a) or math.isnan(b)) else float("nan")
                    for a, b in zip(h_max, h_min)
                ]
                ax.plot(h_dates, h_max, marker="o", linewidth=2, color=colour,
                        label=f"{blabel} hist max")
                ax.plot(h_dates, h_min, marker="o", linewidth=1.5, linestyle=":",
                        color=colour, alpha=0.6, label=f"{blabel} hist min")
                ax.plot(h_dates, h_avg, linewidth=2.5, color="tomato",
                        label=f"{blabel} hist avg")
                vm = [v for v in h_max if not math.isnan(v)]
                vn = [v for v in h_min if not math.isnan(v)]
                va = [v for v in h_avg if not math.isnan(v)]
                if vm and vn:
                    summary_lines.append(
                        f"{bname} (hist): max {max(vm):.1f}{disp_unit}, "
                        f"avg {sum(va)/len(va):.1f}{disp_unit}, min {min(vn):.1f}{disp_unit}"
                    )
            else:
                var_str, _, _ = _PARAMS[param_key]
                vals = [v if v is not None else float("nan")
                        for v in daily.get(var_str, [])]
                ax.plot(h_dates, vals, marker="o", linewidth=2, color=colour,
                        label=f"{blabel} hist")
                vv = [v for v in vals if not math.isnan(v)]
                if vv:
                    summary_lines.append(
                        f"{bname} (hist): avg {sum(vv)/len(vv):.1f}{unit}, "
                        f"max {max(vv):.1f}{unit}, min {min(vv):.1f}{unit}"
                    )

        # ── Forecast ─────────────────────────────────────────────────────────
        fc_rows = _nws_daily_forecast(
            param_key, item["fc_periods"], item["fc_griddata"], forecast_days
        )
        if fc_rows:
            fc_dates = [datetime.date.fromisoformat(r["date"]) for r in fc_rows]

            if has_minmax:
                # Bridge: connect last historical point to first forecast point
                bridge_date = h_dates[-1] if h_dates else None
                bridge_max  = h_max[-1]  if h_max  else None
                bridge_min  = h_min[-1]  if h_min  else None
                bridge_avg  = h_avg[-1]  if h_avg  else None

                fc_max = [r.get("max") for r in fc_rows]
                fc_min = [r.get("min") for r in fc_rows]
                fc_avg = [r.get("avg") for r in fc_rows]

                def _prepend(dates, vals, bdate, bval):
                    if bdate and bval is not None:
                        return [bdate] + dates, [bval] + [_ct(v) for v in vals]
                    return dates, [_ct(v) for v in vals]

                pd_m, pv_m = _prepend(fc_dates, fc_max, bridge_date, bridge_max)
                pd_n, pv_n = _prepend(fc_dates, fc_min, bridge_date, bridge_min)
                pd_a, pv_a = _prepend(fc_dates, fc_avg, bridge_date, bridge_avg)

                ax.plot(pd_m, pv_m, marker="s", linewidth=2, linestyle="--",
                        color=colour, label=f"{blabel} fcst max")
                ax.plot(pd_n, pv_n, marker="s", linewidth=1.5, linestyle="-.",
                        color=colour, alpha=0.6, label=f"{blabel} fcst min")
                ax.plot(pd_a, pv_a, linewidth=2.5, linestyle="--", color="tomato",
                        label=f"{blabel} fcst avg")

                # Shaded forecast band
                valid_pairs = [
                    (d, mx, mn)
                    for d, mx, mn in zip(pd_m, pv_m, pv_n)
                    if not (math.isnan(mx) or math.isnan(mn))
                ]
                if valid_pairs:
                    vd, vmx, vmn = zip(*valid_pairs)
                    ax.fill_between(vd, vmn, vmx, color=colour, alpha=0.08)

                vm = [v for v in pv_m if not math.isnan(v)]
                vn = [v for v in pv_n if not math.isnan(v)]
                va = [v for v in pv_a if not math.isnan(v)]
                if vm and vn:
                    summary_lines.append(
                        f"{bname} (fcst): max {max(vm):.1f}{disp_unit}, "
                        f"avg {sum(va)/len(va):.1f}{disp_unit}, min {min(vn):.1f}{disp_unit}"
                    )
            else:
                fc_vals = [r.get("avg") for r in fc_rows]
                # Bridge
                if h_dates and h_avg:
                    fc_dates = [h_dates[-1]] + fc_dates
                    fc_vals  = [h_avg[-1]]  + fc_vals
                elif h_dates and item["hist"]:
                    var_str, _, _ = _PARAMS[param_key]
                    last_v = item["hist"].get("daily", {}).get(var_str, [None])[-1]
                    if last_v is not None:
                        fc_dates = [h_dates[-1]] + fc_dates
                        fc_vals  = [last_v] + fc_vals

                pv = [v if v is not None else float("nan") for v in fc_vals]
                ax.plot(fc_dates, pv, marker="s", linewidth=2, linestyle="--",
                        color=colour, label=f"{blabel} fcst")
                vv = [v for v in pv if not math.isnan(v)]
                if vv:
                    summary_lines.append(
                        f"{bname} (fcst): avg {sum(vv)/len(vv):.1f}{unit}"
                    )

    # Today line
    ax.axvline(x=today, color="gray", linestyle="--", linewidth=1, alpha=0.6)
    ax.text(today, ax.get_ylim()[1], " Today", fontsize=8, color="gray", va="top")

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.xaxis.set_major_locator(mdates.DayLocator())
    plt.xticks(rotation=45, ha="right")
    ax.set_xlabel("Date")
    ax.set_ylabel(f"{label} ({disp_unit})")

    # Title
    names_list = [d["rec"].get("Real_Property_Asset_Name", "?") for d in all_data]
    if len(names_list) == 1:
        title = f"GSA Building — {names_list[0]}: {label} Trend & Forecast"
    else:
        title = f"GSA Buildings ({len(names_list)}): {label} Trend & Forecast"
    ax.set_title(title)
    ax.legend(loc="best", fontsize=7, ncol=2)
    plt.tight_layout()

    # Save
    import tempfile
    save_dir = output_dir.strip() if output_dir.strip() else tempfile.gettempdir()
    os.makedirs(save_dir, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(save_dir, f"gsa_mar_{param_key}_{timestamp}.png")
    fig.savefig(filepath, dpi=150, bbox_inches="tight")
    plt.close(fig)

    result_lines = [
        f"Chart saved : {filepath}",
        f"Parameter   : {label} ({disp_unit})",
        f"Historical  : past {history_days} days (Open-Meteo)",
        f"Forecast    : next {forecast_days} days (NWS weather.gov)",
        f"Buildings   : {len(all_data)} plotted",
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
    print("Starting gsa-mar-weather-forecast on stdio...", file=sys.stderr)
    mcp.run(transport="stdio")
