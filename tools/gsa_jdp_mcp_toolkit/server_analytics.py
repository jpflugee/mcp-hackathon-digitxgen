#!/usr/bin/env python3
"""GSA MAR Analytics MCP Server — communications outage probability tools.

Completely separate from server.py (James's code). Shares no state with it.

Tools
-----
gsa_mar_comms_outage_scan      — scan ALL GSA sites in a state/area, score each
                                   using the logistic comms-outage model, return
                                   sites above a probability threshold in descending
                                   order (default: top 10 sites in VA above 50%)

gsa_mar_comms_outage_building  — score comms outage probability for a single GSA
                                   building by name, partial name, or location code
"""
import sys
import math
import re
import datetime
import requests
from concurrent.futures import ThreadPoolExecutor
from mcp.server.mcpserver import MCPServer

mcp = MCPServer(name="gsa-mar-analytics", version="0.4.0")

# ---------------------------------------------------------------------------
# IOLP helpers
# ---------------------------------------------------------------------------

_GSA_FEATURE_URL = (
    "https://services1.arcgis.com/eBupDfPlEJK3mdAm/ArcGIS/rest/services/"
    "IOLP_NEW/FeatureServer/0/query"
)
_GSA_FIELDS = (
    "LOCATION_CODE,Real_Property_Asset_Name,STREET_ADDRESS,CITY,STATE_CD,"
    "ZIPCODE5,Latitude,Longitude,REGION_CODE"
)


def _query_iolp(where: str, max_records: int = 200) -> list[dict]:
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


def _find_building(query: str) -> dict | None:
    """Find a single GSA building by partial name, full name, or location code."""
    safe = query.strip().replace("'", "''")
    if re.match(r'^[A-Za-z]{2}\d+$', safe):
        hits = _query_iolp(f"UPPER(LOCATION_CODE) = UPPER('{safe}')", max_records=1)
    else:
        hits = _query_iolp(
            f"UPPER(Real_Property_Asset_Name) LIKE UPPER('%{safe}%')",
            max_records=5,
        )
    return next((h for h in hits if h.get("Latitude") and h.get("Longitude")), None)


def _get_sites_in_area(state: str, max_records: int) -> list[dict]:
    """Return GSA sites for a given state code."""
    safe = state.strip().upper().replace("'", "")
    return _query_iolp(f"STATE_CD = '{safe}'", max_records=max_records)


# ---------------------------------------------------------------------------
# NWS helpers
# ---------------------------------------------------------------------------

_NWS_BASE = "https://api.weather.gov"
_NWS_HEADERS = {
    "User-Agent": "GSA-MAR-Analytics/1.0",
    "Accept": "application/geo+json",
}

# Cache all active alerts once per tool call to avoid N×2 API hits
_ACTIVE_ALERTS_CACHE: list[dict] = []
_ACTIVE_ALERTS_TS: datetime.datetime | None = None


def _get_all_active_alerts() -> list[dict]:
    """Fetch (and cache for this process lifetime) all active NWS alert features."""
    global _ACTIVE_ALERTS_CACHE, _ACTIVE_ALERTS_TS
    now = datetime.datetime.utcnow()
    if _ACTIVE_ALERTS_TS and (now - _ACTIVE_ALERTS_TS).total_seconds() < 120:
        return _ACTIVE_ALERTS_CACHE
    try:
        resp = requests.get(
            f"{_NWS_BASE}/alerts/active",
            headers=_NWS_HEADERS,
            params={"status": "actual", "message_type": "alert,update"},
            timeout=30,
        )
        resp.raise_for_status()
        _ACTIVE_ALERTS_CACHE = resp.json().get("features", [])
        _ACTIVE_ALERTS_TS = now
    except Exception:
        _ACTIVE_ALERTS_CACHE = []
    return _ACTIVE_ALERTS_CACHE


def _build_zone_alert_index(features: list[dict]) -> dict[str, list[dict]]:
    """Build zone_id → [alert_props, ...] index from NWS alert features."""
    index: dict[str, list[dict]] = {}
    for feat in features:
        props = feat.get("properties", {})
        for zone_url in props.get("affectedZones", []):
            zone_id = zone_url.rsplit("/", 1)[-1]
            index.setdefault(zone_id, []).append(props)
    return index


def _resolve_zones(lat: float, lon: float) -> tuple[str, str]:
    """Return (forecastZoneId, countyZoneId) for a coordinate."""
    url = f"{_NWS_BASE}/points/{round(lat, 4)},{round(lon, 4)}"
    try:
        resp = requests.get(url, headers=_NWS_HEADERS, timeout=15)
        if resp.status_code != 200:
            return "", ""
        props = resp.json().get("properties", {})
        fz = props.get("forecastZone", "").rsplit("/", 1)[-1]
        cy = props.get("county", "").rsplit("/", 1)[-1]
        return fz, cy
    except Exception:
        return "", ""


def _alerts_for_site(lat: float, lon: float,
                     zone_index: dict[str, list[dict]]) -> list[dict]:
    """Return deduped alert props for a site using a pre-built zone index."""
    fz, cy = _resolve_zones(lat, lon)
    seen: set[str] = set()
    result: list[dict] = []
    for zone_id in (fz, cy):
        if not zone_id:
            continue
        for props in zone_index.get(zone_id, []):
            aid = props.get("id") or props.get("@id", "")
            if aid not in seen:
                seen.add(aid)
                result.append(props)
    return result


# ---------------------------------------------------------------------------
# Comms outage logistic model
# ---------------------------------------------------------------------------

_SEV_MAP = {"Minor": 1, "Moderate": 2, "Severe": 3, "Extreme": 4}

# H — Infrastructure-Destructive Hazard Flag (spec-exact 5 types):
# Events that physically snap aerial fiber or flood underground vaults.
_DESTRUCTIVE_EVENTS = {
    "Ice Storm Warning",
    "Hurricane Warning",
    "Tornado Warning",
    "High Wind Warning",
    "Flash Flood Warning",
}

# C — Compound Hazard Flag:
# 1 when two or more alerts overlap at the same site, else 0.


def _comms_prob(severity: str, duration_h: float,
                is_compound: bool, is_destructive: bool) -> float:
    """z = -4 + 1.2S + 0.25D + 1.5C + 1.0H  →  prob = 1/(1+exp(-z))"""
    s = _SEV_MAP.get(severity, 1)
    d = min(max(duration_h, 0), 12)
    c = 1 if is_compound else 0
    h = 1 if is_destructive else 0
    z = -4.0 + (1.2 * s) + (0.25 * d) + (1.5 * c) + (1.0 * h)
    return round(1 / (1 + math.exp(-z)) * 100, 1)


def _parse_duration(onset_str: str, expires_str: str) -> float:
    try:
        onset   = datetime.datetime.fromisoformat(onset_str)
        expires = datetime.datetime.fromisoformat(expires_str)
        return max(0.0, round((expires - onset).total_seconds() / 3600, 1))
    except Exception:
        return 6.0


def _risk_label(prob: float) -> str:
    if prob >= 80: return "CRITICAL"
    if prob >= 60: return "HIGH"
    if prob >= 40: return "MODERATE"
    if prob >= 20: return "LOW"
    return "MINIMAL"


def _score_site(alerts: list[dict]) -> tuple[float, str, list[dict]]:
    """Score a site's alerts. Returns (top_prob, risk_label, scored_alerts)."""
    if not alerts:
        baseline = _comms_prob("Minor", 0, False, False)
        return baseline, _risk_label(baseline), []

    is_compound = len(alerts) >= 2
    scored = []
    for a in alerts:
        event    = a.get("event", "Unknown")
        severity = a.get("severity", "Minor")
        if severity not in _SEV_MAP:
            severity = "Minor"
        onset   = a.get("onset", "") or a.get("effective", "")
        expires = a.get("expires", "") or a.get("ends", "")
        duration    = _parse_duration(onset, expires) if onset and expires else 6.0
        is_dest     = event in _DESTRUCTIVE_EVENTS
        prob        = _comms_prob(severity, duration, is_compound, is_dest)
        scored.append({
            "event": event, "severity": severity, "duration_hours": duration,
            "is_compound": is_compound, "is_destructive": is_dest,
            "probability": prob, "risk": _risk_label(prob),
            "headline": (a.get("headline", "") or "")[:80],
            "expires": (expires or "")[:16],
        })
    scored.sort(key=lambda x: x["probability"], reverse=True)
    top = scored[0]
    return top["probability"], top["risk"], scored


# ---------------------------------------------------------------------------
# Tool 1: gsa_mar_comms_outage_scan — area-wide scan
# ---------------------------------------------------------------------------

@mcp.tool(
    name="gsa_mar_comms_outage_scan",
    description=(
        "Scan all GSA sites in a given US state for communications outage risk. "
        "Fetches live NWS alerts for each site, scores each using the logistic model "
        "(S=severity, D=duration capped 12h, C=compound overlap, H=destructive hazard), "
        "filters to sites above a probability threshold, and returns them in descending "
        "order of comms outage probability. "
        "Default: top 10 sites in Virginia (state='VA') above 50% probability. "
        "Use this for questions like: 'Which GSA sites in TX have the highest comms "
        "outage risk?' or 'List GSA buildings with communication failure alerts in FL'."
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
        min_probability: Only return sites at or above this probability % (default 50.0).
                         Set to 0 to return all sites with any active alert.
        top_n: Maximum number of sites to return, sorted highest-first (default 10).
        max_sites: Maximum GSA sites to fetch and check (default 200).

    Returns:
        Ranked list of GSA sites above the threshold, highest probability first.
    """
    # 1. Fetch sites
    try:
        sites = _get_sites_in_area(state, max_sites)
    except Exception as exc:
        return f"Failed to fetch GSA sites for state='{state}': {exc}"
    if not sites:
        return f"No GSA sites found for state='{state}'."

    # 2. Fetch all active NWS alerts once and build zone index
    all_alerts = _get_all_active_alerts()
    zone_index = _build_zone_alert_index(all_alerts)

    # 3. Resolve zones + score each site concurrently
    def _process(rec: dict) -> dict | None:
        lat = rec.get("Latitude")
        lon = rec.get("Longitude")
        if not lat or not lon:
            return None
        alerts = _alerts_for_site(float(lat), float(lon), zone_index)
        prob, risk, scored = _score_site(alerts)
        return {
            "rec": rec,
            "probability": prob,
            "risk": risk,
            "alerts": scored,
            "alert_count": len(alerts),
        }

    results = []
    with ThreadPoolExecutor(max_workers=20) as ex:
        for r in ex.map(_process, sites):
            if r is not None:
                results.append(r)

    # 4. Filter and sort
    above_threshold = [r for r in results if r["probability"] >= min_probability]
    above_threshold.sort(key=lambda x: x["probability"], reverse=True)
    top = above_threshold[:top_n]

    # 5. Format output
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"GSA Comms Outage Scan — State: {state.upper()}",
        f"Threshold: ≥{min_probability}%  |  Showing top {top_n}  |  "
        f"Sites checked: {len(results)}  |  {ts} UTC",
        f"NWS active alerts in feed: {len(all_alerts)}",
        "=" * 72,
    ]

    if not top:
        lines += [
            "",
            f"  ✅  No GSA sites in {state.upper()} exceed {min_probability}% "
            f"communications outage probability.",
            f"  ({len(results)} sites checked against {len(all_alerts)} active NWS alerts)",
        ]
    else:
        lines += ["", f"  {len(above_threshold)} site(s) at or above {min_probability}% "
                  f"— showing top {len(top)}:", ""]
        for i, r in enumerate(top, 1):
            rec = r["rec"]
            name  = rec.get("Real_Property_Asset_Name", "Unknown")
            code  = rec.get("LOCATION_CODE", "")
            city  = rec.get("CITY", "")
            addr  = rec.get("STREET_ADDRESS", "")
            lines.append(f"  {'─'*68}")
            lines.append(
                f"  #{i}  [{r['risk']}]  {r['probability']}%  —  [{code}] {name}"
            )
            lines.append(f"       {addr}, {city}, {state.upper()}")
            if r["alerts"]:
                top_alert = r["alerts"][0]
                lines.append(
                    f"       Top alert : {top_alert['event']} [{top_alert['severity']}]"
                    + (f", expires {top_alert['expires']}" if top_alert["expires"] else "")
                )
                if r["alert_count"] > 1:
                    lines.append(
                        f"       C=1 (compound): {r['alert_count']} overlapping alerts"
                    )
        lines.append(f"  {'─'*68}")

    lines += [
        "", "=" * 72,
        "Model: z = -4.0 + 1.2*S + 0.25*min(D,12) + 1.5*C + 1.0*H",
        "S=severity(1-4)  D=duration_hours  C=compound(≥2 alerts)  H=destructive_hazard",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 2: gsa_mar_comms_outage_building — single building lookup
# ---------------------------------------------------------------------------

@mcp.tool(
    name="gsa_mar_comms_outage_building",
    description=(
        "Score the communications outage probability for a single GSA building. "
        "Accepts partial name, full name, or location code (e.g. 'DC0003', 'WHITTEN', "
        "'1400 Independence'). Fetches live NWS alerts for the site and scores using "
        "the logistic model (S=severity, D=duration capped 12h, C=compound overlap, "
        "H=destructive hazard). Returns a detailed per-alert breakdown with model inputs. "
        "Use this for questions like: 'What is the comms outage risk for WHITTEN?' or "
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
    # 1. Resolve building
    rec = _find_building(building_name)
    if not rec:
        return (
            f"No GSA building found matching '{building_name}'. "
            "Try a partial name (e.g. 'WHITTEN'), location code (e.g. 'DC0003'), "
            "or partial address (e.g. '1400 Independence')."
        )

    name  = rec.get("Real_Property_Asset_Name", "Unknown")
    code  = rec.get("LOCATION_CODE", "")
    city  = rec.get("CITY", "")
    state = rec.get("STATE_CD", "")
    addr  = rec.get("STREET_ADDRESS", "")
    lat   = float(rec["Latitude"])
    lon   = float(rec["Longitude"])

    # 2. Resolve NWS zones
    fz, cy = _resolve_zones(lat, lon)
    zones = [z for z in (fz, cy) if z]
    if not zones:
        return (
            f"[{code}] {name} — {city}, {state}\n"
            f"  ⚠  Could not resolve NWS zone (site may be outside US NWS coverage).\n"
            f"  Communications outage probability: N/A"
        )

    # 3. Fetch alerts for this site
    all_alerts = _get_all_active_alerts()
    zone_index = _build_zone_alert_index(all_alerts)
    alerts = _alerts_for_site(lat, lon, zone_index)

    # 4. Score
    top_prob, top_risk, scored = _score_site(alerts)
    is_compound = len(alerts) >= 2

    # 5. Format output
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
        lines += [
            "",
            "  ✅  No active NWS alerts for this site.",
            f"  Communications outage probability: MINIMAL — {baseline}%",
            "  (S=Minor/1, D=0h, C=No, H=No)",
        ]
    else:
        lines += [
            "",
            f"  SITE RISK: [{top_risk}]  {top_prob}% comms outage probability",
            f"  Active alerts: {len(scored)}"
            + ("  ← C=1 (compound: ≥2 overlapping)" if is_compound else ""),
            "",
        ]
        for i, s in enumerate(scored, 1):
            lines.append(f"  {'─'*68}")
            lines.append(f"  Alert #{i}  [{s['risk']}]  {s['probability']}%")
            lines.append(f"    Event    : {s['event']}")
            if s["headline"]:
                lines.append(f"    Headline : {s['headline']}")
            lines.append(
                f"    Expires  : {s['expires'] if s['expires'] else 'N/A'}"
            )
            if include_model_inputs:
                lines.append(
                    f"    Inputs   : "
                    f"S={s['severity']}({_SEV_MAP.get(s['severity'],1)})  "
                    f"D={s['duration_hours']}h  "
                    f"C={'1 (compound)' if s['is_compound'] else '0'}  "
                    f"H={'1 (destructive)' if s['is_destructive'] else '0'}"
                )
        lines.append(f"  {'─'*68}")

    lines += [
        "", "=" * 72,
        "Model: z = -4.0 + 1.2*S + 0.25*min(D,12) + 1.5*C + 1.0*H",
        "S=severity(1-4)  D=duration_hours  C=compound(≥2 alerts)  H=destructive_hazard",
        "prob = 1 / (1 + exp(-z))",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Starting gsa-mar-analytics on stdio...", file=sys.stderr)
    mcp.run(transport="stdio")
