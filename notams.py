"""NOTAM scanner: active NOTAMs for N89 and every airport within a 25nm
ring around it, shown as a small dashboard tile - just the airport and the
NOTAM number, linking out to the FAA's own NOTAM Search for the full text
rather than us trying to reformat/parse NOTAM contractions ourselves.

Uses the FAA's official NOTAM API (https://api.faa.gov) - unlike the
no-key weather/ADS-B sources in weather.py/adsb.py, the FAA locked down
bulk NOTAM access a few years back, so this needs a personal (free)
client_id/client_secret. Register at https://api.faa.gov -> "NOTAM API" ->
Request Access, then fill in FAA_CLIENT_ID/FAA_CLIENT_SECRET below. Until
that's done, get_dashboard_notams() degrades to just a link out to the
FAA's public NOTAM Search pre-aimed at N89 - the dashboard tile is never
broken, just less convenient.
"""
import json
import urllib.request
import urllib.parse

import db
from adsb import N89_LAT, N89_LON

# Fill these in after registering (free) at https://api.faa.gov.
FAA_CLIENT_ID = ""
FAA_CLIENT_SECRET = ""

NOTAM_API_URL = "https://external-api.faa.gov/notamapi/v1/notams"
FAA_NOTAM_SEARCH_URL = "https://notams.aim.faa.gov/notamSearch/nsapp.html"

SEARCH_RADIUS_NM = 25
CACHE_KEY = "dashboard_notams_n89_25nm"
CACHE_TTL_SECONDS = 30 * 60  # NOTAMs can post any time, but a half hour is plenty fresh for a dashboard tile

# Airports within 25nm of N89 (Joseph Y. Resnick, Ellenville NY), verified
# via SkyVector's own "nearby airports" distances - used to label results
# nicely and to sort near-to-far, purely cosmetic (the API search itself is
# one lat/lon/radius call that already covers all of these).
NEARBY_AIRPORTS = {
    "N89": ("Joseph Y. Resnick (Ellenville)", 0.0),
    "N82": ("Wurtsboro-Sullivan County", 8.7),
    "MGJ": ("Orange County", 14.0),
    "06N": ("Randall (Middletown)", 17.8),
    "SWF": ("New York Stewart Intl", 18.2),
    "MSV": ("Sullivan County Intl (Monticello)", 18.9),
    "POU": ("Hudson Valley Regional", 23.0),
}


def _airport_label(icao_or_faa):
    """'KMGJ' or 'MGJ' -> ('Orange County', 14.0); unknown -> (the code
    itself, None) so a NOTAM for an airport outside our curated list still
    shows up rather than getting dropped."""
    code = (icao_or_faa or "").strip().upper()
    short = code[1:] if len(code) == 4 and code.startswith("K") else code
    label, distance = NEARBY_AIRPORTS.get(short, (code or "?", None))
    return short or code, label, distance


def _fetch_notams():
    if not FAA_CLIENT_ID or not FAA_CLIENT_SECRET:
        return {"error": "not_configured", "notams": []}
    params = {
        "locationLongitude": N89_LON,
        "locationLatitude": N89_LAT,
        "locationRadius": SEARCH_RADIUS_NM,
        "pageSize": 50,
        "pageNum": 1,
        "sortBy": "effectiveStartDate",
        "sortOrder": "Desc",
    }
    url = NOTAM_API_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "client_id": FAA_CLIENT_ID,
        "client_secret": FAA_CLIENT_SECRET,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": f"Couldn't reach the FAA NOTAM API ({e.__class__.__name__}).", "notams": []}

    items = data.get("items") or []
    notams = []
    for item in items:
        try:
            core = item["properties"]["coreNOTAMData"]["notam"]
        except (KeyError, TypeError):
            continue
        raw_loc = core.get("icaoLocation") or core.get("location") or ""
        short, label, distance = _airport_label(raw_loc)
        notams.append({
            "icao": short,
            "airport_label": label,
            "distance_nm": distance,
            "number": core.get("number") or "(no number)",
            "notam_type": core.get("type"),
            "effective_start": core.get("effectiveStart"),
            "effective_end": core.get("effectiveEnd"),
        })
    notams.sort(key=lambda n: (n["distance_nm"] if n["distance_nm"] is not None else 999, n["icao"]))
    return {"error": None, "notams": notams}


def get_dashboard_notams(conn, force=False):
    """Cached wrapper, same shape/pattern as weather.get_dashboard_weather -
    reuses the shared weather_cache table (it's a generic key/payload cache,
    not weather-specific) so this doesn't need its own migration."""
    if not force:
        row = conn.execute(
            "SELECT payload, fetched_at FROM weather_cache WHERE cache_key = ?", (CACHE_KEY,)
        ).fetchone()
        if row:
            from datetime import datetime
            try:
                fetched = datetime.strptime(row["fetched_at"], "%Y-%m-%d %H:%M:%S")
                age = (datetime.now() - fetched).total_seconds()
            except Exception:
                age = CACHE_TTL_SECONDS + 1
            if 0 <= age <= CACHE_TTL_SECONDS:
                try:
                    return json.loads(row["payload"])
                except Exception:
                    pass  # fall through and refetch on a corrupt cache row

    payload = _fetch_notams()
    payload["search_url"] = FAA_NOTAM_SEARCH_URL
    payload["configured"] = bool(FAA_CLIENT_ID and FAA_CLIENT_SECRET)
    payload["airports"] = [{"icao": k, "label": v[0], "distance_nm": v[1]} for k, v in
                            sorted(NEARBY_AIRPORTS.items(), key=lambda kv: kv[1][1])]

    try:
        conn.execute(
            "INSERT INTO weather_cache (cache_key, payload, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(cache_key) DO UPDATE SET payload = excluded.payload, fetched_at = excluded.fetched_at",
            (CACHE_KEY, json.dumps(payload), db.now_iso()))
        conn.commit()
    except Exception:
        pass  # caching is best-effort - a write failure shouldn't break the dashboard

    return payload
