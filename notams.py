"""NOTAM scanner: active NOTAMs for N89 and every airport within a 25nm
ring around it, shown as a small dashboard tile - just the airport and the
NOTAM number, linking out to the FAA's own NOTAM Search for the full text
rather than us trying to reformat/parse NOTAM contractions ourselves.

Uses the FAA's NMS-API (the successor to the old external-api.faa.gov
NOTAM API, which FAA has been retiring) - unlike the no-key weather/ADS-B
sources in weather.py/adsb.py, this needs a personal client_id/client_secret
issued during onboarding (delivered as a KEY/SECRET pair in a
password-protected spreadsheet). Fill in FAA_CLIENT_ID/FAA_CLIENT_SECRET
below once you have them. Until that's done, get_dashboard_notams()
degrades to just a link out to the FAA's public NOTAM Search pre-aimed at
N89 - the dashboard tile is never broken, just less convenient.

Auth is two-step, unlike the old API's static client_id/client_secret
headers:
  1. POST client_id/client_secret (as HTTP Basic auth) to the auth URL to
     get a short-lived bearer token (~30 min).
  2. Send that bearer token as an Authorization header on the actual
     NOTAM search call, along with an nmsResponseFormat header telling the
     API whether to hand back AIXM or GeoJSON - we ask for GeoJSON since
     it's the one that's actually convenient to parse in Python.
The bearer token is cached in-memory for its lifetime so we're not doing a
fresh auth round-trip on every dashboard load.
"""
import base64
import json
import os
import re
import time
import urllib.request
import urllib.parse
import urllib.error

import db
from adsb import N89_LAT, N89_LON

# Dashboard triage hierarchy - most operationally urgent first. Classified
# off the NOTAM's own classification field (FDC = the FAA's flight-data
# category, which is how TFRs are filed) and simple keyword/contraction
# matching on the NOTAM text, using the same standard FAA contractions
# (RWY/TWY/IAP/APCH) pilots already read NOTAMs in - not a full NOTAM
# parser, just enough to triage which ones need eyes first.
CATEGORY_ORDER = ["tfr", "closure", "runway", "taxiway", "approach", "other"]
CATEGORY_LABELS = {
    "tfr": "TFRs",
    "closure": "Airport Closures",
    "runway": "Runways",
    "taxiway": "Taxiways",
    "approach": "Approaches",
    "other": "Everything Else",
}
CATEGORY_RANK = {cat: i for i, cat in enumerate(CATEGORY_ORDER)}

_CLOSURE_RE = re.compile(r"\b(ARPT|AD|AIRPORT)\b[^.]{0,20}\bCLSD\b")
_RUNWAY_RE = re.compile(r"\bRWY\b")
_TAXIWAY_RE = re.compile(r"\bTWY\b")
_APPROACH_RE = re.compile(r"\b(IAP|APCH|APPROACH)\b")


def _classify_notam(core):
    classification = (core.get("classification") or "").upper()
    text = (core.get("text") or "").upper()
    if classification == "FDC" or "FLIGHT RESTRICTION" in text or re.search(r"\bTFR\b", text):
        return "tfr"
    if _CLOSURE_RE.search(text):
        return "closure"
    # Approach text (e.g. "IAP RNAV RWY 3 NA") often mentions a runway by
    # number as part of naming the procedure, so check the unambiguous
    # IAP/APCH keywords before the plain RWY one to avoid misclassifying
    # an approach NOTAM as a runway NOTAM.
    if _APPROACH_RE.search(text):
        return "approach"
    if _RUNWAY_RE.search(text):
        return "runway"
    if _TAXIWAY_RE.search(text):
        return "taxiway"
    return "other"

# Read from the environment rather than hardcoded here, so the credentials
# never end up committed to git (this file is fine to be public/private
# repo either way). Set OPSHUB_FAA_CLIENT_ID / OPSHUB_FAA_CLIENT_SECRET in
# the Pi's systemd unit (see opshub.service) - or export them in your shell
# for local testing.
FAA_CLIENT_ID = os.environ.get("OPSHUB_FAA_CLIENT_ID", "")
FAA_CLIENT_SECRET = os.environ.get("OPSHUB_FAA_CLIENT_SECRET", "")

# NMS-API is available in a few environments. Our client_id/client_secret
# are registered against Staging (Pre-Prod) (confirmed 2026-09-24: Prod
# rejects them with "ClientId is Invalid", Staging issues a real token) -
# if FAA later promotes/reissues these as a Prod registration, switch this
# to https://api-nms.aim.faa.gov.
NMS_API_BASE = "https://api-staging.cgifederal-aim.com"  # Staging (Pre-Prod)
# NMS_API_BASE = "https://api-nms.aim.faa.gov"  # Prod

NMS_AUTH_URL = NMS_API_BASE + "/v1/auth/token"
NMS_NOTAMS_URL = NMS_API_BASE + "/nmsapi/v1/notams"

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

# In-memory bearer token cache - just a module-level dict, no need to
# persist across process restarts since a fresh token is one cheap call
# away and tokens only live ~30 minutes anyway.
_token_cache = {"access_token": None, "expires_at": 0.0}


def _airport_label(icao_or_faa):
    """'KMGJ' or 'MGJ' -> ('Orange County', 14.0); unknown -> (the code
    itself, None) so a NOTAM for an airport outside our curated list still
    shows up rather than getting dropped."""
    code = (icao_or_faa or "").strip().upper()
    short = code[1:] if len(code) == 4 and code.startswith("K") else code
    label, distance = NEARBY_AIRPORTS.get(short, (code or "?", None))
    return short or code, label, distance


def _get_bearer_token():
    """Returns a cached bearer token if it's still fresh, otherwise trades
    the client_id/client_secret for a new one. Raises on failure so
    _fetch_notams can turn it into a dashboard-friendly error message."""
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["access_token"]

    basic = base64.b64encode(f"{FAA_CLIENT_ID}:{FAA_CLIENT_SECRET}".encode("utf-8")).decode("ascii")
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode("utf-8")
    req = urllib.request.Request(NMS_AUTH_URL, data=body, method="POST", headers={
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    token = data.get("access_token")
    if not token:
        raise ValueError("auth response had no access_token")
    # expires_in is seconds-as-a-string (e.g. "1799"); knock 60s off as a
    # safety margin so we don't hand out a token that expires mid-request.
    try:
        ttl = int(data.get("expires_in", 0))
    except (TypeError, ValueError):
        ttl = 0
    _token_cache["access_token"] = token
    _token_cache["expires_at"] = time.time() + max(ttl - 60, 60)
    return token


def _fetch_notams():
    if not FAA_CLIENT_ID or not FAA_CLIENT_SECRET:
        return {"error": "not_configured", "notams": [], "airport_groups": []}

    try:
        token = _get_bearer_token()
    except Exception as e:
        return {"error": f"Couldn't authenticate with the FAA NMS-API ({e.__class__.__name__}).", "notams": [], "airport_groups": []}

    params = {
        "latitude": N89_LAT,
        "longitude": N89_LON,
        "radius": SEARCH_RADIUS_NM,
    }
    url = NMS_NOTAMS_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "nmsResponseFormat": "GEOJSON",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            # Token may have been rejected/expired server-side even though
            # our local clock thought it was still good - drop it so the
            # next dashboard refresh re-authenticates instead of retrying
            # with the same bad token every 30 minutes.
            _token_cache["access_token"] = None
        return {"error": f"FAA NMS-API returned HTTP {e.code}.", "notams": [], "airport_groups": []}
    except Exception as e:
        return {"error": f"Couldn't reach the FAA NMS-API ({e.__class__.__name__}).", "notams": [], "airport_groups": []}

    if data.get("status") != "Success":
        errors = data.get("errors") or []
        msg = errors[0].get("message") if errors and isinstance(errors[0], dict) else None
        return {"error": msg or "FAA NMS-API reported a failure.", "notams": [], "airport_groups": []}

    features = ((data.get("data") or {}).get("geojson")) or []
    notams = []
    for feature in features:
        try:
            core = feature["properties"]["coreNOTAMData"]["notam"]
        except (KeyError, TypeError):
            continue
        raw_loc = core.get("icaoLocation") or core.get("location") or ""
        short, label, distance = _airport_label(raw_loc)
        category = _classify_notam(core)
        notams.append({
            "icao": short,
            "airport_label": label,
            "distance_nm": distance,
            "number": core.get("number") or "(no number)",
            "notam_type": core.get("type"),
            "effective_start": core.get("effectiveStart"),
            "effective_end": core.get("effectiveEnd"),
            "category": category,
            "category_label": CATEGORY_LABELS[category],
        })
    notams.sort(key=lambda n: (n["distance_nm"] if n["distance_nm"] is not None else 999, n["icao"]))
    return {"error": None, "notams": notams, "airport_groups": _group_by_airport(notams)}


def _group_by_airport(notams):
    """Rolls the flat, distance-sorted notam list up into one entry per
    affected airport, each carrying its own notams pre-sorted by the
    triage hierarchy (CATEGORY_ORDER) - this is what the dashboard tile
    actually renders: one clickable, red-flagged row per airport that has
    something active, worst-first."""
    groups = {}
    for n in notams:
        g = groups.setdefault(n["icao"], {
            "icao": n["icao"], "airport_label": n["airport_label"],
            "distance_nm": n["distance_nm"], "notams": [],
        })
        g["notams"].append(n)
    airport_groups = list(groups.values())
    for g in airport_groups:
        g["notams"].sort(key=lambda n: CATEGORY_RANK.get(n["category"], 99))
        g["top_category"] = g["notams"][0]["category"]
        g["top_category_label"] = g["notams"][0]["category_label"]
    airport_groups.sort(key=lambda g: (
        CATEGORY_RANK.get(g["top_category"], 99),
        g["distance_nm"] if g["distance_nm"] is not None else 999,
    ))
    return airport_groups


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
    # For the dashboard strip: every nearby airport, N89 first then by
    # increasing distance (same order as payload["airports"] above, since
    # N89 is distance 0.0) - not the severity order airport_groups uses,
    # just "is this airport worth a glance" in geographic order.
    groups_by_icao = {g["icao"]: g for g in payload.get("airport_groups", [])}
    payload["strip_airports"] = [{
        "icao": a["icao"], "label": a["label"], "distance_nm": a["distance_nm"],
        "has_notams": a["icao"] in groups_by_icao,
        "notams": groups_by_icao[a["icao"]]["notams"] if a["icao"] in groups_by_icao else [],
    } for a in payload["airports"]]

    try:
        conn.execute(
            "INSERT INTO weather_cache (cache_key, payload, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(cache_key) DO UPDATE SET payload = excluded.payload, fetched_at = excluded.fetched_at",
            (CACHE_KEY, json.dumps(payload), db.now_iso()))
        conn.commit()
    except Exception:
        pass  # caching is best-effort - a write failure shouldn't break the dashboard

    return payload
