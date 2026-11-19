#!/usr/bin/env python3
"""GSA Combined MCP Toolkit — all GSA tools in one stdio process.

Combines James's gsa_jdp_* tools with MAR gsa_mar_* tools.
James's server.py and toolkit.yaml are NOT modified.

Tools (James)
-------------
gsa_jdp_current_weather         — live weather via Open-Meteo
gsa_jdp_get_gsa_locations       — query GSA IOLP property list
gsa_jdp_get_gsa_location_detail — full detail for one GSA location
gsa_jdp_get_gsa_weather_alerts  — multi-source weather/hazard alerts

Tools (MAR)
-----------
gsa_mar_plot_weather_trend           — historical + forecast seaborn PNG chart
gsa_mar_show_weather_forecasted_data — text table (historical + NWS forecast)
gsa_mar_plot_weather_forecasted_data — seaborn PNG chart (historical + NWS forecast)
"""
import sys
import os
import re
import math
import datetime
import threading
import xml.etree.ElementTree as ET
import requests
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from mcp.server.mcpserver import MCPServer

mcp = MCPServer(name="gsa-combined-toolkit", version="1.0.0")

# ===========================================================================
# SHARED CONSTANTS
# ===========================================================================

_NWS_BASE = "https://api.weather.gov"
_NWS_HEADERS = {
    "User-Agent": "GSA-Combined-Toolkit/1.0",
    "Accept": "application/geo+json",
}
_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
_GSA_FEATURE_URL = (
    "https://services1.arcgis.com/eBupDfPlEJK3mdAm/ArcGIS/rest/services/"
    "IOLP_NEW/FeatureServer/0/query"
)

# ===========================================================================
# SHARED HELPERS — IOLP
# ===========================================================================

_GSA_FIELDS_FULL = (
    "LOCATION_CODE,Real_Property_Asset_Name,STREET_ADDRESS,CITY,STATE_CD,"
    "ZIPCODE5,Latitude,Longitude,BUILDING_STATUS,OWNED_OR_LEASED_INDICATOR,"
    "REGION_CODE,BUILDING_RSF,YEAR_BUILT"
)
_GSA_FIELDS_COORD = (
    "LOCATION_CODE,Real_Property_Asset_Name,STREET_ADDRESS,CITY,STATE_CD,"
    "ZIPCODE5,Latitude,Longitude,REGION_CODE"
)
_OWNED_OR_LEASED_MAP = {
    "owned": "F", "federal": "F", "f": "F",
    "leased": "L", "l": "L",
}
_PAGE_SIZE = 2000


def _query_gsa_features(where: str, max_records=None, fields: str = _GSA_FIELDS_FULL) -> list[dict]:
    results: list[dict] = []
    offset = 0
    page_size = _PAGE_SIZE if not max_records else min(_PAGE_SIZE, max_records)
    while True:
        resp = requests.get(
            _GSA_FEATURE_URL,
            params={
                "where": where,
                "outFields": fields,
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
        if len(features) < page_size:
            break
        offset += page_size
        if max_records and len(results) >= max_records:
            results = results[:max_records]
            break
    return results


def _find_buildings(query: str) -> list[dict]:
    """Search by partial/full name OR location code (e.g. 'DC0003')."""
    safe = query.strip().replace("'", "''")
    if re.match(r'^[A-Za-z]{2}\d+$', safe):
        hits = _query_gsa_features(
            f"UPPER(LOCATION_CODE) = UPPER('{safe}')",
            max_records=1,
            fields=_GSA_FIELDS_COORD,
        )
        if hits:
            return hits
    return _query_gsa_features(
        f"UPPER(Real_Property_Asset_Name) LIKE UPPER('%{safe}%')",
        max_records=5,
        fields=_GSA_FIELDS_COORD,
    )


def _resolve_buildings(building_names: str) -> tuple[list[dict], list[str]]:
    records: list[dict] = []
    not_found: list[str] = []
    for query in [n.strip() for n in building_names.split(",") if n.strip()]:
        hits = _find_buildings(query)
        chosen = next((h for h in hits if h.get("Latitude") and h.get("Longitude")), None)
        if chosen:
            records.append(chosen)
        else:
            not_found.append(query)
    return records, not_found

# ===========================================================================
# SHARED HELPERS — NWS
# ===========================================================================

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


def _convert_temp(val, to_unit: str):
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return val
    return _c_to_f(val) if to_unit.upper() == "F" else val


# ===========================================================================
# SHARED HELPERS — HAVERSINE / BOUNDS
# ===========================================================================

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def _in_bounds(lat: float, lon: float, bounds: tuple) -> bool:
    return bounds[0] <= lat <= bounds[1] and bounds[2] <= lon <= bounds[3]


_JAPAN_BOUNDS   = (24.0, 46.0, 122.0, 154.0)
_GERMANY_BOUNDS = (47.2, 55.2,   5.8,  15.1)
_ITALY_BOUNDS   = (35.5, 47.2,   6.6,  18.8)
_US_BOUNDS = [
    (24.0, 50.0, -125.0,  -66.0),
    (51.0, 72.0, -168.0, -130.0),
    (18.0, 23.5, -161.0, -154.0),
    (17.0, 19.0,  -68.0,  -64.0),
    (13.0, 21.0,  144.0,  146.5),
    (-15.0, -13.0, -171.5, -169.5),
]


def _in_us_territory(lat: float, lon: float) -> bool:
    return any(
        min_lat <= lat <= max_lat and min_lon <= lon <= max_lon
        for min_lat, max_lat, min_lon, max_lon in _US_BOUNDS
    )


# ===========================================================================
# TOOL 1 (James): gsa_jdp_current_weather
# ===========================================================================

@mcp.tool(
    name="gsa_jdp_current_weather",
    description=(
        "Fetch the current weather at a given latitude/longitude using the "
        "Open-Meteo public API. Returns temperature (°C), wind speed (km/h), "
        "and a WMO weather-condition code."
    ),
)
def gsa_jdp_current_weather(
    latitude: float,
    longitude: float,
    location_name: str = "the requested location",
) -> str:
    """Get current weather conditions for a geographic coordinate.

    Args:
        latitude: Latitude in decimal degrees (e.g. 38.9 for Washington D.C.).
        longitude: Longitude in decimal degrees (e.g. -77.0 for Washington D.C.).
        location_name: Optional human-readable label used in the returned summary.

    Returns:
        A plain-text summary of current weather conditions.
    """
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current_weather": True,
        "wind_speed_unit": "kmh",
    }
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    cw = data["current_weather"]
    temp = cw["temperature"]
    wind = cw["windspeed"]
    code = cw["weathercode"]
    return (
        f"Current weather at {location_name} "
        f"(lat={latitude}, lon={longitude}):\n"
        f"  Temperature : {temp} °C\n"
        f"  Wind speed  : {wind} km/h\n"
        f"  WMO code    : {code}"
    )


# ===========================================================================
# TOOL 2 (James): gsa_jdp_get_gsa_locations
# ===========================================================================

def _format_location(attrs: dict) -> str:
    ownership = "Owned" if attrs.get("OWNED_OR_LEASED_INDICATOR") == "F" else "Leased"
    rsf = attrs.get("BUILDING_RSF")
    rsf_str = f"{rsf:,.0f} RSF" if rsf else "N/A"
    lat = attrs.get("Latitude")
    lon = attrs.get("Longitude")
    coord_str = (
        f"{lat:.6f}, {lon:.6f}" if (lat is not None and lon is not None) else "N/A"
    )
    return (
        f"[{attrs.get('LOCATION_CODE', 'N/A')}] {attrs.get('Real_Property_Asset_Name', 'N/A')}\n"
        f"  Address : {attrs.get('STREET_ADDRESS', 'N/A')}, {attrs.get('CITY', 'N/A')}, "
        f"{attrs.get('STATE_CD', 'N/A')} {attrs.get('ZIPCODE5', '')}\n"
        f"  Coords  : {coord_str}\n"
        f"  Type    : {ownership}  |  Region: {attrs.get('REGION_CODE', 'N/A')}  "
        f"|  Built: {attrs.get('YEAR_BUILT', 'N/A')}  |  Size: {rsf_str}"
    )


@mcp.tool(
    name="gsa_jdp_get_gsa_locations",
    description=(
        "Query the GSA Inventory of Owned and Leased Properties (IOLP) to list "
        "federal government properties. Only buildings with a rentable square footage "
        "(BUILDING_RSF) greater than 50,000 are returned. Results can be further filtered "
        "by US state (two-letter abbreviation, e.g. 'TX'), ownership type "
        "('owned'/'leased'), and/or GSA region code (two-digit string, e.g. '05'). "
        "Pass no filters to retrieve all qualifying locations. "
        "Each result includes the location code, property name, full address, lat/lon "
        "coordinates, ownership type, region, year built, and building size in RSF. "
        "Coordinates are suitable for direct use with gsa_jdp_current_weather."
    ),
)
def gsa_jdp_get_gsa_locations(
    state: str = "",
    ownership: str = "",
    region: str = "",
    max_results: int = 200,
) -> str:
    """List GSA property locations with BUILDING_RSF > 50,000.

    Args:
        state: Two-letter US state abbreviation (e.g. 'CA', 'TX').
        ownership: 'owned' (federal/GSA-owned) or 'leased'. Leave blank for both.
        region: Two-digit GSA region code (e.g. '01' through '11').
        max_results: Maximum number of locations to return (default 200, max 5000).

    Returns:
        A formatted list of matching GSA locations with address and coordinate details.
    """
    clauses: list[str] = ["BUILDING_RSF > 50000"]
    if state.strip():
        clauses.append(f"STATE_CD = '{state.strip().upper()}'")
    if ownership.strip():
        key = ownership.strip().lower()
        indicator = _OWNED_OR_LEASED_MAP.get(key)
        if indicator is None:
            return f"Unrecognised ownership value '{ownership}'. Use 'owned' or 'leased'."
        clauses.append(f"OWNED_OR_LEASED_INDICATOR = '{indicator}'")
    if region.strip():
        clauses.append(f"REGION_CODE = '{region.strip().zfill(2)}'")

    where = " AND ".join(clauses)
    limit = max(1, min(int(max_results), 5000)) if max_results else 5000
    locations = _query_gsa_features(where, max_records=limit)

    if not locations:
        filters = []
        if state:   filters.append(f"state={state.upper()}")
        if ownership: filters.append(f"ownership={ownership}")
        if region:  filters.append(f"region={region}")
        return f"No GSA locations found matching: {', '.join(filters) or 'no filters'}."

    header_parts = []
    if state:     header_parts.append(f"state={state.upper()}")
    if ownership: header_parts.append(f"ownership={ownership}")
    if region:    header_parts.append(f"region={region}")
    filter_desc = " | ".join(header_parts) if header_parts else "all states/regions"

    lines = [
        f"GSA Properties ({filter_desc}) — {len(locations)} result(s)"
        + (f" [limited to {limit}]" if len(locations) == limit else ""),
        "=" * 70,
    ]
    for attrs in locations:
        lines.append(_format_location(attrs))
        lines.append("")
    return "\n".join(lines).rstrip()


# ===========================================================================
# TOOL 3 (James): gsa_jdp_get_gsa_location_detail
# ===========================================================================

@mcp.tool(
    name="gsa_jdp_get_gsa_location_detail",
    description=(
        "Retrieve the full detail record for a single GSA property by its "
        "LOCATION_CODE (e.g. 'DC0001'). Returns all available fields including "
        "congressional district, walk/transit scores, point of contact information, "
        "and asset type. Use gsa_jdp_get_gsa_locations first to discover location codes."
    ),
)
def gsa_jdp_get_gsa_location_detail(location_code: str) -> str:
    """Fetch all available details for a single GSA property.

    Args:
        location_code: The GSA LOCATION_CODE identifier (e.g. 'DC0001').

    Returns:
        A formatted detail record for the matching GSA property.
    """
    code = location_code.strip().upper()
    if not code:
        return "A location_code is required (e.g. 'DC0001')."

    resp = requests.get(
        _GSA_FEATURE_URL,
        params={"where": f"LOCATION_CODE = '{code}'", "outFields": "*",
                "returnGeometry": "false", "f": "json"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"ArcGIS API error: {data['error']}")
    features = data.get("features", [])
    if not features:
        return f"No GSA location found with LOCATION_CODE = '{code}'."

    attrs = features[0]["attributes"]
    ownership = "Owned (Federal)" if attrs.get("OWNED_OR_LEASED_INDICATOR") == "F" else "Leased"
    rsf = attrs.get("BUILDING_RSF")
    rsf_str = f"{rsf:,.0f} RSF" if rsf else "N/A"
    vacant_rsf = attrs.get("BLD_VACANT_RSF")
    vacant_str = f"{vacant_rsf:,.0f} RSF" if vacant_rsf else "N/A"
    lat = attrs.get("Latitude")
    lon = attrs.get("Longitude")

    def _v(val) -> str:
        return str(val) if val is not None else "N/A"

    lines = [
        f"GSA Property Detail: {code}",
        "=" * 60,
        f"  Name               : {_v(attrs.get('Real_Property_Asset_Name'))}",
        f"  Installation       : {_v(attrs.get('Installation_Name'))}",
        f"  Asset Type         : {_v(attrs.get('Real_Property_Asset_type'))}",
        f"  Status             : {_v(attrs.get('BUILDING_STATUS'))}",
        f"  Ownership          : {ownership}",
        "",
        f"  Address            : {_v(attrs.get('STREET_ADDRESS'))}",
        f"  City               : {_v(attrs.get('CITY'))}",
        f"  State              : {_v(attrs.get('STATE_CD'))}",
        f"  ZIP                : {_v(attrs.get('ZIPCODE5'))}",
        f"  Coordinates        : {lat:.6f}, {lon:.6f}" if (lat and lon) else "  Coordinates        : N/A",
        "",
        f"  GSA Region         : {_v(attrs.get('REGION_CODE'))}",
        f"  Congressional Dist : {_v(attrs.get('CONGRESSIONAL_DISTRICT_CODE'))}",
        f"  Year Built         : {_v(attrs.get('YEAR_BUILT'))}",
        f"  Building Size      : {rsf_str}",
        f"  Vacant RSF         : {vacant_str}",
        f"  Walk Score         : {_v(attrs.get('WALK_SCORE'))}",
        f"  Transit Score      : {_v(attrs.get('TRANSIT_SCORE'))}",
        "",
        f"  Senator 1          : {_v(attrs.get('SENATOR1'))}",
        f"  Senator 2          : {_v(attrs.get('SENATOR2'))}",
        f"  Representative     : {_v(attrs.get('REPRESENTATIVE'))}",
        "",
        f"  General POC        : {_v(attrs.get('General_POC'))} <{_v(attrs.get('General_POC_Email'))}>",
        f"  General POC 2      : {_v(attrs.get('General_POC_2'))} <{_v(attrs.get('General_POC_2_Email'))}>",
        f"  Federal POC        : {_v(attrs.get('Fed_POC'))} <{_v(attrs.get('Fed_POC_Email'))}>",
    ]
    return "\n".join(lines)


# ===========================================================================
# TOOL 4 (James): gsa_jdp_get_gsa_weather_alerts
# ---------------------------------------------------------------------------
# International alert helpers (JMA, DWD, MeteoAlarm Italy)
# ===========================================================================

_tlocal = threading.local()


def _nws_session() -> requests.Session:
    if not hasattr(_tlocal, "session"):
        s = requests.Session()
        s.headers.update(_NWS_HEADERS)
        _tlocal.session = s
    return _tlocal.session


def _fetch_all_active_alerts() -> list[dict]:
    url = f"{_NWS_BASE}/alerts/active"
    params = {"status": "actual", "message_type": "alert,update"}
    resp = requests.get(url, headers=_NWS_HEADERS, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("features", [])


_USGS_FEED = (
    "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson"
)
_QUAKE_RADIUS_KM = 150


def _fetch_usgs_earthquakes() -> list[dict]:
    try:
        resp = requests.get(_USGS_FEED, headers=_NWS_HEADERS, timeout=20)
        resp.raise_for_status()
        features = resp.json().get("features", [])
    except Exception:
        return []
    quakes = []
    for feat in features:
        coords = feat.get("geometry", {}).get("coordinates", [])
        if len(coords) < 2:
            continue
        lon, lat = coords[0], coords[1]
        depth = coords[2] if len(coords) > 2 else None
        if not _in_us_territory(lat, lon):
            continue
        props = feat["properties"]
        ts = props.get("time")
        time_str = (
            datetime.datetime.utcfromtimestamp(ts / 1000).strftime("%Y-%m-%dT%H:%M:%SZ")
            if ts else "Unknown"
        )
        quakes.append({
            "source": "USGS",
            "mag": props.get("mag"),
            "place": props.get("place", "Unknown location"),
            "lat": lat, "lon": lon,
            "depth_km": round(depth, 1) if depth is not None else None,
            "alert": props.get("alert"),
            "time_utc": time_str,
            "url": props.get("url", ""),
        })
    return quakes


_SWPC_ALERTS_URL = "https://services.swpc.noaa.gov/products/alerts.json"
_SWPC_POWER_CODES = {
    "WARK04", "WARK05", "WARK06", "WARK07", "WARK08", "WARK09",
    "ALTK04", "ALTK05", "ALTK06", "ALTK07", "ALTK08", "ALTK09",
    "WATA20", "WATA30", "WATA50", "WATA100", "WATA200",
    "ALTEF3", "ALTEF4", "ALTEF5", "ALTTP2", "ALTTP3", "ALTTP4", "ALTTP5",
    "ALTXMF", "ALTXM5", "SUMXM5", "SUM10R", "SUMSUD", "WARSUD",
}
_SWPC_K_SEVERITY = {
    4: "Minor (G1)", 5: "Moderate (G2)", 6: "Strong (G3) ⚡",
    7: "Severe (G4) ⚡⚡", 8: "Extreme (G5) ⚡⚡⚡", 9: "Extreme (G5) ⚡⚡⚡",
}


def _fetch_swpc_alerts() -> list[dict]:
    try:
        resp = requests.get(_SWPC_ALERTS_URL, headers=_NWS_HEADERS, timeout=15)
        resp.raise_for_status()
        raw = resp.json()
    except Exception:
        return []
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=48)
    results = []
    for item in raw:
        code = item.get("product_id", "")
        if code not in _SWPC_POWER_CODES:
            continue
        try:
            issued = datetime.datetime.strptime(
                item["issue_datetime"][:19], "%Y-%m-%d %H:%M:%S"
            )
        except (KeyError, ValueError):
            issued = None
        if issued and issued < cutoff:
            continue
        message = item.get("message", "")
        title = ""
        for line in message.splitlines():
            line = line.strip()
            if line and not line.startswith("Space Weather") and "Serial" not in line \
                    and "Issue Time" not in line:
                title = line
                break
        k_match = re.search(r"K(\d+)", code)
        k_val = int(k_match.group(1)) if k_match else None
        severity = _SWPC_K_SEVERITY.get(k_val, "Notable") if k_val else "Notable"
        results.append({
            "source": "NOAA SWPC", "code": code,
            "issue_datetime": item.get("issue_datetime", ""),
            "title": title, "message": message, "severity": severity, "k_index": k_val,
        })
    seen: dict[str, dict] = {}
    for alert in sorted(results, key=lambda a: a["issue_datetime"]):
        seen[alert["code"]] = alert
    return list(seen.values())


_NWS_SUPPRESSED_EVENTS = {
    "Hydrologic Outlook", "Hydrologic Statement",
    "Coastal Flood Watch", "Coastal Flood Warning", "Coastal Flood Advisory",
    "Coastal Flood Statement", "Lakeshore Flood Watch", "Lakeshore Flood Warning",
    "Lakeshore Flood Advisory", "Frost Advisory", "Freeze Watch", "Freeze Warning",
    "Hard Freeze Watch", "Hard Freeze Warning", "Dense Fog Advisory",
    "Freezing Fog Advisory", "Freezing Spray Advisory", "Heat Advisory", "Heat Watch",
    "Excessive Heat Watch", "Excessive Heat Warning", "Wind Chill Advisory",
    "Wind Chill Watch", "Wind Chill Warning", "Extreme Cold Watch",
    "Extreme Cold Warning", "Cold Weather Advisory", "Red Flag Warning",
    "Fire Weather Watch", "Gale Watch", "Gale Warning", "Small Craft Advisory",
    "Small Craft Advisory for Rough Bar", "Small Craft Advisory for Winds",
    "Small Craft Advisory for Hazardous Seas", "Hazardous Seas Warning",
    "Hazardous Seas Watch", "High Surf Advisory", "High Surf Warning",
    "Rip Current Statement", "Beach Hazards Statement", "Lake Wind Advisory",
    "Lakewind Advisory", "Ashfall Advisory", "Ashfall Warning",
}


def _build_zone_alert_map(alert_features: list[dict]) -> dict[str, list[dict]]:
    zone_map: dict[str, list[dict]] = {}
    for feat in alert_features:
        props = feat["properties"]
        if props.get("event", "") in _NWS_SUPPRESSED_EVENTS:
            continue
        for zone_url in props.get("affectedZones", []):
            zone_id = zone_url.rsplit("/", 1)[-1]
            zone_map.setdefault(zone_id, []).append(props)
    return zone_map


def _get_site_zones(lat: float, lon: float) -> tuple[str, str]:
    url = f"{_NWS_BASE}/points/{round(lat, 4)},{round(lon, 4)}"
    try:
        resp = _nws_session().get(url, timeout=15)
        if resp.status_code != 200:
            return ("", "")
        props = resp.json().get("properties", {})
        fz = props.get("forecastZone", "").rsplit("/", 1)[-1]
        cy = props.get("county", "").rsplit("/", 1)[-1]
        return (fz, cy)
    except Exception:
        return ("", "")


def _severity_rank(severity: str) -> int:
    return {"Extreme": 4, "Severe": 3, "Moderate": 2, "Minor": 1}.get(severity, 0)


def _quake_severity(mag: float) -> str:
    if mag >= 7.0: return "Extreme"
    if mag >= 6.0: return "Severe"
    if mag >= 5.0: return "Moderate"
    return "Minor"


def _format_alert_summary(alert_props: dict) -> str:
    event    = alert_props.get("event", "Unknown")
    severity = alert_props.get("severity", "Unknown")
    urgency  = alert_props.get("urgency", "Unknown")
    expires  = alert_props.get("expires", "")
    headline = alert_props.get("headline", "")
    description = alert_props.get("description", "") or ""
    if headline and len(headline) > 100:
        headline = headline[:97] + "..."
    desc_excerpt = ""
    if description:
        lines = [ln.strip() for ln in description.splitlines() if ln.strip()]
        parts = []
        char_count = 0
        for line in lines:
            if line in ("&&", "$$", "..."):
                continue
            clean = re.sub(r"^\*\s*[A-Z /]+\.\.\.", "", line).strip()
            if not clean:
                continue
            if char_count + len(clean) > 160:
                remaining = 160 - char_count
                if remaining > 20:
                    parts.append(clean[:remaining] + "...")
                break
            parts.append(clean)
            char_count += len(clean) + 1
            if len(parts) >= 2:
                break
        desc_excerpt = " ".join(parts)
    return (
        f"{event} [{severity}/{urgency}]"
        + (f" — expires {expires}" if expires else "")
        + (f"\n      {headline}" if headline else "")
        + (f"\n      {desc_excerpt}" if desc_excerpt else "")
    )


# --- JMA ---
_JMA_PREFECTURES = [
    (41.2, 45.6, 139.3, 145.9, "016000", "Hokkaido"), (41.2, 45.6, 141.0, 145.9, "016000", "Hokkaido"),
    (40.0, 41.6, 140.2, 141.7, "020000", "Aomori"), (38.7, 40.5, 140.6, 142.1, "030000", "Iwate"),
    (37.7, 39.0, 140.2, 141.7, "040000", "Miyagi"), (39.0, 40.5, 139.7, 141.0, "050000", "Akita"),
    (37.7, 39.0, 139.8, 140.9, "060000", "Yamagata"), (36.8, 37.9, 139.0, 141.1, "070000", "Fukushima"),
    (35.7, 36.9, 139.7, 140.9, "080000", "Ibaraki"), (36.2, 37.2, 139.3, 140.3, "090000", "Tochigi"),
    (36.1, 37.0, 138.4, 139.7, "100000", "Gunma"), (35.7, 36.3, 138.9, 139.9, "110000", "Saitama"),
    (34.9, 36.1, 139.7, 140.9, "120000", "Chiba"), (35.5, 35.9, 138.9, 139.9, "130000", "Tokyo"),
    (35.1, 35.7, 138.9, 139.8, "140000", "Kanagawa"), (36.7, 38.6, 137.6, 139.8, "150000", "Niigata"),
    (36.4, 37.0, 136.7, 137.8, "160000", "Toyama"), (36.1, 37.9, 136.2, 137.4, "170000", "Ishikawa"),
    (35.4, 36.3, 135.7, 136.9, "180000", "Fukui"), (35.3, 35.9, 138.3, 139.2, "190000", "Yamanashi"),
    (35.2, 37.0, 137.3, 138.6, "200000", "Nagano"), (34.6, 35.6, 137.5, 139.2, "220000", "Shizuoka"),
    (34.6, 35.4, 136.7, 137.9, "230000", "Aichi"), (33.8, 35.2, 135.8, 136.9, "240000", "Mie"),
    (34.7, 35.7, 135.7, 136.4, "250000", "Shiga"), (34.8, 35.9, 135.0, 136.0, "260000", "Kyoto"),
    (34.3, 35.0, 135.1, 135.8, "270000", "Osaka"), (34.3, 35.7, 134.3, 135.5, "280000", "Hyogo"),
    (33.9, 35.1, 135.6, 136.2, "290000", "Nara"), (33.4, 34.5, 135.0, 136.1, "300000", "Wakayama"),
    (35.0, 35.6, 133.2, 134.3, "310000", "Tottori"), (34.3, 35.5, 131.7, 133.4, "320000", "Shimane"),
    (34.5, 35.3, 133.2, 134.5, "330000", "Okayama"), (34.0, 35.0, 131.8, 133.4, "340000", "Hiroshima"),
    (33.8, 34.8, 130.7, 132.2, "350000", "Yamaguchi"), (33.7, 34.4, 133.8, 134.9, "360000", "Tokushima"),
    (34.0, 34.5, 133.4, 134.4, "370000", "Kagawa"), (33.0, 34.2, 132.2, 133.7, "380000", "Ehime"),
    (32.7, 34.0, 132.5, 134.3, "390000", "Kochi"), (33.4, 34.1, 130.0, 131.2, "400000", "Fukuoka"),
    (33.1, 33.7, 129.7, 130.7, "410000", "Saga"), (32.3, 33.9, 128.6, 130.5, "420000", "Nagasaki"),
    (32.1, 33.4, 130.0, 131.5, "430000", "Kumamoto"), (32.7, 33.7, 130.8, 132.1, "440000", "Oita"),
    (31.3, 33.0, 130.7, 131.9, "450000", "Miyazaki"), (31.0, 32.5, 129.3, 131.5, "460100", "Kagoshima"),
    (26.0, 27.0, 127.5, 128.8, "471000", "Okinawa (Main)"),
    (24.0, 26.0, 123.0, 125.6, "474000", "Okinawa (Yaeyama)"),
    (25.5, 26.5, 125.5, 127.0, "473000", "Okinawa (Miyako)"),
    (26.0, 27.5, 128.0, 130.0, "472000", "Okinawa (Amami-Oshima area)"),
]
_JMA_POWER_CODES = {
    "02": "Heavy Rain Warning", "03": "Heavy Snow Warning", "04": "Storm Warning",
    "05": "Strong Wind Warning", "06": "Storm Surge Warning", "07": "Tsunami Warning",
    "10": "Heavy Rain Advisory", "12": "Heavy Snow Advisory", "13": "Strong Wind Advisory",
    "14": "High Waves Advisory", "16": "Thunderstorm Advisory", "33": "High Waves Warning",
    "62": "Blizzard Warning", "70": "Special Heavy Rain Warning", "71": "Special Storm Warning",
    "72": "Special Heavy Snow Warning", "73": "Special Storm Surge Warning",
}
_JMA_BASE = "https://www.jma.go.jp/bosai/warning/data/warning"


def _jma_office_for_coord(lat: float, lon: float) -> tuple[str, str]:
    for min_lat, max_lat, min_lon, max_lon, code, name in _JMA_PREFECTURES:
        if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
            return code, name
    return "", ""


def _fetch_jma_warnings(office_code: str) -> list[dict]:
    url = f"{_JMA_BASE}/{office_code}.json"
    try:
        resp = requests.get(url, headers=_NWS_HEADERS, timeout=15)
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception:
        return []
    area_types = data.get("areaTypes", [])
    if not area_types:
        return []
    seen_codes: set[str] = set()
    results = []
    for area in area_types[0].get("areas", []):
        area_name = area.get("name", "")
        for warning in area.get("warnings", []):
            code = str(warning.get("code", ""))
            status = warning.get("status", "")
            if status not in ("発表", "継続"):
                continue
            if code not in _JMA_POWER_CODES:
                continue
            if code in seen_codes:
                continue
            seen_codes.add(code)
            event = _JMA_POWER_CODES[code]
            level = "warning" if int(code) in (2,3,4,5,6,7,33,62,70,71,72,73) else "advisory"
            results.append({"office_code": office_code, "code": code, "event": event,
                             "level": level, "area_name": area_name})
    return results


def _fetch_jma_warnings_for_sites(sites: list[dict]) -> dict[tuple[float, float], list[dict]]:
    office_to_coords: dict[str, list[tuple[float, float]]] = {}
    coord_to_office: dict[tuple[float, float], str] = {}
    for site in sites:
        lat, lon = site.get("Latitude"), site.get("Longitude")
        if lat is None or lon is None:
            continue
        if not _in_bounds(float(lat), float(lon), _JAPAN_BOUNDS):
            continue
        coord = (round(float(lat), 4), round(float(lon), 4))
        office, _ = _jma_office_for_coord(coord[0], coord[1])
        if not office:
            continue
        coord_to_office[coord] = office
        office_to_coords.setdefault(office, []).append(coord)
    if not office_to_coords:
        return {}
    office_warnings: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        future_map = {ex.submit(_fetch_jma_warnings, code): code for code in office_to_coords}
        for fut in future_map:
            code = future_map[fut]
            try:
                office_warnings[code] = fut.result()
            except Exception:
                office_warnings[code] = []
    result: dict[tuple[float, float], list[dict]] = {}
    for coord, office in coord_to_office.items():
        warnings = office_warnings.get(office, [])
        if warnings:
            result[coord] = warnings
    return result


# --- DWD ---
_DWD_WFS_URL = (
    "https://maps.dwd.de/geoserver/dwd/ows"
    "?service=WFS&version=2.0.0&request=GetFeature"
    "&typeName=dwd:Warnungen_Gemeinden&outputFormat=application/json"
)
_DWD_SEVERITY = {1: "Minor", 2: "Moderate", 3: "Severe", 4: "Extreme"}
_DWD_POWER_EVENTS = {
    "GEWITTER", "STURM", "ORKAN", "TORNADO", "HAGEL",
    "SCHNEEFALL", "SCHNEE", "GLATTEIS", "FROST", "BLITZEIS", "UNWETTER",
}


def _fetch_dwd_warnings_for_site(lat: float, lon: float, radius_deg: float = 0.3) -> list[dict]:
    bbox = f"{lon - radius_deg},{lat - radius_deg},{lon + radius_deg},{lat + radius_deg},EPSG:4326"
    try:
        resp = requests.get(_DWD_WFS_URL, params={"BBOX": bbox}, headers=_NWS_HEADERS, timeout=20)
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception:
        return []
    results = []
    seen: set[str] = set()
    for feat in data.get("features", []):
        props = feat.get("properties", {})
        event = (props.get("EVENT") or props.get("event") or "").upper()
        headline = props.get("HEADLINE") or props.get("headline") or ""
        level = props.get("LEVEL") or props.get("level") or 0
        desc = props.get("DESCRIPTION") or props.get("description") or ""
        region = props.get("REGIONNAME") or props.get("regionName") or ""
        if not any(kw in event for kw in _DWD_POWER_EVENTS):
            continue
        key = f"{event}|{level}|{region}"
        if key in seen:
            continue
        seen.add(key)
        try:
            level_int = int(level)
        except (ValueError, TypeError):
            level_int = 1
        results.append({
            "event": headline or event, "headline": headline, "level": level_int,
            "severity": _DWD_SEVERITY.get(level_int, "Minor"),
            "description": desc[:200] if desc else "", "region_name": region,
        })
    return results


def _fetch_dwd_warnings_for_sites(sites: list[dict]) -> dict[tuple[float, float], list[dict]]:
    germany_sites = []
    for site in sites:
        lat, lon = site.get("Latitude"), site.get("Longitude")
        if lat is None or lon is None:
            continue
        if _in_bounds(float(lat), float(lon), _GERMANY_BOUNDS):
            germany_sites.append((round(float(lat), 4), round(float(lon), 4)))
    if not germany_sites:
        return {}
    unique = list(dict.fromkeys(germany_sites))
    result: dict[tuple[float, float], list[dict]] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        future_map = {ex.submit(_fetch_dwd_warnings_for_site, lat, lon): (lat, lon) for lat, lon in unique}
        for fut in future_map:
            coord = future_map[fut]
            try:
                warnings = fut.result()
                if warnings:
                    result[coord] = warnings
            except Exception:
                pass
    return result


# --- MeteoAlarm Italy ---
_METEOALARM_ITALY_FEED = "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-italy"
_ITALY_REGIONS: dict[str, tuple[str, float, float]] = {
    "IT001": ("Calabria", 38.9, 16.6), "IT002": ("Basilicata", 40.5, 16.0),
    "IT003": ("Puglia", 41.1, 16.5), "IT004": ("Campania", 40.8, 15.0),
    "IT005": ("Molise", 41.7, 14.6), "IT006": ("Abruzzo", 42.3, 13.8),
    "IT007": ("Lazio", 41.9, 12.7), "IT008": ("Sardinia", 40.1, 9.1),
    "IT009": ("Sicily", 37.6, 14.0), "IT010": ("Umbria", 43.1, 12.4),
    "IT011": ("Marche", 43.6, 13.5), "IT012": ("Tuscany", 43.8, 11.2),
    "IT013": ("Emilia-Romagna", 44.5, 11.3), "IT014": ("Liguria", 44.3, 8.5),
    "IT015": ("Friuli-Venezia Giulia", 46.2, 13.2), "IT016": ("Veneto", 45.5, 11.9),
    "IT017": ("Trentino-Alto Adige", 46.5, 11.3), "IT018": ("Lombardy", 45.5, 9.7),
    "IT019": ("Valle d'Aosta", 45.7, 7.4), "IT020": ("Piedmont", 44.9, 7.9),
    "IT021": ("Calabria (South)", 38.3, 16.0),
}
_METEOALARM_POWER_TYPES = {
    "Wind", "Thunderstorms", "Snow/Ice", "Avalanches",
    "Extreme cold", "Fog", "Coastal Event", "Forest fire",
}
_CAP_SEVERITY_LEVEL = {"Minor": "1", "Moderate": "2", "Severe": "3", "Extreme": "4"}
_METEOALARM_EVENT_KEYWORD_MAP = {
    "thunderstorm": "Thunderstorms", "storm": "Thunderstorms", "wind": "Wind",
    "snow": "Snow/Ice", "ice": "Snow/Ice", "fog": "Fog", "rain": "Rain",
    "flood": "Flooding", "avalanche": "Avalanches", "cold": "Extreme cold",
    "coastal": "Coastal Event", "wave": "Coastal Event", "fire": "Forest fire",
}
_ATOM_NS = "http://www.w3.org/2005/Atom"
_CAP_NS  = "urn:oasis:names:tc:emergency:cap:1.2"


def _fetch_meteoalarm_italy() -> list[dict]:
    try:
        resp = requests.get(_METEOALARM_ITALY_FEED, headers=_NWS_HEADERS, timeout=20)
        if resp.status_code != 200:
            return []
        root = ET.fromstring(resp.content)
    except Exception:
        return []
    results = []
    for entry in root.findall(f"{{{_ATOM_NS}}}entry"):
        emma_id = ""
        geocode = entry.find(f"{{{_CAP_NS}}}geocode")
        if geocode is not None:
            vname = geocode.findtext(f"{{{_ATOM_NS}}}valueName", "")
            vval  = geocode.findtext(f"{{{_ATOM_NS}}}value", "")
            if vname == "EMMA_ID":
                emma_id = vval
        if not emma_id:
            for link_el in entry.findall(f"{{{_ATOM_NS}}}link"):
                m = re.search(r"EMMA_ID:(IT\d{3})", link_el.get("href", ""))
                if m:
                    emma_id = m.group(1)
                    break
        if not emma_id:
            eid = (entry.findtext(f"{{{_ATOM_NS}}}id", "") or "")
            m = re.search(r"/(IT\d{3})/", eid)
            if m:
                emma_id = m.group(1)
        if not emma_id:
            continue
        area_desc = entry.findtext(f"{{{_CAP_NS}}}areaDesc", "").strip()
        region_info = _ITALY_REGIONS.get(emma_id)
        region_name = area_desc if area_desc else (region_info[0] if region_info else emma_id)
        awareness_type = ""
        cap_event = entry.findtext(f"{{{_CAP_NS}}}event", "")
        if cap_event:
            ev_lower = cap_event.lower()
            awareness_type = next(
                (v for k, v in _METEOALARM_EVENT_KEYWORD_MAP.items() if k in ev_lower), ""
            )
        if not awareness_type:
            summary = (entry.findtext(f"{{{_ATOM_NS}}}summary", "") or "")
            title   = (entry.findtext(f"{{{_ATOM_NS}}}title",   "") or "")
            type_m = re.search(r"(?i)awareness[_ ]type[:\s]+([^;,\n]+)", summary or title)
            if type_m:
                awareness_type = type_m.group(1).strip()
        if not awareness_type or awareness_type not in _METEOALARM_POWER_TYPES:
            continue
        cap_sev = entry.findtext(f"{{{_CAP_NS}}}severity", "")
        if cap_sev:
            level = _CAP_SEVERITY_LEVEL.get(cap_sev, "1")
            severity_label = cap_sev
        else:
            summary = (entry.findtext(f"{{{_ATOM_NS}}}summary", "") or "")
            lm = re.search(r"(?i)awareness[_ ]level[:\s]+(\d)", summary)
            level = lm.group(1) if lm else "1"
            severity_label = {"1": "Minor", "2": "Moderate", "3": "Severe", "4": "Extreme"}.get(level, "Minor")
        expires  = entry.findtext(f"{{{_CAP_NS}}}expires", "")
        headline = entry.findtext(f"{{{_ATOM_NS}}}title", "").strip()
        results.append({
            "emma_id": emma_id, "region_name": region_name,
            "awareness_type": awareness_type, "level": level,
            "severity": severity_label, "headline": headline, "expires": expires,
        })
    return results


def _match_meteoalarm_italy(lat: float, lon: float, italy_alerts: list[dict], radius_km: float = 150.0) -> list[dict]:
    matches = []
    for alert in italy_alerts:
        region_info = _ITALY_REGIONS.get(alert["emma_id"])
        if not region_info:
            continue
        _, r_lat, r_lon = region_info
        if _haversine_km(lat, lon, r_lat, r_lon) <= radius_km:
            matches.append(alert)
    return matches


@mcp.tool(
    name="gsa_jdp_get_gsa_weather_alerts",
    description=(
        "Cross-reference active weather alerts and hazards from multiple authoritative "
        "sources against GSA property locations from the IOLP dataset. "
        "Sources: (1) U.S. National Weather Service (NWS) — weather alerts and watches "
        "for all US states and territories; (2) USGS Earthquake Hazards Program — M4.5+ "
        "earthquakes within 150 km of GSA sites in the past 24 hours; "
        "(3) NOAA Space Weather Prediction Center (SWPC) — geomagnetic storm watches "
        "and alerts affecting the entire US power grid and communications infrastructure; "
        "(4) Japan Meteorological Agency (JMA) — storm, strong wind, tsunami, and blizzard "
        "warnings for GSA sites in Japan, matched by prefecture; "
        "(5) Deutscher Wetterdienst (DWD) — thunderstorm, storm, and snow/ice warnings "
        "for GSA sites in Germany, matched by municipality bounding box; "
        "(6) MeteoAlarm Italy — wind, thunderstorm, snow/ice, and coastal event alerts "
        "for GSA sites in Italy, matched by region proximity. "
        "For every GSA site under an active alert, returns the site code, name, full "
        "address, and associated alert details with source tag. "
        "Optionally filter GSA sites by state, ownership type ('owned'/'leased'), or "
        "GSA region code."
    ),
)
def gsa_jdp_get_gsa_weather_alerts(
    state: str = "",
    ownership: str = "",
    region: str = "",
    max_sites: int = 500,
    severity_filter: str = "",
) -> str:
    """List GSA sites under active weather/hazard alerts from NWS, USGS, NOAA SWPC, JMA, DWD, MeteoAlarm.

    Args:
        state: Two-letter US state abbreviation to restrict GSA sites (e.g. 'TX').
        ownership: Filter GSA sites: 'owned' or 'leased'. Leave blank for both.
        region: Two-digit GSA region code (e.g. '07'). Leave blank for all regions.
        max_sites: Maximum number of GSA sites to check (default 500).
        severity_filter: Optional minimum severity: 'Extreme', 'Severe', 'Moderate', or 'Minor'.

    Returns:
        A consolidated hazard report for GSA sites.
    """
    nws_features: list[dict] = []
    nws_error: str = ""
    usgs_quakes: list[dict] = []
    swpc_alerts: list[dict] = []

    def _load_nws():
        nonlocal nws_features, nws_error
        try:
            nws_features = _fetch_all_active_alerts()
        except Exception as exc:
            nws_error = str(exc)

    with ThreadPoolExecutor(max_workers=3) as ex:
        f_nws = ex.submit(_load_nws)
        f_usgs = ex.submit(_fetch_usgs_earthquakes)
        f_swpc = ex.submit(_fetch_swpc_alerts)
        f_nws.result()
        usgs_quakes = f_usgs.result()
        swpc_alerts = f_swpc.result()

    zone_alert_map = _build_zone_alert_map(nws_features) if nws_features else {}

    clauses: list[str] = []
    if state.strip():
        clauses.append(f"STATE_CD = '{state.strip().upper()}'")
    if ownership.strip():
        key = ownership.strip().lower()
        indicator = _OWNED_OR_LEASED_MAP.get(key)
        if indicator is None:
            return f"Unrecognised ownership value '{ownership}'. Use 'owned' or 'leased'."
        clauses.append(f"OWNED_OR_LEASED_INDICATOR = '{indicator}'")
    if region.strip():
        clauses.append(f"REGION_CODE = '{region.strip().zfill(2)}'")
    where = " AND ".join(clauses) if clauses else "1=1"
    limit = max(1, int(max_sites)) if max_sites else None

    try:
        sites = _query_gsa_features(where, max_records=limit)
    except Exception as exc:
        return f"Failed to fetch GSA locations: {exc}"
    if not sites:
        return "No GSA sites found matching the specified filters."

    italy_alerts: list[dict] = []
    jma_alerts_map: dict[tuple[float, float], list[dict]] = {}
    dwd_alerts_map: dict[tuple[float, float], list[dict]] = {}

    has_japan = any(_in_bounds(float(s["Latitude"]), float(s["Longitude"]), _JAPAN_BOUNDS)
                    for s in sites if s.get("Latitude") is not None and s.get("Longitude") is not None)
    has_germany = any(_in_bounds(float(s["Latitude"]), float(s["Longitude"]), _GERMANY_BOUNDS)
                      for s in sites if s.get("Latitude") is not None and s.get("Longitude") is not None)
    has_italy = any(_in_bounds(float(s["Latitude"]), float(s["Longitude"]), _ITALY_BOUNDS)
                    for s in sites if s.get("Latitude") is not None and s.get("Longitude") is not None)

    intl_futures = {}
    with ThreadPoolExecutor(max_workers=3) as ex_intl:
        if has_japan:   intl_futures["jma"]   = ex_intl.submit(_fetch_jma_warnings_for_sites, sites)
        if has_germany: intl_futures["dwd"]   = ex_intl.submit(_fetch_dwd_warnings_for_sites, sites)
        if has_italy:   intl_futures["italy"] = ex_intl.submit(_fetch_meteoalarm_italy)
        if "jma"   in intl_futures:
            try: jma_alerts_map = intl_futures["jma"].result()
            except Exception: jma_alerts_map = {}
        if "dwd"   in intl_futures:
            try: dwd_alerts_map = intl_futures["dwd"].result()
            except Exception: dwd_alerts_map = {}
        if "italy" in intl_futures:
            try: italy_alerts = intl_futures["italy"].result()
            except Exception: italy_alerts = []

    coord_to_zones: dict[tuple[float, float], tuple[str, str]] = {}
    unique_coords: list[tuple[float, float]] = []
    for site in sites:
        lat, lon = site.get("Latitude"), site.get("Longitude")
        if lat is not None and lon is not None:
            key = (round(float(lat), 4), round(float(lon), 4))
            if key not in coord_to_zones:
                coord_to_zones[key] = ("", "")
                unique_coords.append(key)

    def _resolve(coord: tuple[float, float]) -> tuple[tuple[float, float], tuple[str, str]]:
        return coord, _get_site_zones(coord[0], coord[1])

    with ThreadPoolExecutor(max_workers=20) as executor:
        for coord, zones in executor.map(_resolve, unique_coords):
            coord_to_zones[coord] = zones

    min_rank = _severity_rank(severity_filter.strip().capitalize()) if severity_filter.strip() else 0
    hits: list[dict] = []

    for site in sites:
        lat, lon = site.get("Latitude"), site.get("Longitude")
        if lat is None or lon is None:
            continue
        slat, slon = float(lat), float(lon)
        coord = (round(slat, 4), round(slon, 4))
        fz, cy = coord_to_zones.get(coord, ("", ""))
        site_alerts: list[dict] = []
        seen_ids: set[str] = set()

        for zone_id in (fz, cy):
            if not zone_id:
                continue
            for alert_props in zone_alert_map.get(zone_id, []):
                aid = alert_props.get("id") or alert_props.get("@id", "")
                if aid in seen_ids:
                    continue
                seen_ids.add(aid)
                if _severity_rank(alert_props.get("severity", "")) >= min_rank:
                    site_alerts.append({"_source": "NWS", **alert_props})

        for quake in usgs_quakes:
            dist_km = _haversine_km(slat, slon, quake["lat"], quake["lon"])
            if dist_km <= _QUAKE_RADIUS_KM:
                qid = f"usgs-{quake['lat']}-{quake['lon']}-{quake['mag']}"
                if qid in seen_ids:
                    continue
                seen_ids.add(qid)
                severity = _quake_severity(quake["mag"])
                if _severity_rank(severity) >= min_rank:
                    site_alerts.append({
                        "_source": "USGS",
                        "event": f"Earthquake M{quake['mag']}",
                        "severity": severity, "urgency": "Immediate",
                        "headline": (
                            f"M{quake['mag']} earthquake — {quake['place']} "
                            f"({dist_km:.0f} km from site), depth {quake['depth_km']} km, "
                            f"PAGER alert: {quake['alert'] or 'pending'}"
                        ),
                        "expires": "",
                        **{k: v for k, v in quake.items() if k not in ("place",)},
                    })

        for jma_warn in jma_alerts_map.get(coord, []):
            jid = f"jma-{jma_warn.get('office_code','')}-{jma_warn['code']}"
            if jid in seen_ids:
                continue
            seen_ids.add(jid)
            severity = "Severe" if jma_warn["level"] == "warning" else "Moderate"
            area_label = f" — {jma_warn['area_name']}" if jma_warn.get("area_name") else ""
            if _severity_rank(severity) >= min_rank:
                site_alerts.append({
                    "_source": "JMA", "event": jma_warn["event"],
                    "severity": severity, "urgency": "Expected",
                    "headline": f"{jma_warn['event']}{area_label} (JMA {jma_warn['level']})",
                    "expires": "",
                })

        for dwd_warn in dwd_alerts_map.get(coord, []):
            did = f"dwd-{dwd_warn['event']}-{dwd_warn['region_name']}"
            if did in seen_ids:
                continue
            seen_ids.add(did)
            if _severity_rank(dwd_warn["severity"]) >= min_rank:
                site_alerts.append({
                    "_source": "DWD", "event": dwd_warn["event"],
                    "severity": dwd_warn["severity"], "urgency": "Expected",
                    "headline": (
                        f"{dwd_warn['headline'] or dwd_warn['event']} "
                        f"— {dwd_warn['region_name']} (Level {dwd_warn['level']})"
                        + (f": {dwd_warn['description']}" if dwd_warn.get("description") else "")
                    ),
                    "expires": "",
                })

        for italy_warn in _match_meteoalarm_italy(slat, slon, italy_alerts):
            iid = f"meteoalarm-{italy_warn['emma_id']}-{italy_warn['awareness_type']}"
            if iid in seen_ids:
                continue
            seen_ids.add(iid)
            if _severity_rank(italy_warn["severity"]) >= min_rank:
                site_alerts.append({
                    "_source": "MeteoAlarm", "event": italy_warn["awareness_type"],
                    "severity": italy_warn["severity"], "urgency": "Expected",
                    "headline": (
                        f"{italy_warn['headline'] or italy_warn['awareness_type']} "
                        f"— {italy_warn['region_name']} (Level {italy_warn['level']})"
                    ),
                    "expires": italy_warn.get("expires", ""),
                })

        if site_alerts:
            site_alerts.sort(key=lambda a: _severity_rank(a.get("severity", "")), reverse=True)
            hits.append({"site": site, "alerts": site_alerts})

    filter_parts = []
    if state:          filter_parts.append(f"state={state.upper()}")
    if ownership:      filter_parts.append(f"ownership={ownership}")
    if region:         filter_parts.append(f"region={region}")
    if severity_filter: filter_parts.append(f"min_severity={severity_filter}")
    filter_desc = " | ".join(filter_parts) if filter_parts else "all states/regions"

    swpc_lines: list[str] = []
    if swpc_alerts:
        swpc_lines.append("━" * 72)
        swpc_lines.append("⚡ NOAA SPACE WEATHER ALERTS — National Power Grid & Communications Risk")
        swpc_lines.append("━" * 72)
        for sa in sorted(swpc_alerts, key=lambda a: (a.get("k_index") or 0), reverse=True):
            swpc_lines.append(
                f"  [{sa['source']}] {sa['severity']} | Code: {sa['code']} "
                f"| Issued: {sa['issue_datetime'][:16]}"
            )
            if sa["title"]:
                swpc_lines.append(f"    {sa['title']}")
        swpc_lines.append(
            "  ⚠ Space weather events affect all GSA sites nationwide — "
            "check power systems and long-haul communications."
        )
        swpc_lines.append("")

    intl_parts = []
    if has_japan:   intl_parts.append(f"JMA ({sum(len(v) for v in jma_alerts_map.values())} JP warnings)")
    if has_germany: intl_parts.append(f"DWD ({sum(len(v) for v in dwd_alerts_map.values())} DE warnings)")
    if has_italy:   intl_parts.append(f"MeteoAlarm ({len(italy_alerts)} IT alerts)")
    intl_str = " | ".join(intl_parts)

    if not hits:
        no_hit_msg = (
            f"No active weather or hazard alerts found for any of the {len(sites)} "
            f"GSA sites checked ({filter_desc}).\n"
            f"(Checked {len(unique_coords)} unique coordinates against "
            f"{len(nws_features)} NWS alerts / {len(usgs_quakes)} US earthquakes"
            + (f" / {intl_str}" if intl_str else "") + ".)"
        )
        if swpc_lines:
            return "\n".join(swpc_lines) + "\n" + no_hit_msg
        return no_hit_msg

    hits.sort(
        key=lambda h: (
            -max(_severity_rank(a.get("severity", "")) for a in h["alerts"]),
            h["site"].get("STATE_CD", ""),
            h["site"].get("CITY", ""),
        )
    )

    lines: list[str] = []
    if swpc_lines:
        lines.extend(swpc_lines)

    source_line = (
        f"Sources: NWS ({len(nws_features)} alerts) | "
        f"USGS ({len(usgs_quakes)} US earthquakes M4.5+) | "
        f"NOAA SWPC ({len(swpc_alerts)} space weather alerts)"
        + (f" | {intl_str}" if intl_str else "")
    )
    lines += [
        f"GSA Sites Under Active Alerts ({filter_desc})",
        source_line,
        f"Checked {len(sites)} sites ({len(unique_coords)} unique coordinates)",
        "=" * 72, "",
    ]

    for hit in hits:
        s = hit["site"]
        ownership_label = "Owned" if s.get("OWNED_OR_LEASED_INDICATOR") == "F" else "Leased"
        lines.append(f"[{s.get('LOCATION_CODE', 'N/A')}] {s.get('Real_Property_Asset_Name', 'N/A')}")
        lines.append(
            f"  {s.get('STREET_ADDRESS', '')}, {s.get('CITY', '')}, "
            f"{s.get('STATE_CD', '')} {s.get('ZIPCODE5', '')} "
            f"({ownership_label}, Region {s.get('REGION_CODE', '?')})"
        )
        lines.append(f"  Coordinates: {s.get('Latitude', ''):.5f}, {s.get('Longitude', ''):.5f}")
        lines.append(f"  Active Alerts ({len(hit['alerts'])}):")
        for alert in hit["alerts"]:
            source_tag = f"[{alert.get('_source', 'NWS')}] "
            for i, alert_line in enumerate(_format_alert_summary(alert).split("\n")):
                prefix = f"    • {source_tag}" if i == 0 else "      "
                lines.append(f"{prefix}{alert_line}")
        lines.append("")

    lines.append(f"Total: {len(hits)} GSA site(s) affected out of {len(sites)} checked.")
    if nws_error:
        lines.append(f"⚠ NWS fetch error: {nws_error}")
    return "\n".join(lines)



# ===========================================================================
# MAR TOOLS — shared weather param config
# ===========================================================================

_MAR_PARAMS: dict[str, tuple[str, str, str]] = {
    "temperature":   ("temperature_2m_max,temperature_2m_min", "Temperature", "°C"),
    "precipitation": ("precipitation_sum",                      "Precipitation", "mm"),
    "humidity":      ("relative_humidity_2m_max",               "Humidity",    "%"),
    "wind":          ("windspeed_10m_max",                      "Wind Speed",  "km/h"),
}

_MAR_WEATHER_PARAMS: dict[str, tuple[str, str | None, str]] = {
    "temperature":   ("temperature_2m_max", "temperature_2m_min", "°C"),
    "temp":          ("temperature_2m_max", "temperature_2m_min", "°C"),
    "precipitation": ("precipitation_sum",  None,                  "mm"),
    "rainfall":      ("precipitation_sum",  None,                  "mm"),
    "rain":          ("precipitation_sum",  None,                  "mm"),
    "wind":          ("windspeed_10m_max",  None,                  "km/h"),
    "windspeed":     ("windspeed_10m_max",  None,                  "km/h"),
    "humidity":      ("relative_humidity_2m_max", None,            "%"),
}


def _fetch_historical(lat: float, lon: float, var_str: str, days: int) -> dict:
    end = datetime.date.today() - datetime.timedelta(days=1)
    start = end - datetime.timedelta(days=days - 1)
    resp = requests.get(
        _ARCHIVE_URL,
        params={
            "latitude": lat, "longitude": lon,
            "start_date": start.isoformat(), "end_date": end.isoformat(),
            "daily": var_str, "timezone": "auto",
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def _nws_daily_forecast(
    param_key: str, periods: list[dict], griddata: list[dict], forecast_days: int
) -> list[dict]:
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
            mx, mn = info.get("max"), info.get("min")
            avg = round((mx + mn) / 2, 1) if mx is not None and mn is not None else None
            rows.append({"date": d, "max": mx, "min": mn, "avg": avg,
                         "desc": info.get("desc", ""), "pop": info.get("pop")})
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
        bucket: dict[str, list[float]] = defaultdict(list)
        cutoff = today + datetime.timedelta(days=forecast_days)
        for entry in griddata:
            time_str, dur_str = entry["validTime"].split("/")
            dt = datetime.datetime.fromisoformat(time_str).date()
            if dt < today or dt >= cutoff:
                continue
            for _ in range(max(1, int(_parse_iso_duration_hours(dur_str)))):
                bucket[dt.isoformat()].append(entry["value"])
        for i in range(forecast_days):
            d = (today + datetime.timedelta(days=i)).isoformat()
            vals = bucket.get(d, [])
            rows.append({"date": d, "avg": sum(vals) / len(vals) if vals else None, "desc": ""})
    return rows


def _load_all_buildings(
    building_names: str, param_key: str, history_days: int, forecast_days: int
) -> tuple[list[dict], list[str]]:
    var_str, _, _ = _MAR_PARAMS[param_key]
    records, not_found = _resolve_buildings(building_names)
    if not records:
        return [], not_found

    def _load(rec: dict) -> dict:
        lat, lon = float(rec["Latitude"]), float(rec["Longitude"])
        item: dict = {"rec": rec, "hist": None, "fc_periods": [], "fc_griddata": [], "error": ""}
        try:
            item["hist"] = _fetch_historical(lat, lon, var_str, history_days)
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


# ===========================================================================
# TOOL 5 (MAR): gsa_mar_show_weather_forecasted_data
# ===========================================================================

@mcp.tool(
    name="gsa_mar_show_weather_forecasted_data",
    description=(
        "Show historical and NWS-forecasted weather data for one or more GSA buildings "
        "as a formatted text table. Building names are matched against the GSA IOLP "
        "dataset — partial names, full names, or location codes (e.g. 'DC0003') all work. "
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
        building_names: Comma-separated GSA building names, partial names, or location codes
                        (e.g. 'DC0003, WHITTEN').
        weather_parameter: One of: temperature, precipitation, humidity, wind. Default: temperature.
        history_days: Past days of historical data (1–14). Default: 5.
        forecast_days: NWS forecast days to show (1–7). Default: 7.
        temperature_unit: 'F' for Fahrenheit (default) or 'C' for Celsius.
                          Only applies when weather_parameter is temperature.

    Returns:
        Formatted text table — historical then forecast — one section per building.
    """
    param_key = weather_parameter.strip().lower()
    if param_key not in _MAR_PARAMS:
        return f"Unknown parameter '{weather_parameter}'. Choose from: {', '.join(_MAR_PARAMS)}."
    history_days  = max(1, min(int(history_days), 14))
    forecast_days = max(1, min(int(forecast_days), 7))
    t_unit = temperature_unit.strip().upper() if temperature_unit.strip().upper() in ("C", "F") else "F"
    t_sym  = "°F" if t_unit == "F" else "°C"
    _, label, unit = _MAR_PARAMS[param_key]
    disp_unit = t_sym if param_key == "temperature" else unit

    all_data, not_found = _load_all_buildings(building_names, param_key, history_days, forecast_days)
    if not all_data:
        return "No GSA buildings found for: " + ", ".join(not_found or [building_names])

    lines: list[str] = [
        f"GSA Weather Data — {label} ({disp_unit})",
        f"Historical: past {history_days} day(s)  |  Forecast: next {forecast_days} day(s) via NWS",
        "=" * 72,
    ]
    col_w = 12

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
                var_str, _, _ = _MAR_PARAMS[param_key]
                vals = daily.get(var_str.split(",")[0], [])
                lines.append(f"  {'Date':<{col_w}}  {label:>12}")
                lines.append(f"  {'-'*col_w}  {'------------':>12}")
                for d, v in zip(dates, vals):
                    v_s = f"{v:.1f} {unit}" if v is not None else "N/A"
                    lines.append(f"  {d:<{col_w}}  {v_s:>12}")
        else:
            lines.append("  No historical data available.")

        lines.append(f"  ── FORECAST (next {forecast_days} days, NWS) ──")
        fc_rows = _nws_daily_forecast(param_key, item["fc_periods"], item["fc_griddata"], forecast_days)
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


# ===========================================================================
# TOOL 6 (MAR): gsa_mar_plot_weather_forecasted_data
# ===========================================================================

@mcp.tool(
    name="gsa_mar_plot_weather_forecasted_data",
    description=(
        "Plot historical and NWS-forecasted weather data for one or more GSA buildings "
        "as a seaborn line chart saved to a PNG file. Building names are matched against "
        "the GSA IOLP dataset — partial names, full names, or location codes all work. "
        "Historical data (solid lines) comes from Open-Meteo archive; "
        "NWS forecast (dashed lines) covers the next 1–7 days. "
        "For temperature, max/avg/min lines are drawn with a tomato-coloured average "
        "centerline. Supported parameters: temperature, precipitation, humidity, wind. "
        "Returns the saved PNG file path and a data summary."
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
        building_names: Comma-separated GSA building names, partial names, or location codes
                        (e.g. 'DC0003, WHITTEN').
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
    if param_key not in _MAR_PARAMS:
        return f"Unknown parameter '{weather_parameter}'. Choose from: {', '.join(_MAR_PARAMS)}."
    history_days  = max(3, min(int(history_days), 30))
    forecast_days = max(1, min(int(forecast_days), 7))
    t_unit = temperature_unit.strip().upper() if temperature_unit.strip().upper() in ("C", "F") else "F"
    t_sym  = "°F" if t_unit == "F" else "°C"
    _, label, unit = _MAR_PARAMS[param_key]
    disp_unit = t_sym if param_key == "temperature" else unit
    has_minmax = param_key == "temperature"

    def _ct(val):
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
        bname  = rec.get("Real_Property_Asset_Name", "?")
        blabel = f"{bname} ({rec.get('CITY','')}, {rec.get('STATE_CD','')})"

        if item["error"]:
            errors.append(f"{bname}: {item['error'].strip()}")

        h_dates: list[datetime.date] = []
        h_max: list[float] = []
        h_min: list[float] = []
        h_avg: list[float] = []

        if item["hist"]:
            daily = item["hist"].get("daily", {})
            h_dates = [datetime.date.fromisoformat(d) for d in daily.get("time", [])]
            if has_minmax:
                h_max = [_ct(v) for v in daily.get("temperature_2m_max", [])]
                h_min = [_ct(v) for v in daily.get("temperature_2m_min", [])]
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
                var_str = _MAR_PARAMS[param_key][0].split(",")[0]
                vals = [v if v is not None else float("nan") for v in daily.get(var_str, [])]
                ax.plot(h_dates, vals, marker="o", linewidth=2, color=colour, label=f"{blabel} hist")
                vv = [v for v in vals if not math.isnan(v)]
                if vv:
                    summary_lines.append(
                        f"{bname} (hist): avg {sum(vv)/len(vv):.1f}{unit}, "
                        f"max {max(vv):.1f}{unit}, min {min(vv):.1f}{unit}"
                    )

        fc_rows = _nws_daily_forecast(param_key, item["fc_periods"], item["fc_griddata"], forecast_days)
        if fc_rows:
            fc_dates = [datetime.date.fromisoformat(r["date"]) for r in fc_rows]
            if has_minmax:
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

                valid_pairs = [
                    (d, mx, mn) for d, mx, mn in zip(pd_m, pv_m, pv_n)
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
                if h_dates and h_avg:
                    fc_dates = [h_dates[-1]] + fc_dates
                    fc_vals  = [h_avg[-1]]  + fc_vals
                elif h_dates and item["hist"]:
                    var_str = _MAR_PARAMS[param_key][0].split(",")[0]
                    last_v = item["hist"].get("daily", {}).get(var_str, [None])[-1]
                    if last_v is not None:
                        fc_dates = [h_dates[-1]] + fc_dates
                        fc_vals  = [last_v] + fc_vals
                pv = [v if v is not None else float("nan") for v in fc_vals]
                ax.plot(fc_dates, pv, marker="s", linewidth=2, linestyle="--",
                        color=colour, label=f"{blabel} fcst")
                vv = [v for v in pv if not math.isnan(v)]
                if vv:
                    summary_lines.append(f"{bname} (fcst): avg {sum(vv)/len(vv):.1f}{unit}")

    ax.axvline(x=today, color="gray", linestyle="--", linewidth=1, alpha=0.6)
    ax.text(today, ax.get_ylim()[1], " Today", fontsize=8, color="gray", va="top")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.xaxis.set_major_locator(mdates.DayLocator())
    plt.xticks(rotation=45, ha="right")
    ax.set_xlabel("Date")
    ax.set_ylabel(f"{label} ({disp_unit})")
    names_list = [d["rec"].get("Real_Property_Asset_Name", "?") for d in all_data]
    title = (
        f"GSA Building — {names_list[0]}: {label} Trend & Forecast"
        if len(names_list) == 1 else
        f"GSA Buildings ({len(names_list)}): {label} Trend & Forecast"
    )
    ax.set_title(title)
    ax.legend(loc="best", fontsize=7, ncol=2)
    plt.tight_layout()

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


# ===========================================================================
# TOOL 7 (MAR): gsa_mar_plot_weather_trend
# ===========================================================================

@mcp.tool(
    name="gsa_mar_plot_weather_trend",
    description=(
        "Fetch historical daily weather data (Open-Meteo archive) AND NWS forecast "
        "(next 1–7 days) for one or more GSA buildings and produce a seaborn "
        "line-chart PNG saved to disk. Historical data is shown as solid lines; "
        "forecast data as dashed lines with a shaded region between them. "
        "Buildings are looked up by partial name, full name, or location code "
        "in the GSA IOLP dataset. "
        "Supported weather parameters: temperature, precipitation (rainfall), "
        "wind (windspeed), humidity. "
        "Returns the full path of the saved PNG file and a data summary."
    ),
)
def gsa_mar_plot_weather_trend(
    building_names: str,
    weather_parameter: str = "temperature",
    history_days: int = 10,
    forecast_days: int = 7,
    output_dir: str = "",
) -> str:
    """Plot historical + NWS forecast weather trend for one or more GSA buildings.

    Args:
        building_names: Comma-separated GSA building names, partial names, or location codes
                        (e.g. 'WHITTEN, DC0003').
        weather_parameter: One of: temperature, precipitation, rainfall, wind, windspeed,
                           humidity. Default: temperature.
        history_days: Number of past days of historical data (3–30). Default: 10.
        forecast_days: Number of NWS forecast days to include (1–7). Default: 7.
        output_dir: Directory to save the PNG. Defaults to the system temp directory.

    Returns:
        Full file path of the saved PNG chart plus a data summary.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    import seaborn as sns

    param_key = weather_parameter.strip().lower()
    if param_key not in _MAR_WEATHER_PARAMS:
        valid = ", ".join(sorted(set(_MAR_WEATHER_PARAMS.keys())))
        return f"Unknown weather_parameter '{weather_parameter}'. Choose from: {valid}."

    var_main, var_secondary, unit = _MAR_WEATHER_PARAMS[param_key]
    history_days  = max(3, min(int(history_days), 30))
    forecast_days = max(1, min(int(forecast_days), 7))

    names = [n.strip() for n in building_names.split(",") if n.strip()]
    if not names:
        return "Please provide at least one building name in building_names."

    building_records: list[dict] = []
    not_found: list[str] = []
    for query in names:
        hits = _find_buildings(query)
        chosen = next((h for h in hits if h.get("Latitude") and h.get("Longitude")), None)
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

    daily_vars = var_main + ("," + var_secondary if var_secondary else "")

    def _load(rec: dict) -> dict:
        lat, lon = rec["lat"], rec["lon"]
        result: dict = {"rec": rec, "hist": None, "fc_periods": [], "fc_griddata": []}
        try:
            result["hist"] = _fetch_historical(lat, lon, daily_vars, history_days)
        except Exception:
            pass
        try:
            office, gx, gy = _nws_gridpoint(lat, lon)
            if office:
                result["fc_periods"] = _fetch_nws_forecast(office, gx, gy)
                if param_key == "humidity":
                    result["fc_griddata"] = _fetch_nws_griddata(office, gx, gy, "relativeHumidity")
                elif param_key in ("wind", "windspeed"):
                    result["fc_griddata"] = _fetch_nws_griddata(office, gx, gy, "windSpeed")
        except Exception:
            pass
        return result

    all_data: list[dict] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_load, rec): rec for rec in building_records}
        for fut in futures:
            try:
                all_data.append(fut.result())
            except Exception as exc:
                errors.append(f"{futures[fut]['name']}: {exc}")

    if not all_data:
        return "Failed to fetch weather data. Errors: " + "; ".join(errors)

    # NWS forecast series helper (inline, reuses _nws_daily_forecast for temp/precip/other)
    def _nws_forecast_series_trend(param_k, periods, griddata, fc_days):
        today = datetime.date.today()
        dates_main, vals_main, dates_sec, vals_sec = [], [], [], []
        if param_k in ("temperature", "temp"):
            daily: dict[str, dict] = {}
            for p in periods:
                dt = datetime.datetime.fromisoformat(p["startTime"]).date()
                if dt < today or dt >= today + datetime.timedelta(days=fc_days):
                    continue
                temp_c = _f_to_c(p.get("temperature", 0))
                if p.get("isDaytime"):
                    daily.setdefault(dt.isoformat(), {})["max"] = temp_c
                else:
                    daily.setdefault(dt.isoformat(), {})["min"] = temp_c
            for i in range(fc_days):
                d = today + datetime.timedelta(days=i)
                info = daily.get(d.isoformat(), {})
                dates_main.append(d); vals_main.append(info.get("max"))
                dates_sec.append(d);  vals_sec.append(info.get("min"))
        elif param_k in ("precipitation", "rainfall", "rain"):
            pop_map: dict[str, float] = {}
            for p in periods:
                if not p.get("isDaytime"):
                    continue
                dt = datetime.datetime.fromisoformat(p["startTime"]).date()
                pop = (p.get("probabilityOfPrecipitation") or {}).get("value") or 0
                pop_map[dt.isoformat()] = float(pop)
            for i in range(fc_days):
                d = today + datetime.timedelta(days=i)
                dates_main.append(d); vals_main.append(pop_map.get(d.isoformat()))
        else:
            bucket: dict[str, list[float]] = defaultdict(list)
            cutoff = today + datetime.timedelta(days=fc_days)
            for entry in griddata:
                time_str, dur_str = entry["validTime"].split("/")
                dt = datetime.datetime.fromisoformat(time_str).date()
                if dt < today or dt >= cutoff:
                    continue
                hrs = max(1, int(_parse_iso_duration_hours(dur_str)))
                for _ in range(hrs):
                    bucket[dt.isoformat()].append(entry["value"])
            for i in range(fc_days):
                d = today + datetime.timedelta(days=i)
                vals = bucket.get(d.isoformat(), [])
                dates_main.append(d); vals_main.append(sum(vals)/len(vals) if vals else None)
        return dates_main, vals_main, dates_sec, vals_sec

    sns.set_theme(style="whitegrid", font_scale=1.0)
    fig, ax = plt.subplots(figsize=(14, 5))
    palette = sns.color_palette("tab10", n_colors=max(len(all_data), 1))
    today = datetime.date.today()
    summary_lines: list[str] = []

    for idx, item in enumerate(all_data):
        rec = item["rec"]
        colour = palette[idx]
        label_base = f"{rec['name']} ({rec['city']}, {rec['state']})"

        if item["hist"]:
            daily = item["hist"].get("daily", {})
            hist_dates = [datetime.date.fromisoformat(d) for d in daily.get("time", [])]
            vals_m = [v if v is not None else float("nan") for v in daily.get(var_main, [])]
            if hist_dates and vals_m:
                ax.plot(hist_dates, vals_m, marker="o", linewidth=2, color=colour,
                        label=f"{label_base} hist{'  max' if var_secondary else ''}")
                if var_secondary:
                    vals_s = [v if v is not None else float("nan")
                              for v in daily.get(var_secondary, [])]
                    ax.plot(hist_dates, vals_s, marker="o", linewidth=1.5,
                            linestyle=":", color=colour, alpha=0.6,
                            label=f"{label_base} hist  min")
                    vals_avg = [
                        (a+b)/2 if not (math.isnan(a) or math.isnan(b)) else float("nan")
                        for a, b in zip(vals_m, vals_s)
                    ]
                    ax.plot(hist_dates, vals_avg, linewidth=2.5, linestyle="-",
                            color="tomato", label=f"{label_base} hist  avg")
                    valid_m = [v for v in vals_m if not math.isnan(v)]
                    valid_s = [v for v in vals_s if not math.isnan(v)]
                    valid_avg = [v for v in vals_avg if not math.isnan(v)]
                    if valid_m and valid_s:
                        summary_lines.append(
                            f"{rec['name']} (hist): max {max(valid_m):.1f}{unit}, "
                            f"avg {sum(valid_avg)/len(valid_avg):.1f}{unit}, min {min(valid_s):.1f}{unit}"
                        )
                else:
                    valid_m = [v for v in vals_m if not math.isnan(v)]
                    if valid_m:
                        summary_lines.append(
                            f"{rec['name']} (hist): avg {sum(valid_m)/len(valid_m):.1f}{unit}, "
                            f"max {max(valid_m):.1f}{unit}, min {min(valid_m):.1f}{unit}"
                        )

        if item["fc_periods"] or item["fc_griddata"]:
            fc_dates_m, fc_vals_m, fc_dates_s, fc_vals_s = _nws_forecast_series_trend(
                param_key, item["fc_periods"], item["fc_griddata"], forecast_days
            )
            if item["hist"]:
                h_daily = item["hist"].get("daily", {})
                h_dates = h_daily.get("time", [])
                h_main  = h_daily.get(var_main, [])
                if h_dates and h_main and h_main[-1] is not None:
                    bridge_d = datetime.date.fromisoformat(h_dates[-1])
                    fc_dates_m = [bridge_d] + list(fc_dates_m)
                    fc_vals_m  = [h_main[-1]] + list(fc_vals_m)
                    if var_secondary:
                        h_sec = h_daily.get(var_secondary, [])
                        if h_sec and h_sec[-1] is not None:
                            fc_dates_s = [bridge_d] + list(fc_dates_s)
                            fc_vals_s  = [h_sec[-1]]  + list(fc_vals_s)

            clean_m = [v if v is not None else float("nan") for v in fc_vals_m]
            if fc_dates_m and any(not math.isnan(v) for v in clean_m):
                ax.plot(fc_dates_m, clean_m, marker="s", linewidth=2, linestyle="--",
                        color=colour,
                        label=f"{label_base} fcst{'  max' if fc_vals_s else ''}")
                if fc_vals_s:
                    clean_s = [v if v is not None else float("nan") for v in fc_vals_s]
                    ax.plot(fc_dates_s, clean_s, marker="s", linewidth=1.5,
                            linestyle="-.", color=colour, alpha=0.6,
                            label=f"{label_base} fcst  min")
                    fc_avg = [
                        (a+b)/2 if not (math.isnan(a) or math.isnan(b)) else float("nan")
                        for a, b in zip(clean_m, clean_s)
                    ]
                    ax.plot(fc_dates_m, fc_avg, linewidth=2.5, linestyle="--",
                            color="tomato", label=f"{label_base} fcst  avg")
                    valid_pairs = [
                        (d, mx, mn) for d, mx, mn in zip(fc_dates_m, clean_m, clean_s)
                        if not math.isnan(mx) and not math.isnan(mn)
                    ]
                    if valid_pairs:
                        vd, vmx, vmn = zip(*valid_pairs)
                        ax.fill_between(vd, vmn, vmx, color=colour, alpha=0.08)
                    valid_m = [v for v in clean_m if not math.isnan(v)]
                    valid_s = [v for v in clean_s if not math.isnan(v)]
                    valid_avg = [v for v in fc_avg if not math.isnan(v)]
                    if valid_m and valid_s:
                        summary_lines.append(
                            f"{rec['name']} (fcst): max {max(valid_m):.1f}{unit}, "
                            f"avg {sum(valid_avg)/len(valid_avg):.1f}{unit}, min {min(valid_s):.1f}{unit}"
                        )
                else:
                    valid_m = [v for v in clean_m if not math.isnan(v)]
                    if valid_m:
                        summary_lines.append(
                            f"{rec['name']} (fcst): avg {sum(valid_m)/len(valid_m):.1f}{unit}"
                        )

    ax.axvline(x=today, color="gray", linestyle="--", linewidth=1, alpha=0.6)
    ax.text(today, ax.get_ylim()[1], " Today", fontsize=8, color="gray", va="top")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.xaxis.set_major_locator(mdates.DayLocator())
    plt.xticks(rotation=45, ha="right")
    ax.set_xlabel("Date")
    ax.set_ylabel(f"{weather_parameter.capitalize()} ({unit})")
    if len(all_data) == 1:
        bname = all_data[0]["rec"]["name"]
        title = f"GSA Building — {bname}: {weather_parameter.capitalize()} Trend & Forecast"
    else:
        title = f"GSA Buildings ({len(all_data)}): {weather_parameter.capitalize()} Trend & Forecast"
    ax.set_title(title)
    ax.legend(loc="best", fontsize=7, ncol=2)
    plt.tight_layout()

    import tempfile
    save_dir = output_dir.strip() if output_dir.strip() else tempfile.gettempdir()
    os.makedirs(save_dir, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(save_dir, f"gsa_weather_{param_key}_{timestamp}.png")
    fig.savefig(filepath, dpi=150, bbox_inches="tight")
    plt.close(fig)

    result_lines = [
        f"Chart saved: {filepath}",
        f"Parameter  : {weather_parameter} ({unit})",
        f"Historical : past {history_days} days (Open-Meteo)",
        f"Forecast   : next {forecast_days} days (NWS weather.gov)",
        f"Buildings  : {len(all_data)} plotted",
    ]
    if summary_lines:
        result_lines += [""] + summary_lines
    if not_found:
        result_lines += ["", "Not found in IOLP: " + ", ".join(not_found)]
    if errors:
        result_lines += ["", "Errors: " + "; ".join(errors)]
    return "\n".join(result_lines)


# ===========================================================================
# TOOLS 8 & 9 (MAR): comms outage probability
# ===========================================================================

# H — Infrastructure-Destructive Hazard Flag (spec-exact 5 types)
_COMMS_DESTRUCTIVE = {
    "Ice Storm Warning", "Hurricane Warning", "Tornado Warning",
    "High Wind Warning", "Flash Flood Warning",
}
# S mapping
_COMMS_SEV_MAP = {"Minor": 1, "Moderate": 2, "Severe": 3, "Extreme": 4}

# NWS alert cache (shared across both tools within the same process invocation)
_COMMS_ALERTS_CACHE: list[dict] = []
_COMMS_ALERTS_TS: datetime.datetime | None = None


def _comms_get_all_alerts() -> list[dict]:
    global _COMMS_ALERTS_CACHE, _COMMS_ALERTS_TS
    now = datetime.datetime.utcnow()
    if _COMMS_ALERTS_TS and (now - _COMMS_ALERTS_TS).total_seconds() < 120:
        return _COMMS_ALERTS_CACHE
    try:
        resp = requests.get(
            f"{_NWS_BASE}/alerts/active", headers=_NWS_HEADERS,
            params={"status": "actual", "message_type": "alert,update"}, timeout=30,
        )
        resp.raise_for_status()
        _COMMS_ALERTS_CACHE = resp.json().get("features", [])
        _COMMS_ALERTS_TS = now
    except Exception:
        _COMMS_ALERTS_CACHE = []
    return _COMMS_ALERTS_CACHE


def _comms_zone_index(features: list[dict]) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    for feat in features:
        props = feat.get("properties", {})
        for z in props.get("affectedZones", []):
            index.setdefault(z.rsplit("/", 1)[-1], []).append(props)
    return index


def _comms_resolve_zones(lat: float, lon: float) -> tuple[str, str]:
    url = f"{_NWS_BASE}/points/{round(lat,4)},{round(lon,4)}"
    try:
        resp = requests.get(url, headers=_NWS_HEADERS, timeout=15)
        if resp.status_code != 200:
            return "", ""
        p = resp.json().get("properties", {})
        return (p.get("forecastZone","").rsplit("/",1)[-1],
                p.get("county","").rsplit("/",1)[-1])
    except Exception:
        return "", ""


def _comms_alerts_for_site(lat: float, lon: float,
                            index: dict[str, list[dict]]) -> list[dict]:
    fz, cy = _comms_resolve_zones(lat, lon)
    seen, result = set(), []
    for zone in (fz, cy):
        if not zone:
            continue
        for props in index.get(zone, []):
            aid = props.get("id") or props.get("@id", "")
            if aid not in seen:
                seen.add(aid)
                result.append(props)
    return result


def _comms_prob(sev: str, dur_h: float, compound: bool, dest: bool) -> float:
    s = _COMMS_SEV_MAP.get(sev, 1)
    d = min(max(dur_h, 0), 12)
    z = -4.0 + 1.2*s + 0.25*d + 1.5*(1 if compound else 0) + 1.0*(1 if dest else 0)
    return round(1 / (1 + math.exp(-z)) * 100, 1)


def _comms_parse_dur(onset: str, expires: str) -> float:
    try:
        return max(0.0, round(
            (datetime.datetime.fromisoformat(expires) -
             datetime.datetime.fromisoformat(onset)).total_seconds() / 3600, 1))
    except Exception:
        return 6.0


def _comms_risk(prob: float) -> str:
    if prob >= 80: return "CRITICAL"
    if prob >= 60: return "HIGH"
    if prob >= 40: return "MODERATE"
    if prob >= 20: return "LOW"
    return "MINIMAL"


def _comms_score_site(alerts: list[dict]) -> tuple[float, str, list[dict]]:
    if not alerts:
        b = _comms_prob("Minor", 0, False, False)
        return b, _comms_risk(b), []
    compound = len(alerts) >= 2
    scored = []
    for a in alerts:
        ev  = a.get("event", "Unknown")
        sev = a.get("severity", "Minor")
        if sev not in _COMMS_SEV_MAP:
            sev = "Minor"
        onset   = a.get("onset","") or a.get("effective","")
        expires = a.get("expires","") or a.get("ends","")
        dur  = _comms_parse_dur(onset, expires) if onset and expires else 6.0
        dest = ev in _COMMS_DESTRUCTIVE
        prob = _comms_prob(sev, dur, compound, dest)
        scored.append({
            "event": ev, "severity": sev, "duration_hours": dur,
            "is_compound": compound, "is_destructive": dest,
            "probability": prob, "risk": _comms_risk(prob),
            "headline": (a.get("headline","") or "")[:80],
            "expires": (expires or "")[:16],
        })
    scored.sort(key=lambda x: x["probability"], reverse=True)
    return scored[0]["probability"], scored[0]["risk"], scored


_COMMS_MODEL_FOOTER = [
    "", "=" * 72,
    "Model: z = -4.0 + 1.2*S + 0.25*min(D,12) + 1.5*C + 1.0*H",
    "S=severity(1-4)  D=duration_hours  C=compound(≥2 alerts)  H=destructive_hazard",
    "prob = 1 / (1 + exp(-z))",
]


@mcp.tool(
    name="gsa_mar_comms_outage_scan",
    description=(
        "Scan all GSA sites in a given US state for communications outage risk. "
        "Fetches live NWS alerts for each site, scores each using the logistic model "
        "(S=severity, D=duration capped 12h, C=compound when ≥2 alerts overlap, "
        "H=destructive hazard: Ice Storm/Hurricane/Tornado/High Wind/Flash Flood). "
        "Returns sites above a probability threshold in descending order. "
        "Default: top 10 sites in Virginia (state='VA') at or above 50%. "
        "Use for: 'Which GSA sites in TX are at risk of comms outage?' or "
        "'List GSA buildings with communication failure probability above 60% in FL'."
    ),
)
def gsa_mar_comms_outage_scan(
    state: str = "VA",
    min_probability: float = 50.0,
    top_n: int = 10,
    max_sites: int = 200,
) -> str:
    """Scan GSA sites in a state and rank by communications outage probability.

    Args:
        state: Two-letter US state code (e.g. 'VA', 'TX', 'FL'). Default: 'VA'.
        min_probability: Only return sites at or above this % threshold (default 50.0).
                         Set to 0 to list all sites with any active alert.
        top_n: Max sites to return, sorted highest first (default 10).
        max_sites: Max GSA sites to fetch and check (default 200).

    Returns:
        Ranked list of GSA sites above the threshold, highest probability first.
    """
    try:
        safe = state.strip().upper().replace("'", "")
        sites = _query_gsa_features(f"STATE_CD = '{safe}'", max_records=max_sites)
    except Exception as exc:
        return f"Failed to fetch GSA sites for state='{state}': {exc}"
    if not sites:
        return f"No GSA sites found for state='{state}'."

    all_alerts = _comms_get_all_alerts()
    index = _comms_zone_index(all_alerts)

    def _process(rec: dict) -> dict | None:
        lat, lon = rec.get("Latitude"), rec.get("Longitude")
        if not lat or not lon:
            return None
        alerts = _comms_alerts_for_site(float(lat), float(lon), index)
        prob, risk, scored = _comms_score_site(alerts)
        return {"rec": rec, "probability": prob, "risk": risk,
                "alerts": scored, "alert_count": len(alerts)}

    results = [r for r in ThreadPoolExecutor(max_workers=20).map(_process, sites) if r]
    above = sorted([r for r in results if r["probability"] >= min_probability],
                   key=lambda x: x["probability"], reverse=True)
    top = above[:top_n]

    ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"GSA Comms Outage Scan — State: {state.upper()}",
        f"Threshold: ≥{min_probability}%  |  Top {top_n}  |  "
        f"Sites checked: {len(results)}  |  {ts} UTC",
        f"NWS active alerts in feed: {len(all_alerts)}",
        "=" * 72,
    ]
    if not top:
        lines += [
            "",
            f"  ✅  No GSA sites in {state.upper()} exceed {min_probability}% comms outage probability.",
            f"  ({len(results)} sites checked against {len(all_alerts)} active NWS alerts)",
        ]
    else:
        lines += ["", f"  {len(above)} site(s) ≥{min_probability}% — showing top {len(top)}:", ""]
        for i, r in enumerate(top, 1):
            rec = r["rec"]
            lines.append(f"  {'─'*68}")
            lines.append(f"  #{i}  [{r['risk']}]  {r['probability']}%  —  "
                         f"[{rec.get('LOCATION_CODE','')}] {rec.get('Real_Property_Asset_Name','?')}")
            lines.append(f"       {rec.get('STREET_ADDRESS','')}, {rec.get('CITY','')}, {state.upper()}")
            if r["alerts"]:
                ta = r["alerts"][0]
                lines.append(f"       Top alert : {ta['event']} [{ta['severity']}]"
                              + (f", expires {ta['expires']}" if ta["expires"] else ""))
                if r["alert_count"] > 1:
                    lines.append(f"       C=1 (compound): {r['alert_count']} overlapping alerts")
        lines.append(f"  {'─'*68}")
    lines += _COMMS_MODEL_FOOTER
    return "\n".join(lines)


@mcp.tool(
    name="gsa_mar_comms_outage_building",
    description=(
        "Score the communications outage probability for a single GSA building. "
        "Accepts partial name, full name, or location code (e.g. 'DC0003', 'WHITTEN'). "
        "Fetches live NWS alerts for the site and scores using the logistic model "
        "(S=severity, D=duration capped 12h, C=compound when ≥2 alerts overlap, "
        "H=destructive hazard: Ice Storm/Hurricane/Tornado/High Wind/Flash Flood). "
        "Returns a detailed per-alert breakdown with model inputs S/D/C/H. "
        "Use for: 'What is the comms outage risk for WHITTEN?' or "
        "'What is the communication failure probability for DC0003?'"
    ),
)
def gsa_mar_comms_outage_building(
    building_name: str,
    include_model_inputs: bool = True,
) -> str:
    """Score comms outage probability for a single GSA building.

    Args:
        building_name: GSA building partial name, full name, or location code
                       (e.g. 'WHITTEN', 'DC0003', '1400 Independence').
        include_model_inputs: Show S/D/C/H inputs per alert (default True).

    Returns:
        Detailed probability score with per-alert breakdown and model inputs.
    """
    hits = _find_buildings(building_name)
    rec = next((h for h in hits if h.get("Latitude") and h.get("Longitude")), None)
    if not rec:
        return (
            f"No GSA building found matching '{building_name}'. "
            "Try a partial name (e.g. 'WHITTEN') or location code (e.g. 'DC0003')."
        )

    name  = rec.get("Real_Property_Asset_Name", "Unknown")
    code  = rec.get("LOCATION_CODE", "")
    city  = rec.get("CITY", "")
    state = rec.get("STATE_CD", "")
    addr  = rec.get("STREET_ADDRESS", "")
    lat, lon = float(rec["Latitude"]), float(rec["Longitude"])

    fz, cy = _comms_resolve_zones(lat, lon)
    zones = [z for z in (fz, cy) if z]
    if not zones:
        return (f"[{code}] {name} — {city}, {state}\n"
                f"  ⚠  Could not resolve NWS zone (outside US NWS coverage).\n"
                f"  Communications outage probability: N/A")

    all_alerts = _comms_get_all_alerts()
    index = _comms_zone_index(all_alerts)
    alerts = _comms_alerts_for_site(lat, lon, index)
    top_prob, top_risk, scored = _comms_score_site(alerts)
    is_compound = len(alerts) >= 2

    ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"GSA Comms Outage Probability — {name}",
        f"[{code}]  {addr}, {city}, {state}",
        f"Coordinates: {lat:.5f}, {lon:.5f}  |  NWS zones: {', '.join(zones)}",
        f"Timestamp: {ts} UTC",
        "=" * 72,
    ]
    if not scored:
        baseline = _comms_prob("Minor", 0, False, False)
        lines += ["", "  ✅  No active NWS alerts for this site.",
                  f"  Communications outage probability: MINIMAL — {baseline}%",
                  "  (S=Minor/1, D=0h, C=No, H=No)"]
    else:
        lines += ["",
                  f"  SITE RISK: [{top_risk}]  {top_prob}% comms outage probability",
                  f"  Active alerts: {len(scored)}"
                  + ("  ← C=1 (compound: ≥2 overlapping)" if is_compound else ""),
                  ""]
        for i, s in enumerate(scored, 1):
            lines.append(f"  {'─'*68}")
            lines.append(f"  Alert #{i}  [{s['risk']}]  {s['probability']}%")
            lines.append(f"    Event    : {s['event']}")
            if s["headline"]:
                lines.append(f"    Headline : {s['headline']}")
            lines.append(f"    Expires  : {s['expires'] or 'N/A'}")
            if include_model_inputs:
                lines.append(
                    f"    Inputs   : S={s['severity']}({_COMMS_SEV_MAP.get(s['severity'],1)})  "
                    f"D={s['duration_hours']}h  "
                    f"C={'1 (compound)' if s['is_compound'] else '0'}  "
                    f"H={'1 (destructive)' if s['is_destructive'] else '0'}"
                )
        lines.append(f"  {'─'*68}")
    lines += _COMMS_MODEL_FOOTER
    return "\n".join(lines)


# ===========================================================================
# Entrypoint
# ===========================================================================

if __name__ == "__main__":
    print("Starting gsa-combined-toolkit on stdio...", file=sys.stderr)
    mcp.run(transport="stdio")

