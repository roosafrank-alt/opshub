"""Live ADS-B position lookup for the Active Flight tab, via OpenSky
Network's free public API (https://opensky-network.org/apidoc/).

Two modes:
- "school": only planes with a flight currently clocked in AND an
  ICAO24/Mode S hex on file (assets.icao24_hex) - looked up by hex, one
  OpenSky call for the whole list.
- "all": every aircraft OpenSky is currently reporting within a 25nm
  bounding box around N89 (Joseph Y. Resnick Airport, Ellenville NY -
  41.72778, -74.37722 [wikipedia]), cross-referenced against known school
  planes so those can be shown differently on the map/list.

Deliberately does NOT try to derive a plane's ICAO24 hex from its tail
number - that conversion has enough edge cases that getting it wrong
risks showing the position of the WRONG aircraft, which is worse than
just not showing one. Instead each school plane's hex is entered once by
an admin on its profile - see asset_form.html. A plane with no hex on
file simply doesn't appear as "school" traffic (it can still show up as
unidentified traffic in "all" mode, just not labeled).

Only the standard library is used (urllib, json), same reasoning as
notify.py/weather.py. Results are cached briefly (CACHE_TTL_SECONDS)
since OpenSky's anonymous/free tier is rate-limited and the map polls on
an interval.
"""

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import db

CACHE_TTL_SECONDS = 15
REQUEST_TIMEOUT = 8
OPENSKY_URL = "https://opensky-network.org/api/states/all"

# N89 - Joseph Y. Resnick Airport, Ellenville NY (41°43'40"N 074°22'38"W).
N89_LAT = 41.72778
N89_LON = -74.37722
AREA_RADIUS_NM = 25


def _get_json(url, timeout=REQUEST_TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": "OpsHub-FlightSchool/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _cache_get(conn, cache_key, ttl):
    row = conn.execute(
        "SELECT payload, fetched_at FROM weather_cache WHERE cache_key = ?", (cache_key,)
    ).fetchone()
    if not row:
        return None
    try:
        fetched = datetime.strptime(row["fetched_at"], "%Y-%m-%d %H:%M:%S")
        age = (datetime.now() - fetched).total_seconds()
    except Exception:
        return None
    if not (0 <= age <= ttl):
        return None
    try:
        return json.loads(row["payload"])
    except Exception:
        return None


def _cache_set(conn, cache_key, payload):
    try:
        conn.execute(
            "INSERT INTO weather_cache (cache_key, payload, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(cache_key) DO UPDATE SET payload = excluded.payload, fetched_at = excluded.fetched_at",
            (cache_key, json.dumps(payload), db.now_iso()))
        conn.commit()
    except Exception:
        pass


TRACK_MAX_AGE_HOURS = 12
TRACK_MAX_POINTS_PER_PLANE = 400


def _record_track_points(conn, entries):
    """Appends one breadcrumb point per school plane with a live position,
    from a genuinely fresh OpenSky fetch (called only where the cache was a
    miss - see get_live_positions). Also prunes old points so the table
    stays small; this runs on the same cadence as fresh fetches (every
    ~15s at most, since most polls are served from cache), which is cheap
    enough not to need a separate cleanup job."""
    now = db.now_iso()
    fresh = [e for e in entries if e.get("is_school_plane") and e.get("lat") is not None and e.get("lon") is not None]
    if not fresh:
        return
    try:
        conn.executemany(
            "INSERT INTO adsb_track_points (icao24, lat, lon, recorded_at) VALUES (?, ?, ?, ?)",
            [(e["icao24"], e["lat"], e["lon"], now) for e in fresh])
        conn.execute(
            "DELETE FROM adsb_track_points WHERE recorded_at < datetime('now', ?)",
            (f"-{TRACK_MAX_AGE_HOURS} hours",))
        conn.commit()
    except Exception:
        pass


def get_track(conn, icao24, since_minutes=None):
    """Recent breadcrumb trail for one plane, oldest first, capped to
    TRACK_MAX_POINTS_PER_PLANE most recent points so a long flight doesn't
    hand back an unbounded list."""
    icao24 = (icao24 or "").strip().lower()
    if not icao24:
        return []
    sql = "SELECT lat, lon, recorded_at FROM adsb_track_points WHERE icao24 = ?"
    params = [icao24]
    if since_minutes:
        sql += " AND recorded_at >= datetime('now', ?)"
        params.append(f"-{since_minutes} minutes")
    sql += " ORDER BY recorded_at DESC LIMIT ?"
    params.append(TRACK_MAX_POINTS_PER_PLANE)
    rows = conn.execute(sql, params).fetchall()
    return [{"lat": r["lat"], "lon": r["lon"], "recorded_at": r["recorded_at"]} for r in reversed(rows)]


def distance_nm(lat1, lon1, lat2, lon2):
    """Great-circle distance in nautical miles (haversine)."""
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return None
    r_nm = 3440.065  # Earth radius in nautical miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r_nm * math.asin(math.sqrt(a))


def _area_bbox(lat, lon, radius_nm):
    """A simple lat/lon bounding box roughly radius_nm around a point -
    good enough for an OpenSky query prefilter (it doesn't need to be a
    precise circle, just an area that comfortably contains it)."""
    lat_delta = radius_nm / 60.0  # 1 degree latitude ~= 60 nm
    lon_delta = radius_nm / (60.0 * max(math.cos(math.radians(lat)), 0.1))
    return lat - lat_delta, lat + lat_delta, lon - lon_delta, lon + lon_delta


def _school_plane_map(conn):
    """icao24 hex (lowercase) -> {asset_id, plane_tag}, for every plane
    that has one on file, regardless of whether it's currently flying."""
    rows = conn.execute(
        "SELECT id as asset_id, tag as plane_tag, icao24_hex FROM assets "
        "WHERE icao24_hex IS NOT NULL AND icao24_hex != '' AND deleted_at IS NULL AND show_on_map = 1"
    ).fetchall()
    return {r["icao24_hex"].lower(): {"asset_id": r["asset_id"], "plane_tag": r["plane_tag"]} for r in rows}


def _active_flight_map(conn):
    """asset_id -> flight details, for planes with a flight clocked in right now."""
    rows = conn.execute("""
        SELECT f.asset_id, f.started_at, s.name as student_name, f.solo, c.name as cfi_name
        FROM flights f
        JOIN students s ON s.id = f.student_id
        LEFT JOIN cfis c ON c.id = f.cfi_id
        WHERE f.started_at IS NOT NULL AND f.ended_at IS NULL
    """).fetchall()
    return {r["asset_id"]: dict(r) for r in rows}


def _state_to_dict(s):
    # OpenSky state vector column order: icao24, callsign, origin_country,
    # time_position, last_contact, longitude, latitude, baro_altitude,
    # on_ground, velocity, true_track, vertical_rate, ...
    return {
        "icao24": (s[0] or "").strip().lower(),
        "callsign": (s[1] or "").strip() or None,
        "lon": s[5], "lat": s[6],
        "altitude_ft": (s[7] * 3.28084) if s[7] is not None else None,
        "on_ground": s[8],
        "speed_kt": (s[9] * 1.94384) if s[9] is not None else None,
        "track_deg": s[10],
        "last_contact": s[4],
    }


def _enrich(state, school_map, flight_map):
    """Adds distance-from-N89, school-plane flag, and (if it's a school
    plane with a flight running) elapsed/student details to one state."""
    d = dict(state)
    d["distance_nm_from_n89"] = distance_nm(state.get("lat"), state.get("lon"), N89_LAT, N89_LON)
    school = school_map.get(state["icao24"])
    if school:
        d["is_school_plane"] = True
        d["asset_id"] = school["asset_id"]
        d["plane_tag"] = school["plane_tag"]
        flight = flight_map.get(school["asset_id"])
        if flight:
            d["flight_active"] = True
            d["started_at"] = flight["started_at"]
            d["student_name"] = flight["student_name"]
            d["cfi_name"] = flight["cfi_name"]
            d["solo"] = bool(flight["solo"])
        else:
            d["flight_active"] = False
    else:
        d["is_school_plane"] = False
        d["asset_id"] = None
        d["plane_tag"] = None
        d["flight_active"] = False
    return d


def get_live_positions(conn, mode="school", force=False):
    """mode='school': one entry per currently-active school flight whose
    plane has a hex on file (position may be None if OpenSky has nothing).
    mode='all': every aircraft OpenSky reports within AREA_RADIUS_NM of
    N89, each flagged is_school_plane / flight_active as applicable.
    Never raises - any OpenSky/network failure degrades to an empty or
    partial list rather than breaking the page."""
    school_map = _school_plane_map(conn)
    flight_map = _active_flight_map(conn)

    if mode == "all":
        cache_key = "adsb_area_all"
        cached = None if force else _cache_get(conn, cache_key, CACHE_TTL_SECONDS)
        if cached is not None:
            return cached
        lamin, lamax, lomin, lomax = _area_bbox(N89_LAT, N89_LON, AREA_RADIUS_NM)
        params = {"lamin": lamin, "lamax": lamax, "lomin": lomin, "lomax": lomax}
        try:
            data = _get_json(OPENSKY_URL + "?" + urllib.parse.urlencode(params))
            states = data.get("states") or []
        except Exception:
            states = []
        result = [_enrich(_state_to_dict(s), school_map, flight_map) for s in states]
        # Keep it to genuinely within-radius traffic (the bbox is a square,
        # slightly wider than the circle at the corners).
        result = [r for r in result if r["distance_nm_from_n89"] is None or r["distance_nm_from_n89"] <= AREA_RADIUS_NM]
        _cache_set(conn, cache_key, result)
        _record_track_points(conn, result)
        return result

    # mode == "school"
    tracked = [{"asset_id": v["asset_id"], "plane_tag": v["plane_tag"], "icao24_hex": h}
               for h, v in school_map.items() if v["asset_id"] in flight_map]
    if not tracked:
        return []
    cache_key = "adsb_school_" + ",".join(sorted(t["icao24_hex"] for t in tracked))
    cached = None if force else _cache_get(conn, cache_key, CACHE_TTL_SECONDS)
    if cached is not None:
        return cached
    try:
        params = [("icao24", t["icao24_hex"]) for t in tracked]
        data = _get_json(OPENSKY_URL + "?" + urllib.parse.urlencode(params))
        states = {(s[0] or "").strip().lower(): _state_to_dict(s) for s in (data.get("states") or [])}
    except Exception:
        states = {}
    result = []
    for t in tracked:
        state = states.get(t["icao24_hex"])
        entry = _enrich(state, school_map, flight_map) if state else {
            "icao24": t["icao24_hex"], "lat": None, "lon": None, "altitude_ft": None, "speed_kt": None,
            "track_deg": None, "on_ground": None, "callsign": None, "distance_nm_from_n89": None,
            "is_school_plane": True, "asset_id": t["asset_id"], "plane_tag": t["plane_tag"],
        }
        flight = flight_map.get(t["asset_id"])
        if flight:
            entry["flight_active"] = True
            entry["started_at"] = flight["started_at"]
            entry["student_name"] = flight["student_name"]
            entry["cfi_name"] = flight["cfi_name"]
            entry["solo"] = bool(flight["solo"])
        result.append(entry)
    _cache_set(conn, cache_key, result)
    _record_track_points(conn, result)
    return result
