#!/usr/bin/env python3
"""GSA JDP MCP Toolkit — tools served over stdio.

Tools
-----
gsa_jdp_current_weather         — live weather via Open-Meteo (no API key required)
gsa_jdp_get_gsa_locations       — query GSA Inventory of Owned & Leased Properties (IOLP)
gsa_jdp_get_gsa_location_detail — full detail for a single GSA location by LOCATION_CODE
gsa_jdp_get_gsa_weather_alerts  — cross-reference NWS/USGS/SWPC/JMA/DWD/MeteoAlarm alerts
                                   with GSA site locations (US + Japan + Germany + Italy)
"""
import sys
import math
import re
import datetime
import threading
import xml.etree.ElementTree as ET
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from mcp.server.mcpserver import MCPServer

mcp = MCPServer(name="gsa-jdp-toolkit", version="0.1.0")

# ---------------------------------------------------------------------------
# Tool 1: current weather
# ---------------------------------------------------------------------------

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
        latitude: Latitude of the location in decimal degrees (e.g. 38.9 for Washington D.C.).
        longitude: Longitude of the location in decimal degrees (e.g. -77.0 for Washington D.C.).
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


# ---------------------------------------------------------------------------
# Tool 2: GSA locations list
# ---------------------------------------------------------------------------

_GSA_FEATURE_URL = (
    "https://services1.arcgis.com/eBupDfPlEJK3mdAm/ArcGIS/rest/services/"
    "IOLP_NEW/FeatureServer/0/query"
)
_GSA_FIELDS = (
    "LOCATION_CODE,Real_Property_Asset_Name,STREET_ADDRESS,CITY,STATE_CD,"
    "ZIPCODE5,Latitude,Longitude,BUILDING_STATUS,OWNED_OR_LEASED_INDICATOR,"
    "REGION_CODE,BUILDING_RSF,YEAR_BUILT"
)
_OWNED_OR_LEASED_MAP = {
    "owned": "F",
    "federal": "F",
    "f": "F",
    "leased": "L",
    "l": "L",
}

# ArcGIS Feature Service returns at most 2 000 records per page; we page
# automatically so callers never have to think about pagination.
_PAGE_SIZE = 2000


def _query_gsa_features(where: str, max_records=None) -> list[dict]:
    """Page through the IOLP Feature Service and return attribute dicts."""
    results: list[dict] = []
    offset = 0
    page_size = _PAGE_SIZE if not max_records else min(_PAGE_SIZE, max_records)
    while True:
        params: dict = {
            "where": where,
            "outFields": _GSA_FIELDS,
            "returnGeometry": "false",
            "resultOffset": offset,
            "resultRecordCount": page_size,
            "f": "json",
        }
        resp = requests.get(_GSA_FEATURE_URL, params=params, timeout=30)
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
    """List GSA property locations with BUILDING_RSF > 50,000, optionally filtered by state, ownership, or region.

    Args:
        state: Two-letter US state abbreviation to filter by (e.g. 'CA', 'TX').
               Leave blank to include all states.
        ownership: Filter by ownership type. Accepts 'owned' (federal/GSA-owned) or
                   'leased'. Leave blank for both.
        region: Two-digit GSA region code to filter by (e.g. '01' through '11').
                Leave blank to include all regions.
        max_results: Maximum number of locations to return (default 200, max 5000).
                     Use a higher value or 0 to retrieve more records.

    Returns:
        A formatted list of matching GSA locations (BUILDING_RSF > 50,000) with
        address and coordinate details.
    """
    clauses: list[str] = ["BUILDING_RSF > 50000"]

    if state.strip():
        s = state.strip().upper()
        clauses.append(f"STATE_CD = '{s}'")

    if ownership.strip():
        key = ownership.strip().lower()
        indicator = _OWNED_OR_LEASED_MAP.get(key)
        if indicator is None:
            return (
                f"Unrecognised ownership value '{ownership}'. "
                "Use 'owned' (federal/GSA-owned) or 'leased'."
            )
        clauses.append(f"OWNED_OR_LEASED_INDICATOR = '{indicator}'")

    if region.strip():
        r = region.strip().zfill(2)
        clauses.append(f"REGION_CODE = '{r}'")

    where = " AND ".join(clauses) if clauses else "1=1"
    limit = max(1, min(int(max_results), 5000)) if max_results else 5000

    locations = _query_gsa_features(where, max_records=limit)

    if not locations:
        filters = []
        if state:
            filters.append(f"state={state.upper()}")
        if ownership:
            filters.append(f"ownership={ownership}")
        if region:
            filters.append(f"region={region}")
        filter_str = ", ".join(filters) if filters else "no filters"
        return f"No GSA locations found matching: {filter_str}."

    header_parts = []
    if state:
        header_parts.append(f"state={state.upper()}")
    if ownership:
        header_parts.append(f"ownership={ownership}")
    if region:
        header_parts.append(f"region={region}")
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


# ---------------------------------------------------------------------------
# Tool 5: GSA single location detail
# ---------------------------------------------------------------------------


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

    params = {
        "where": f"LOCATION_CODE = '{code}'",
        "outFields": "*",
        "returnGeometry": "false",
        "f": "json",
    }
    resp = requests.get(_GSA_FEATURE_URL, params=params, timeout=15)
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

    def _v(val: object) -> str:
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


# ---------------------------------------------------------------------------
# Tool 6: GSA sites under active weather alerts (NWS + USGS + NOAA SWPC)
# ---------------------------------------------------------------------------

_NWS_BASE = "https://api.weather.gov"
_NWS_HEADERS = {
    "User-Agent": "GSA AutoNOC Polaris Project",
    "Accept": "application/geo+json",
}

# Thread-local session for concurrent /points/ requests
_tlocal = threading.local()


def _nws_session() -> requests.Session:
    if not hasattr(_tlocal, "session"):
        s = requests.Session()
        s.headers.update(_NWS_HEADERS)
        _tlocal.session = s
    return _tlocal.session


def _fetch_all_active_alerts() -> list[dict]:
    """Return all active NWS alert features (GeoJSON features list)."""
    url = f"{_NWS_BASE}/alerts/active"
    params = {"status": "actual", "message_type": "alert,update"}
    resp = requests.get(url, headers=_NWS_HEADERS, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("features", [])


# ---------------------------------------------------------------------------
# USGS Earthquake helpers
# ---------------------------------------------------------------------------

_USGS_FEED = (
    "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson"
)
# US territory bounding boxes [min_lat, max_lat, min_lon, max_lon]
_US_BOUNDS = [
    (24.0, 50.0, -125.0, -66.0),    # Continental US
    (51.0, 72.0, -168.0, -130.0),   # Alaska
    (18.0, 23.5, -161.0, -154.0),   # Hawaii
    (17.0, 19.0, -68.0, -64.0),     # Puerto Rico / USVI
    (13.0, 21.0, 144.0, 146.5),     # Guam / Northern Marianas
    (-15.0, -13.0, -171.5, -169.5), # American Samoa
]

# Radius (km) within which a GSA site is considered "affected" by a quake
_QUAKE_RADIUS_KM = 150


def _in_us_territory(lat: float, lon: float) -> bool:
    return any(
        min_lat <= lat <= max_lat and min_lon <= lon <= max_lon
        for min_lat, max_lat, min_lon, max_lon in _US_BOUNDS
    )


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres between two lat/lon points."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def _fetch_usgs_earthquakes() -> list[dict]:
    """Return M4.5+ earthquakes in US territories from the past 24 hours.

    Each returned dict has keys: mag, place, lat, lon, depth_km, alert,
    time_utc, url — normalised for use in alert formatting.
    """
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
            "lat": lat,
            "lon": lon,
            "depth_km": round(depth, 1) if depth is not None else None,
            "alert": props.get("alert"),  # green/yellow/orange/red or None
            "time_utc": time_str,
            "url": props.get("url", ""),
        })
    return quakes


# ---------------------------------------------------------------------------
# NOAA SWPC (Space Weather) helpers
# ---------------------------------------------------------------------------

_SWPC_ALERTS_URL = "https://services.swpc.noaa.gov/products/alerts.json"

# Codes where geomagnetic storms or solar events threaten power grid / comms.
# WAR/ALT K## = Geomagnetic storm (K5=G1, K6=G2, K7=G3, K8=G4, K9=G5)
# WAT A## = Geomagnetic storm watch (A-index)
# SUM = Watch/Warning summary (may contain critical detail)
_SWPC_POWER_CODES = {
    # Geomagnetic storm watches & warnings (K-index based)
    "WARK04", "WARK05", "WARK06", "WARK07", "WARK08", "WARK09",
    "ALTK04", "ALTK05", "ALTK06", "ALTK07", "ALTK08", "ALTK09",
    # Geomagnetic storm watches (A-index)
    "WATA20", "WATA30", "WATA50", "WATA100", "WATA200",
    # Solar energetic particle events (radio blackout, satellite/GPS disruption)
    "ALTEF3", "ALTEF4", "ALTEF5",
    # Proton events (polar/radio disruption)
    "ALTTP2", "ALTTP3", "ALTTP4", "ALTTP5",
    # X-ray / radio blackout
    "ALTXMF", "ALTXM5", "SUMXM5",
    # Sudden commencement / storm summaries
    "SUM10R", "SUMSUD", "WARSUD",
}

# Minimum K-index severity worth including (4 = G1 = minor grid fluctuations)
_SWPC_MIN_K = 4

# Severity labels for output
_SWPC_K_SEVERITY = {
    4: "Minor (G1)",
    5: "Moderate (G2)",
    6: "Strong (G3) ⚡",
    7: "Severe (G4) ⚡⚡",
    8: "Extreme (G5) ⚡⚡⚡",
    9: "Extreme (G5) ⚡⚡⚡",
}


def _fetch_swpc_alerts() -> list[dict]:
    """Return power/connectivity-relevant NOAA SWPC space weather alerts issued
    within the past 48 hours.

    Each returned dict has keys: code, issue_datetime, title, message, severity.
    These are national (not site-specific) — affect the whole US power grid
    and long-haul communications infrastructure.
    """
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
        # Only include power/connectivity-relevant codes
        if code not in _SWPC_POWER_CODES:
            continue
        # Parse issue_datetime
        try:
            issued = datetime.datetime.strptime(
                item["issue_datetime"][:19], "%Y-%m-%d %H:%M:%S"
            )
        except (KeyError, ValueError):
            issued = None
        # Skip alerts older than 48h
        if issued and issued < cutoff:
            continue

        message = item.get("message", "")

        # Extract a short title from the message (first non-blank content line)
        title = ""
        for line in message.splitlines():
            line = line.strip()
            if line and not line.startswith("Space Weather") and "Serial" not in line \
                    and "Issue Time" not in line:
                title = line
                break

        # Determine severity from K-index in code (e.g. WARK06 -> K6)
        k_match = re.search(r"K(\d+)", code)
        k_val = int(k_match.group(1)) if k_match else None
        severity = _SWPC_K_SEVERITY.get(k_val, "Notable") if k_val else "Notable"

        results.append({
            "source": "NOAA SWPC",
            "code": code,
            "issue_datetime": item.get("issue_datetime", ""),
            "title": title,
            "message": message,
            "severity": severity,
            "k_index": k_val,
        })

    # De-duplicate: keep only the most recent alert per code type
    seen: dict[str, dict] = {}
    for alert in sorted(results, key=lambda a: a["issue_datetime"]):
        seen[alert["code"]] = alert
    return list(seen.values())


# NWS event types suppressed from results — not relevant to connectivity/power impact.
# Intentionally KEPT (not in this set):
#   "Flash Flood Warning"  — can directly block site access and damage infrastructure
#   "Dense Smoke Advisory" — visibility/air quality can affect operations
_NWS_SUPPRESSED_EVENTS = {
    # Flood / water (non-flash)
    "Hydrologic Outlook",
    "Hydrologic Statement",
    "Coastal Flood Watch",
    "Coastal Flood Warning",
    "Coastal Flood Advisory",
    "Coastal Flood Statement",
    "Lakeshore Flood Watch",
    "Lakeshore Flood Warning",
    "Lakeshore Flood Advisory",
    # Frost / freeze
    "Frost Advisory",
    "Freeze Watch",
    "Freeze Warning",
    "Hard Freeze Watch",
    "Hard Freeze Warning",
    # Visibility (dense fog; dense smoke kept in)
    "Dense Fog Advisory",
    "Freezing Fog Advisory",
    "Freezing Spray Advisory",
    # Heat / cold
    "Heat Advisory",
    "Heat Watch",
    "Excessive Heat Watch",
    "Excessive Heat Warning",
    "Wind Chill Advisory",
    "Wind Chill Watch",
    "Wind Chill Warning",
    "Extreme Cold Watch",
    "Extreme Cold Warning",
    "Cold Weather Advisory",
    # Red flag / fire weather
    "Red Flag Warning",
    "Fire Weather Watch",
    # Gale
    "Gale Watch",
    "Gale Warning",
    # Small craft
    "Small Craft Advisory",
    "Small Craft Advisory for Rough Bar",
    "Small Craft Advisory for Winds",
    "Small Craft Advisory for Hazardous Seas",
    # Hazardous seas / surf / rip current
    "Hazardous Seas Warning",
    "Hazardous Seas Watch",
    "High Surf Advisory",
    "High Surf Warning",
    "Rip Current Statement",
    "Beach Hazards Statement",
    "Lake Wind Advisory",
    "Lakewind Advisory",
    # Ashfall
    "Ashfall Advisory",
    "Ashfall Warning",
}


def _build_zone_alert_map(alert_features: list[dict]) -> dict[str, list[dict]]:
    """Build a mapping of zone_id -> list of alert property dicts.

    Events listed in _NWS_SUPPRESSED_EVENTS are dropped before indexing —
    they are not relevant to site connectivity or power impact.
    Flash Flood Warning and Dense Smoke Advisory are intentionally kept.
    """
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
    """Return (forecastZoneId, countyZoneId) for a coordinate via NWS /points/."""
    # Round to 4 decimal places — NWS /points/ rejects more precision sometimes
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
    """Higher rank = more severe (for sorting output)."""
    return {"Extreme": 4, "Severe": 3, "Moderate": 2, "Minor": 1}.get(severity, 0)


def _quake_severity(mag: float) -> str:
    """Map earthquake magnitude to a severity label consistent with NWS scale."""
    if mag >= 7.0:
        return "Extreme"
    if mag >= 6.0:
        return "Severe"
    if mag >= 5.0:
        return "Moderate"
    return "Minor"


def _format_alert_summary(alert_props: dict) -> str:
    """Return a concise summary of a NWS alert.

    Shows: event type + severity/urgency + expiry on the first line,
    the NWS headline on the second line, and the first meaningful
    excerpt from the description on a third line so that vague
    event names (e.g. 'Special Weather Statement') convey what the
    actual hazard is.
    """
    event = alert_props.get("event", "Unknown")
    severity = alert_props.get("severity", "Unknown")
    urgency = alert_props.get("urgency", "Unknown")
    expires = alert_props.get("expires", "")
    headline = alert_props.get("headline", "")
    description = alert_props.get("description", "") or ""

    # Truncate headline to avoid very long lines
    if headline and len(headline) > 100:
        headline = headline[:97] + "..."

    # Build a short description excerpt: take the first non-blank line(s)
    # of the description that add substance beyond the headline, cap at 160 chars.
    desc_excerpt = ""
    if description:
        # Collapse the structured "* WHAT...text" NWS bullet format into a
        # plain sentence, and grab plain prose openers for other formats.
        lines = [ln.strip() for ln in description.splitlines() if ln.strip()]
        parts = []
        char_count = 0
        for line in lines:
            # Skip lines that are pure section headers with no content
            if line in ("&&", "$$", "..."):
                continue
            # Strip leading "* LABEL..." bullet marker from structured alerts
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
            # One or two lines is enough
            if len(parts) >= 2:
                break
        desc_excerpt = " ".join(parts)

    out = (
        f"{event} [{severity}/{urgency}]"
        + (f" — expires {expires}" if expires else "")
        + (f"\n      {headline}" if headline else "")
        + (f"\n      {desc_excerpt}" if desc_excerpt else "")
    )
    return out


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
        "International sources are fetched automatically when the site list contains "
        "coordinates in Japan, Germany, or Italy. "
        "For every GSA site under an active alert, returns the site code, name, full "
        "address, and associated alert details with source tag. "
        "Optionally filter GSA sites by state, ownership type ('owned'/'leased'), or "
        "GSA region code. Alert data is retrieved live from all active sources. "
        "This is the primary tool for generating a weather and hazard early-warning "
        "list for GSA sites."
    ),
)
def gsa_jdp_get_gsa_weather_alerts(
    state: str = "",
    ownership: str = "",
    region: str = "",
    max_sites: int = 500,
    severity_filter: str = "",
) -> str:
    """List GSA sites under active weather/hazard alerts from NWS, USGS, NOAA SWPC,
    JMA (Japan), DWD (Germany), and MeteoAlarm (Italy).

    Args:
        state: Two-letter US state abbreviation to restrict GSA sites (e.g. 'TX').
               Leave blank to check sites across all states.
        ownership: Filter GSA sites by ownership type: 'owned' or 'leased'.
                   Leave blank for both.
        region: Two-digit GSA region code to restrict sites (e.g. '07').
                Leave blank for all regions.
        max_sites: Maximum number of GSA sites to check (default 500).
                   Higher values increase run time. Use 0 for all sites (may be slow).
        severity_filter: Optional minimum severity to include in results.
                         Accepts 'Extreme', 'Severe', 'Moderate', or 'Minor'.
                         Leave blank to include all severities.

    Returns:
        A consolidated hazard report for GSA sites, covering weather alerts,
        earthquakes, and space weather events.
    """
    # ── 1. Fetch US alert sources concurrently ──────────────────────────────
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

    # ── 2. Fetch GSA site list ───────────────────────────────────────────────
    clauses: list[str] = []
    if state.strip():
        clauses.append(f"STATE_CD = '{state.strip().upper()}'")
    if ownership.strip():
        key = ownership.strip().lower()
        indicator = _OWNED_OR_LEASED_MAP.get(key)
        if indicator is None:
            return (
                f"Unrecognised ownership value '{ownership}'. "
                "Use 'owned' or 'leased'."
            )
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

    # ── 2b. Fetch international alert sources (only if relevant sites exist) ─
    # These run concurrently with the NWS zone resolution below.
    italy_alerts: list[dict] = []
    jma_alerts_map: dict[tuple[float, float], list[dict]] = {}
    dwd_alerts_map: dict[tuple[float, float], list[dict]] = {}

    has_japan = any(
        _in_bounds(float(s["Latitude"]), float(s["Longitude"]), _JAPAN_BOUNDS)
        for s in sites if s.get("Latitude") is not None and s.get("Longitude") is not None
    )
    has_germany = any(
        _in_bounds(float(s["Latitude"]), float(s["Longitude"]), _GERMANY_BOUNDS)
        for s in sites if s.get("Latitude") is not None and s.get("Longitude") is not None
    )
    has_italy = any(
        _in_bounds(float(s["Latitude"]), float(s["Longitude"]), _ITALY_BOUNDS)
        for s in sites if s.get("Latitude") is not None and s.get("Longitude") is not None
    )

    intl_futures = {}
    with ThreadPoolExecutor(max_workers=3) as ex_intl:
        if has_japan:
            intl_futures["jma"] = ex_intl.submit(_fetch_jma_warnings_for_sites, sites)
        if has_germany:
            intl_futures["dwd"] = ex_intl.submit(_fetch_dwd_warnings_for_sites, sites)
        if has_italy:
            intl_futures["italy"] = ex_intl.submit(_fetch_meteoalarm_italy)

        if "jma" in intl_futures:
            try:
                jma_alerts_map = intl_futures["jma"].result()
            except Exception:
                jma_alerts_map = {}
        if "dwd" in intl_futures:
            try:
                dwd_alerts_map = intl_futures["dwd"].result()
            except Exception:
                dwd_alerts_map = {}
        if "italy" in intl_futures:
            try:
                italy_alerts = intl_futures["italy"].result()
            except Exception:
                italy_alerts = []

    # ── 3. Resolve zone IDs for each unique coordinate (concurrent) ──────────
    # Deduplicate coordinates to minimise NWS /points/ API calls
    coord_to_zones: dict[tuple[float, float], tuple[str, str]] = {}
    unique_coords: list[tuple[float, float]] = []
    for site in sites:
        lat = site.get("Latitude")
        lon = site.get("Longitude")
        if lat is not None and lon is not None:
            key = (round(float(lat), 4), round(float(lon), 4))
            if key not in coord_to_zones:
                coord_to_zones[key] = ("", "")
                unique_coords.append(key)

    # Fetch zones concurrently (up to 20 workers; NWS is rate-limited ~5 rps)
    def _resolve(coord: tuple[float, float]) -> tuple[tuple[float, float], tuple[str, str]]:
        return coord, _get_site_zones(coord[0], coord[1])

    with ThreadPoolExecutor(max_workers=20) as executor:
        for coord, zones in executor.map(_resolve, unique_coords):
            coord_to_zones[coord] = zones

    # ── 4. Match each site against NWS zones and USGS quake radii ───────────
    min_rank = _severity_rank(severity_filter.strip().capitalize()) if severity_filter.strip() else 0

    hits: list[dict] = []

    for site in sites:
        lat = site.get("Latitude")
        lon = site.get("Longitude")
        if lat is None or lon is None:
            continue
        slat, slon = float(lat), float(lon)
        coord = (round(slat, 4), round(slon, 4))
        fz, cy = coord_to_zones.get(coord, ("", ""))

        site_alerts: list[dict] = []
        seen_ids: set[str] = set()

        # ── NWS zone-based alerts ──
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

        # ── USGS earthquake proximity ──
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
                        "severity": severity,
                        "urgency": "Immediate",
                        "headline": (
                            f"M{quake['mag']} earthquake — {quake['place']} "
                            f"({dist_km:.0f} km from site), depth {quake['depth_km']} km, "
                            f"PAGER alert: {quake['alert'] or 'pending'}"
                        ),
                        "expires": "",
                        **{k: v for k, v in quake.items() if k not in ("place",)},
                    })

        # ── JMA (Japan) alerts ──
        for jma_warn in jma_alerts_map.get(coord, []):
            # area_name may be empty; use office_code + warning code as dedup key
            jid = f"jma-{jma_warn.get('office_code','')}-{jma_warn['code']}"
            if jid in seen_ids:
                continue
            seen_ids.add(jid)
            severity = "Severe" if jma_warn["level"] == "warning" else "Moderate"
            area_label = f" — {jma_warn['area_name']}" if jma_warn.get("area_name") else ""
            if _severity_rank(severity) >= min_rank:
                site_alerts.append({
                    "_source": "JMA",
                    "event": jma_warn["event"],
                    "severity": severity,
                    "urgency": "Expected",
                    "headline": (
                        f"{jma_warn['event']}{area_label} "
                        f"(JMA {jma_warn['level']})"
                    ),
                    "expires": "",
                })

        # ── DWD (Germany) alerts ──
        for dwd_warn in dwd_alerts_map.get(coord, []):
            did = f"dwd-{dwd_warn['event']}-{dwd_warn['region_name']}"
            if did in seen_ids:
                continue
            seen_ids.add(did)
            if _severity_rank(dwd_warn["severity"]) >= min_rank:
                site_alerts.append({
                    "_source": "DWD",
                    "event": dwd_warn["event"],
                    "severity": dwd_warn["severity"],
                    "urgency": "Expected",
                    "headline": (
                        f"{dwd_warn['headline'] or dwd_warn['event']} "
                        f"— {dwd_warn['region_name']} (Level {dwd_warn['level']})"
                        + (f": {dwd_warn['description']}" if dwd_warn.get("description") else "")
                    ),
                    "expires": "",
                })

        # ── MeteoAlarm Italy alerts ──
        for italy_warn in _match_meteoalarm_italy(slat, slon, italy_alerts):
            iid = f"meteoalarm-{italy_warn['emma_id']}-{italy_warn['awareness_type']}"
            if iid in seen_ids:
                continue
            seen_ids.add(iid)
            if _severity_rank(italy_warn["severity"]) >= min_rank:
                site_alerts.append({
                    "_source": "MeteoAlarm",
                    "event": italy_warn["awareness_type"],
                    "severity": italy_warn["severity"],
                    "urgency": "Expected",
                    "headline": (
                        f"{italy_warn['headline'] or italy_warn['awareness_type']} "
                        f"— {italy_warn['region_name']} (Level {italy_warn['level']})"
                    ),
                    "expires": italy_warn.get("expires", ""),
                })

        if site_alerts:
            site_alerts.sort(key=lambda a: _severity_rank(a.get("severity", "")), reverse=True)
            hits.append({"site": site, "alerts": site_alerts})

    # ── 5. Format output ─────────────────────────────────────────────────────
    filter_parts = []
    if state:
        filter_parts.append(f"state={state.upper()}")
    if ownership:
        filter_parts.append(f"ownership={ownership}")
    if region:
        filter_parts.append(f"region={region}")
    if severity_filter:
        filter_parts.append(f"min_severity={severity_filter}")
    filter_desc = " | ".join(filter_parts) if filter_parts else "all states/regions"

    # ── SWPC section: national (not site-specific), prepend if any alerts ──
    swpc_lines: list[str] = []
    if swpc_alerts:
        swpc_lines.append("━" * 72)
        swpc_lines.append(
            "⚡ NOAA SPACE WEATHER ALERTS — National Power Grid & Communications Risk"
        )
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

    # Build international source summary for header
    intl_parts = []
    if has_japan:
        intl_parts.append(f"JMA ({sum(len(v) for v in jma_alerts_map.values())} JP warnings)")
    if has_germany:
        intl_parts.append(f"DWD ({sum(len(v) for v in dwd_alerts_map.values())} DE warnings)")
    if has_italy:
        intl_parts.append(f"MeteoAlarm ({len(italy_alerts)} IT alerts)")
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

    # Sort hits by worst alert severity descending, then state+city
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
        "=" * 72,
        "",
    ]

    for hit in hits:
        s = hit["site"]
        ownership_label = "Owned" if s.get("OWNED_OR_LEASED_INDICATOR") == "F" else "Leased"
        lines.append(
            f"[{s.get('LOCATION_CODE', 'N/A')}] {s.get('Real_Property_Asset_Name', 'N/A')}"
        )
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

    lines.append(
        f"Total: {len(hits)} GSA site(s) affected out of {len(sites)} checked."
    )
    if nws_error:
        lines.append(f"⚠ NWS fetch error: {nws_error}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# International alert helpers — Japan (JMA), Germany (DWD), Italy (MeteoAlarm)
# ---------------------------------------------------------------------------

# Country bounding boxes [min_lat, max_lat, min_lon, max_lon]
_JAPAN_BOUNDS = (24.0, 46.0, 122.0, 154.0)
_GERMANY_BOUNDS = (47.2, 55.2, 5.8, 15.1)
_ITALY_BOUNDS = (35.5, 47.2, 6.6, 18.8)


def _in_bounds(lat: float, lon: float, bounds: tuple) -> bool:
    return bounds[0] <= lat <= bounds[1] and bounds[2] <= lon <= bounds[3]


# ---------------------------------------------------------------------------
# JMA (Japan Meteorological Agency) helpers
# ---------------------------------------------------------------------------

# Prefecture → JMA office code mapping.
# Each entry: (min_lat, max_lat, min_lon, max_lon, office_code, name)
# Ordered from north to south so first match wins for ambiguous borders.
_JMA_PREFECTURES = [
    # Hokkaido
    (41.2, 45.6, 139.3, 145.9, "016000", "Hokkaido (Soya/Abashiri)"),
    (41.2, 45.6, 141.0, 145.9, "016000", "Hokkaido"),
    # Aomori
    (40.0, 41.6, 140.2, 141.7, "020000", "Aomori"),
    # Iwate
    (38.7, 40.5, 140.6, 142.1, "030000", "Iwate"),
    # Miyagi
    (37.7, 39.0, 140.2, 141.7, "040000", "Miyagi"),
    # Akita
    (39.0, 40.5, 139.7, 141.0, "050000", "Akita"),
    # Yamagata
    (37.7, 39.0, 139.8, 140.9, "060000", "Yamagata"),
    # Fukushima
    (36.8, 37.9, 139.0, 141.1, "070000", "Fukushima"),
    # Ibaraki
    (35.7, 36.9, 139.7, 140.9, "080000", "Ibaraki"),
    # Tochigi
    (36.2, 37.2, 139.3, 140.3, "090000", "Tochigi"),
    # Gunma
    (36.1, 37.0, 138.4, 139.7, "100000", "Gunma"),
    # Saitama
    (35.7, 36.3, 138.9, 139.9, "110000", "Saitama"),
    # Chiba
    (34.9, 36.1, 139.7, 140.9, "120000", "Chiba"),
    # Tokyo (mainland)
    (35.5, 35.9, 138.9, 139.9, "130000", "Tokyo"),
    # Kanagawa
    (35.1, 35.7, 138.9, 139.8, "140000", "Kanagawa"),
    # Niigata
    (36.7, 38.6, 137.6, 139.8, "150000", "Niigata"),
    # Toyama
    (36.4, 37.0, 136.7, 137.8, "160000", "Toyama"),
    # Ishikawa
    (36.1, 37.9, 136.2, 137.4, "170000", "Ishikawa"),
    # Fukui
    (35.4, 36.3, 135.7, 136.9, "180000", "Fukui"),
    # Yamanashi
    (35.3, 35.9, 138.3, 139.2, "190000", "Yamanashi"),
    # Nagano
    (35.2, 37.0, 137.3, 138.6, "200000", "Nagano"),
    # Shizuoka
    (34.6, 35.6, 137.5, 139.2, "220000", "Shizuoka"),
    # Aichi
    (34.6, 35.4, 136.7, 137.9, "230000", "Aichi"),
    # Mie
    (33.8, 35.2, 135.8, 136.9, "240000", "Mie"),
    # Shiga
    (34.7, 35.7, 135.7, 136.4, "250000", "Shiga"),
    # Kyoto
    (34.8, 35.9, 135.0, 136.0, "260000", "Kyoto"),
    # Osaka
    (34.3, 35.0, 135.1, 135.8, "270000", "Osaka"),
    # Hyogo
    (34.3, 35.7, 134.3, 135.5, "280000", "Hyogo"),
    # Nara
    (33.9, 35.1, 135.6, 136.2, "290000", "Nara"),
    # Wakayama
    (33.4, 34.5, 135.0, 136.1, "300000", "Wakayama"),
    # Tottori
    (35.0, 35.6, 133.2, 134.3, "310000", "Tottori"),
    # Shimane
    (34.3, 35.5, 131.7, 133.4, "320000", "Shimane"),
    # Okayama
    (34.5, 35.3, 133.2, 134.5, "330000", "Okayama"),
    # Hiroshima
    (34.0, 35.0, 131.8, 133.4, "340000", "Hiroshima"),
    # Yamaguchi
    (33.8, 34.8, 130.7, 132.2, "350000", "Yamaguchi"),
    # Tokushima
    (33.7, 34.4, 133.8, 134.9, "360000", "Tokushima"),
    # Kagawa
    (34.0, 34.5, 133.4, 134.4, "370000", "Kagawa"),
    # Ehime
    (33.0, 34.2, 132.2, 133.7, "380000", "Ehime"),
    # Kochi
    (32.7, 34.0, 132.5, 134.3, "390000", "Kochi"),
    # Fukuoka
    (33.4, 34.1, 130.0, 131.2, "400000", "Fukuoka"),
    # Saga
    (33.1, 33.7, 129.7, 130.7, "410000", "Saga"),
    # Nagasaki
    (32.3, 33.9, 128.6, 130.5, "420000", "Nagasaki"),
    # Kumamoto
    (32.1, 33.4, 130.0, 131.5, "430000", "Kumamoto"),
    # Oita
    (32.7, 33.7, 130.8, 132.1, "440000", "Oita"),
    # Miyazaki
    (31.3, 33.0, 130.7, 131.9, "450000", "Miyazaki"),
    # Kagoshima (mainland — excludes Ryukyu chain)
    (31.0, 32.5, 129.3, 131.5, "460100", "Kagoshima"),
    # Okinawa — main island handled by 471000; outer islands have other codes
    (26.0, 27.0, 127.5, 128.8, "471000", "Okinawa (Main)"),
    (24.0, 26.0, 123.0, 125.6, "474000", "Okinawa (Yaeyama)"),
    (25.5, 26.5, 125.5, 127.0, "473000", "Okinawa (Miyako)"),
    (26.0, 27.5, 128.0, 130.0, "472000", "Okinawa (Amami-Oshima area)"),
]

# JMA warning codes that map to connectivity/power-relevant events.
# Warnings (higher severity): 02-07, 33, 62, 70-73
# Advisories (lower severity): 10, 12, 13, 14, 16
_JMA_POWER_CODES = {
    "02": "Heavy Rain Warning",
    "03": "Heavy Snow Warning",
    "04": "Storm Warning",
    "05": "Strong Wind Warning",
    "06": "Storm Surge Warning",
    "07": "Tsunami Warning",
    "10": "Heavy Rain Advisory",
    "12": "Heavy Snow Advisory",
    "13": "Strong Wind Advisory",
    "14": "High Waves Advisory",
    "16": "Thunderstorm Advisory",
    "33": "High Waves Warning",
    "62": "Blizzard Warning",
    "70": "Special Heavy Rain Warning",
    "71": "Special Storm Warning",
    "72": "Special Heavy Snow Warning",
    "73": "Special Storm Surge Warning",
}

_JMA_BASE = "https://www.jma.go.jp/bosai/warning/data/warning"


def _jma_office_for_coord(lat: float, lon: float) -> tuple[str, str]:
    """Return (office_code, prefecture_name) for a lat/lon in Japan, or ('','')."""
    for min_lat, max_lat, min_lon, max_lon, code, name in _JMA_PREFECTURES:
        if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
            return code, name
    return "", ""


def _fetch_jma_warnings(office_code: str) -> list[dict]:
    """Fetch active JMA warnings for a prefecture office code.

    Returns a list of dicts: {office_code, code, event, level, area_name}.
    level: 'warning' (higher) or 'advisory' (lower).
    Deduplicates by warning code — one entry per distinct active code per office.
    """
    url = f"{_JMA_BASE}/{office_code}.json"
    try:
        resp = requests.get(url, headers=_NWS_HEADERS, timeout=15)
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception:
        return []

    # areaTypes[0] = prefecture-level areas (fewer, broader)
    area_types = data.get("areaTypes", [])
    if not area_types:
        return []

    # Collect unique active warning codes across all areas in the prefecture
    seen_codes: set[str] = set()
    results = []
    for area in area_types[0].get("areas", []):
        area_name = area.get("name", "")
        for warning in area.get("warnings", []):
            code = str(warning.get("code", ""))
            status = warning.get("status", "")
            # status values: "発表" (issued), "継続" (continuing)
            if status not in ("発表", "継続"):
                continue
            if code not in _JMA_POWER_CODES:
                continue
            if code in seen_codes:
                continue
            seen_codes.add(code)
            event = _JMA_POWER_CODES[code]
            level = "warning" if int(code) in (
                2, 3, 4, 5, 6, 7, 33, 62, 70, 71, 72, 73
            ) else "advisory"
            results.append({
                "office_code": office_code,
                "code": code,
                "event": event,
                "level": level,
                "area_name": area_name,
            })
    return results


def _fetch_jma_warnings_for_sites(
    sites: list[dict],
) -> dict[tuple[float, float], list[dict]]:
    """Return a mapping of (lat, lon) -> list of JMA warning dicts for all Japan sites.

    Deduplicates office code fetches — one HTTP call per distinct prefecture.
    """
    # Group sites by office code
    office_to_coords: dict[str, list[tuple[float, float]]] = {}
    coord_to_office: dict[tuple[float, float], str] = {}
    for site in sites:
        lat = site.get("Latitude")
        lon = site.get("Longitude")
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

    # Fetch each unique office concurrently
    office_warnings: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        future_map = {ex.submit(_fetch_jma_warnings, code): code for code in office_to_coords}
        for fut in future_map:
            code = future_map[fut]
            try:
                office_warnings[code] = fut.result()
            except Exception:
                office_warnings[code] = []

    # Build result keyed by coordinate
    result: dict[tuple[float, float], list[dict]] = {}
    for coord, office in coord_to_office.items():
        warnings = office_warnings.get(office, [])
        if warnings:
            result[coord] = warnings
    return result


# ---------------------------------------------------------------------------
# DWD (Deutscher Wetterdienst — German Weather Service) helpers
# ---------------------------------------------------------------------------

_DWD_WFS_URL = (
    "https://maps.dwd.de/geoserver/dwd/ows"
    "?service=WFS&version=2.0.0&request=GetFeature"
    "&typeName=dwd:Warnungen_Gemeinden&outputFormat=application/json"
)

# DWD severity level → our severity label
_DWD_SEVERITY = {1: "Minor", 2: "Moderate", 3: "Severe", 4: "Extreme"}

# DWD event names that are connectivity/power relevant (substring match, German)
_DWD_POWER_EVENTS = {
    "GEWITTER", "STURM", "ORKAN", "TORNADO", "HAGEL",
    "SCHNEEFALL", "SCHNEE", "GLATTEIS", "FROST",
    "BLITZEIS", "UNWETTER",
}


def _fetch_dwd_warnings_for_site(lat: float, lon: float, radius_deg: float = 0.3) -> list[dict]:
    """Fetch DWD Warnungen_Gemeinden features within a bounding box around lat/lon.

    radius_deg ≈ 0.3° ≈ 30 km — matches municipality-level granularity.
    Returns list of dicts: {event, headline, level, severity, description, region_name}.
    """
    bbox = f"{lon - radius_deg},{lat - radius_deg},{lon + radius_deg},{lat + radius_deg},EPSG:4326"
    params = {"BBOX": bbox}
    try:
        resp = requests.get(_DWD_WFS_URL, params=params, headers=_NWS_HEADERS, timeout=20)
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

        # Only connectivity/power-relevant events
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
            "event": headline or event,
            "headline": headline,
            "level": level_int,
            "severity": _DWD_SEVERITY.get(level_int, "Minor"),
            "description": desc[:200] if desc else "",
            "region_name": region,
        })
    return results


def _fetch_dwd_warnings_for_sites(
    sites: list[dict],
) -> dict[tuple[float, float], list[dict]]:
    """Return a mapping of (lat, lon) -> list of DWD warning dicts for German sites."""
    germany_sites = []
    for site in sites:
        lat = site.get("Latitude")
        lon = site.get("Longitude")
        if lat is None or lon is None:
            continue
        if _in_bounds(float(lat), float(lon), _GERMANY_BOUNDS):
            germany_sites.append((round(float(lat), 4), round(float(lon), 4)))

    if not germany_sites:
        return {}

    # Deduplicate coords
    unique = list(dict.fromkeys(germany_sites))

    result: dict[tuple[float, float], list[dict]] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        future_map = {ex.submit(_fetch_dwd_warnings_for_site, lat, lon): (lat, lon)
                      for lat, lon in unique}
        for fut in future_map:
            coord = future_map[fut]
            try:
                warnings = fut.result()
                if warnings:
                    result[coord] = warnings
            except Exception:
                pass
    return result


# ---------------------------------------------------------------------------
# MeteoAlarm Italy (via Copernicus EFAS / meteoalarm.org Atom feed) helpers
# ---------------------------------------------------------------------------

_METEOALARM_ITALY_FEED = "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-italy"

# EMMA_ID → (region_name, centroid_lat, centroid_lon)
# Centroids are approximate geographic centres used for distance matching.
_ITALY_REGIONS: dict[str, tuple[str, float, float]] = {
    "IT001": ("Calabria", 38.9, 16.6),
    "IT002": ("Basilicata", 40.5, 16.0),
    "IT003": ("Puglia", 41.1, 16.5),
    "IT004": ("Campania", 40.8, 15.0),
    "IT005": ("Molise", 41.7, 14.6),
    "IT006": ("Abruzzo", 42.3, 13.8),
    "IT007": ("Lazio", 41.9, 12.7),
    "IT008": ("Sardinia", 40.1, 9.1),
    "IT009": ("Sicily", 37.6, 14.0),
    "IT010": ("Umbria", 43.1, 12.4),
    "IT011": ("Marche", 43.6, 13.5),
    "IT012": ("Tuscany", 43.8, 11.2),
    "IT013": ("Emilia-Romagna", 44.5, 11.3),
    "IT014": ("Liguria", 44.3, 8.5),
    "IT015": ("Friuli-Venezia Giulia", 46.2, 13.2),
    "IT016": ("Veneto", 45.5, 11.9),
    "IT017": ("Trentino-Alto Adige", 46.5, 11.3),
    "IT018": ("Lombardy", 45.5, 9.7),
    "IT019": ("Valle d'Aosta", 45.7, 7.4),
    "IT020": ("Piedmont", 44.9, 7.9),
    "IT021": ("Calabria (South)", 38.3, 16.0),
}

# MeteoAlarm awareness types that are connectivity/power relevant
_METEOALARM_POWER_TYPES = {
    "Wind", "Thunderstorms", "Snow/Ice", "Avalanches",
    "Extreme cold", "Fog", "Coastal Event", "Forest fire",
}

# MeteoAlarm cap:severity string → our numeric level (for min_severity filter)
_CAP_SEVERITY_LEVEL = {"Minor": "1", "Moderate": "2", "Severe": "3", "Extreme": "4"}

# cap:event keyword → awareness type (new CAP feed format)
_METEOALARM_EVENT_KEYWORD_MAP = {
    "thunderstorm": "Thunderstorms",
    "storm":        "Thunderstorms",
    "wind":         "Wind",
    "snow":         "Snow/Ice",
    "ice":          "Snow/Ice",
    "fog":          "Fog",
    "rain":         "Rain",
    "flood":        "Flooding",
    "avalanche":    "Avalanches",
    "cold":         "Extreme cold",
    "coastal":      "Coastal Event",
    "wave":         "Coastal Event",
    "fire":         "Forest fire",
}

# Atom namespace
_ATOM_NS = "http://www.w3.org/2005/Atom"
_CAP_NS = "urn:oasis:names:tc:emergency:cap:1.2"


def _fetch_meteoalarm_italy() -> list[dict]:
    """Fetch active MeteoAlarm Italy alerts.

    Returns list of dicts: {emma_id, region_name, awareness_type, level,
                             severity, headline, expires}.

    Supports both the legacy Atom summary format and the current CAP-element
    format where event type and severity come from cap:event / cap:severity.
    EMMA_ID is read from cap:geocode; region_name from cap:areaDesc (trusted
    over the static _ITALY_REGIONS name table).
    """
    try:
        resp = requests.get(_METEOALARM_ITALY_FEED, headers=_NWS_HEADERS, timeout=20)
        if resp.status_code != 200:
            return []
        root = ET.fromstring(resp.content)
    except Exception:
        return []

    results = []
    for entry in root.findall(f"{{{_ATOM_NS}}}entry"):
        # ── 1. EMMA_ID ────────────────────────────────────────────────────────
        emma_id = ""
        # Primary: cap:geocode / atom:valueName == "EMMA_ID" / atom:value
        geocode = entry.find(f"{{{_CAP_NS}}}geocode")
        if geocode is not None:
            vname = geocode.findtext(f"{{{_ATOM_NS}}}valueName", "")
            vval  = geocode.findtext(f"{{{_ATOM_NS}}}value", "")
            if vname == "EMMA_ID":
                emma_id = vval
        # Fallback: link href contains EMMA_ID:ITnnn
        if not emma_id:
            for link_el in entry.findall(f"{{{_ATOM_NS}}}link"):
                m = re.search(r"EMMA_ID:(IT\d{3})", link_el.get("href", ""))
                if m:
                    emma_id = m.group(1)
                    break
        # Last resort: legacy entry <id> URL pattern /ITnnn/
        if not emma_id:
            eid = (entry.findtext(f"{{{_ATOM_NS}}}id", "") or "")
            m = re.search(r"/(IT\d{3})/", eid)
            if m:
                emma_id = m.group(1)

        if not emma_id:
            continue

        # ── 2. region_name — trust cap:areaDesc from feed ─────────────────────
        area_desc = entry.findtext(f"{{{_CAP_NS}}}areaDesc", "").strip()
        region_info = _ITALY_REGIONS.get(emma_id)
        region_name = area_desc if area_desc else (region_info[0] if region_info else emma_id)

        # ── 3. awareness_type ─────────────────────────────────────────────────
        awareness_type = ""
        cap_event = entry.findtext(f"{{{_CAP_NS}}}event", "")
        if cap_event:
            # New format: "Yellow Thunderstorm Warning", "Yellow Wind Warning", etc.
            ev_lower = cap_event.lower()
            awareness_type = next(
                (v for k, v in _METEOALARM_EVENT_KEYWORD_MAP.items() if k in ev_lower), ""
            )
        if not awareness_type:
            # Legacy format: "Awareness Type: Wind; Awareness Level: 2" in summary/title
            summary = (entry.findtext(f"{{{_ATOM_NS}}}summary", "") or "")
            title   = (entry.findtext(f"{{{_ATOM_NS}}}title", "") or "")
            type_m = re.search(r"(?i)awareness[_ ]type[:\s]+([^;,\n]+)", summary or title)
            if type_m:
                awareness_type = type_m.group(1).strip()

        if not awareness_type or awareness_type not in _METEOALARM_POWER_TYPES:
            continue

        # ── 4. severity / level ───────────────────────────────────────────────
        cap_sev = entry.findtext(f"{{{_CAP_NS}}}severity", "")
        if cap_sev:
            level = _CAP_SEVERITY_LEVEL.get(cap_sev, "1")
            severity_label = cap_sev
        else:
            # Legacy: "Awareness Level: 2"
            summary = (entry.findtext(f"{{{_ATOM_NS}}}summary", "") or "")
            lm = re.search(r"(?i)awareness[_ ]level[:\s]+(\d)", summary)
            level = lm.group(1) if lm else "1"
            _legacy_sev = {"1": "Minor", "2": "Moderate", "3": "Severe", "4": "Extreme"}
            severity_label = _legacy_sev.get(level, "Minor")

        expires  = entry.findtext(f"{{{_CAP_NS}}}expires", "")
        headline = entry.findtext(f"{{{_ATOM_NS}}}title", "").strip()

        results.append({
            "emma_id":        emma_id,
            "region_name":    region_name,
            "awareness_type": awareness_type,
            "level":          level,
            "severity":       severity_label,
            "headline":       headline,
            "expires":        expires,
        })
    return results


def _match_meteoalarm_italy(
    lat: float,
    lon: float,
    italy_alerts: list[dict],
    radius_km: float = 150.0,
) -> list[dict]:
    """Return MeteoAlarm Italy alerts whose region centroid is within radius_km of lat/lon."""
    matches = []
    for alert in italy_alerts:
        region_info = _ITALY_REGIONS.get(alert["emma_id"])
        if not region_info:
            continue
        _, r_lat, r_lon = region_info
        dist = _haversine_km(lat, lon, r_lat, r_lon)
        if dist <= radius_km:
            matches.append(alert)
    return matches


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Starting gsa-jdp-toolkit on stdio...", file=sys.stderr)
    mcp.run(transport="stdio")
