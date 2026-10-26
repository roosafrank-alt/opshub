"""Aviation navigation database for the Flight Academy logbook.

Lives in its own SQLite file (instance/navdata.db) so it can be rebuilt or swapped
on its own without touching the main database. Filled by tools/import_navdata.py
from OurAirports (airports + navaids, worldwide) and FAA NASR (fixes, navaids and
airways, US).

Provides: type-ahead search, code <-> name lookup, and route parsing with
great-circle distance in nautical miles.
"""
import math
import os
import re
import sqlite3

NAV_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "navdata.db")

HOME_CODE = "N89"
HOME = {"code": "N89", "name": "Joseph Y. Resnick (Ellenville)", "lat": 41.72778, "lon": -74.37722, "kind": "airport"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS airports (
    ident TEXT PRIMARY KEY,      -- code people type: KPOU, N89, CYUL
    icao TEXT, iata TEXT, local TEXT,
    name TEXT, city TEXT, region TEXT, country TEXT, type TEXT,
    lat REAL NOT NULL, lon REAL NOT NULL, elev INTEGER, tower INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS airports_pos ON airports(lat, lon);
CREATE TABLE IF NOT EXISTS navaids (
    ident TEXT NOT NULL, type TEXT, name TEXT, freq TEXT, country TEXT,
    lat REAL NOT NULL, lon REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS navaids_ident ON navaids(ident);
CREATE INDEX IF NOT EXISTS navaids_pos ON navaids(lat, lon);
CREATE TABLE IF NOT EXISTS fixes (
    ident TEXT NOT NULL, state TEXT, lat REAL NOT NULL, lon REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS fixes_ident ON fixes(ident);
CREATE INDEX IF NOT EXISTS fixes_pos ON fixes(lat, lon);
CREATE TABLE IF NOT EXISTS airway_pts (
    airway TEXT NOT NULL, loc TEXT, seq INTEGER NOT NULL, ident TEXT NOT NULL, lat REAL, lon REAL
);
CREATE INDEX IF NOT EXISTS airway_pts_aw ON airway_pts(airway, loc, seq);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

_AIRWAY = re.compile(r"^(?:[VJQTABGRLMNPWYZ]|UL|UN|UM|UA|UB|UG|UH)\d{1,4}[A-Z]?$")
_SPLIT = re.compile(r"[\s,.\-/>]+")
_SKIP = {"VIA", "DCT", "DIRECT", "TO", "AND", "THEN"}


def connect(path=None):
    p = path or NAV_DB_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


SHIPPED_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "navdata.db.gz")


def _unpack_shipped(p):
    """First run on a machine: unpack the database that ships in git (data/navdata.db.gz)
    into instance/. instance/ is never touched by deploys, so a rebuilt db there is kept."""
    import gzip
    import shutil
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".part"
    with gzip.open(SHIPPED_DB, "rb") as src, open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst)
    os.replace(tmp, p)


def available(path=None):
    p = path or NAV_DB_PATH
    if not os.path.exists(p) and p == NAV_DB_PATH and os.path.exists(SHIPPED_DB):
        try:
            _unpack_shipped(p)
        except OSError:
            return False
    if not os.path.exists(p):
        return False
    try:
        conn = connect(p)
        n = conn.execute("SELECT COUNT(*) FROM airports").fetchone()[0]
        conn.close()
        return n > 0
    except sqlite3.Error:
        return False


def nm_between(lat1, lon1, lat2, lon2):
    """Great-circle distance in nautical miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 3440.065 * math.asin(min(1.0, math.sqrt(a)))


def true_course(lat1, lon1, lat2, lon2):
    """Initial true course (degrees, 1-360) along the great circle from point 1 to point 2."""
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return int(round((math.degrees(math.atan2(y, x)) + 360) % 360)) or 360


def _norm(code):
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def display_code(r):
    """The code pilots use: ICAO if it has one, else the local FAA code. A K-prefixed code with a
    number in it (K20N) drops the K (20N), unless it is only 3 characters long already."""
    code = r["icao"] or r["local"] or r["ident"]
    if len(code) > 3 and code.startswith("K") and any(ch.isdigit() for ch in code):
        code = code[1:]
    return code


def _row_airport(r):
    # Show the code pilots use: the ICAO code if it has one (KPOU), else the FAA/local code (N89).
    # OurAirports' own id for a field without an ICAO code is a made-up K+local (KN89).
    return {"code": display_code(r), "tower": bool(r["tower"]), "elev": r["elev"], "type": r["type"], "icao": r["icao"], "iata": r["iata"], "name": r["name"] or "",
            "city": r["city"] or "", "region": r["region"] or "", "country": r["country"] or "",
            "lat": r["lat"], "lon": r["lon"], "kind": "airport"}


_TYPE_RANK = {"large_airport": 0, "medium_airport": 1, "small_airport": 2, "seaplane_base": 3, "balloonport": 4, "heliport": 5}


def _rank(r, code, near=None):
    """Lower = better. A code that is the airport's CURRENT code (ICAO / local) beats one that is only its
    old internal id; then bigger airports beat heliports; then nearest to `near`."""
    current = 0 if code in (r["icao"], r["local"]) else (1 if code == r["ident"] else 2)
    d = nm_between(near[0], near[1], r["lat"], r["lon"]) if near else 0
    return (current, _TYPE_RANK.get(r["type"], 6), d)


def lookup_airport(conn, code, near=None):
    """Airport by any of its codes (KPOU, POU, N89...). None if unknown.
    Several airports can share a small-field code in the source data; the best match wins
    (current code > old id, bigger airport > heliport, then nearest to `near`)."""
    c = _norm(code)
    if not c:
        return None
    cands = [c]
    if len(c) == 3 and c.isalpha():
        cands.append("K" + c)
    best = None
    for x in cands:
        rows = conn.execute("SELECT * FROM airports WHERE ident=? OR icao=? OR local=? OR iata=?", (x, x, x, x)).fetchall()
        if rows:
            best = min(rows, key=lambda r: _rank(r, x, near))
            break
    if best:
        return _row_airport(best)
    if c == HOME_CODE:
        return dict(HOME)
    return None


def search_airports(conn, q, limit=8, near=None, freq=None):
    """Type-ahead. Matches code prefixes first, then name/city words.
    Order: airports the student flies to a lot (most visits first), then the
    rest closest to `near` (lat, lon). `freq` = {code: visits}."""
    q = (q or "").strip().upper()
    if not q:
        return []
    qn = _norm(q)
    freq = freq or {}
    rows = {}
    if qn:
        like = qn + "%"
        for r in conn.execute("""SELECT * FROM airports WHERE (ident LIKE ? OR icao LIKE ? OR iata LIKE ? OR local LIKE ?)
                                 AND type != 'closed' LIMIT 400""", (like, like, like, like)):
            rows[r["ident"]] = (0, r)
    if len(q) >= 3:   # words in the name or city that START with what was typed (kin -> Kingston, not Jenkins)
        t = q.replace("%", "").replace("_", "")
        pats = [t + "%", "% " + t + "%", "%-" + t + "%", "%/" + t + "%"]
        cond = " OR ".join(["UPPER(name) LIKE ?", "UPPER(city) LIKE ?"][i % 2] for i in range(8))
        args = [x for pat in pats for x in (pat, pat)]
        for r in conn.execute(f"SELECT * FROM airports WHERE ({cond}) AND type != 'closed' LIMIT 6000", args):
            rows.setdefault(r["ident"], (1, r))
    if not rows and qn == HOME_CODE[:len(qn)]:
        return [dict(HOME)]

    def key(item):
        tier, r = item
        visits = freq.get(r["ident"], 0) + freq.get(r["icao"] or "", 0) + freq.get(r["local"] or "", 0)
        d = nm_between(near[0], near[1], r["lat"], r["lon"]) if near else 0
        # exact code match always first, then visited airports, then nearest
        exact = 0 if qn in (r["icao"], r["local"]) else (1 if qn == r["ident"] else 2)   # (IATA codes like KPO = Pohang must not beat KPOU)
        return (exact, 0 if visits else 1, -visits, 1 if r["type"] == "heliport" else 0, d, r["ident"])   # heliports after airports

    out = []
    for item in sorted(rows.values(), key=key)[:limit]:
        a = _row_airport(item[1])
        if near:
            a["nm"] = round(nm_between(near[0], near[1], a["lat"], a["lon"]))
        out.append(a)
    return out


def _find_point(conn, ident, near=None):
    """Airport, navaid or fix by identifier. Several can share a name (SAX is a New Jersey
    VOR and an airport in Mexico), so the one nearest `near` wins."""
    c = _norm(ident)
    if not c:
        return None
    cands = []
    ap = lookup_airport(conn, c, near)
    if ap:
        cands.append(ap)
    for r in conn.execute("SELECT ident, type, name, lat, lon FROM navaids WHERE ident=?", (c,)):
        cands.append({"code": r["ident"], "name": r["name"] or "", "lat": r["lat"], "lon": r["lon"], "kind": "navaid"})
    for r in conn.execute("SELECT ident, lat, lon FROM fixes WHERE ident=?", (c,)):
        cands.append({"code": r["ident"], "name": "", "lat": r["lat"], "lon": r["lon"], "kind": "fix"})
    if not cands:
        return None
    if near:
        cands.sort(key=lambda p: nm_between(near[0], near[1], p["lat"], p["lon"]))
    return cands[0]


def _airway_slice(conn, awy, a_ident, b_ident):
    """The points of airway `awy` from a_ident to b_ident (either direction), or None."""
    for loc in [r[0] for r in conn.execute("SELECT DISTINCT loc FROM airway_pts WHERE airway=?", (awy,))]:
        pts = conn.execute("SELECT seq, ident, lat, lon FROM airway_pts WHERE airway=? AND loc IS ? ORDER BY seq", (awy, loc)).fetchall()
        names = [p["ident"] for p in pts]
        if a_ident in names and b_ident in names:
            i, j = names.index(a_ident), names.index(b_ident)
            return list(pts[i:j + 1]) if i <= j else list(reversed(pts[j:i + 1]))
    return None


def parse_route(conn, dep, dest, route=""):
    """Resolve a flight into plotted points and a distance.
    No route text -> straight line dep -> dest. With a route: waypoints,
    navaids and airways (V1 / J80 / T200 ...) between them are followed.
    Returns {"points": [...], "nm": float, "straight": bool, "unresolved": [...], "legs": [nm...]}"""
    d0 = lookup_airport(conn, dep) if dep else None
    d1 = lookup_airport(conn, dest) if dest else None
    toks = [t for t in _SPLIT.split((route or "").upper()) if t and t not in _SKIP]
    unresolved = []
    points = []
    if d0:
        points.append(d0)
    ref = (d0["lat"], d0["lon"]) if d0 else None
    i = 0
    while i < len(toks):
        t = toks[i]
        if _AIRWAY.match(t) and points and i + 1 < len(toks):
            # airway: from the last point to the point named next
            prev = points[-1]["code"]
            nxt = toks[i + 1]
            seg = _airway_slice(conn, t, prev, nxt)
            if seg is None:
                unresolved.append(f"{t} (between {prev} and {nxt})")
            else:
                for p in seg[1:]:
                    lat, lon = p["lat"], p["lon"]
                    if lat is None:
                        f = _find_point(conn, p["ident"], (points[-1]["lat"], points[-1]["lon"]))
                        if not f:
                            continue
                        lat, lon = f["lat"], f["lon"]
                    points.append({"code": p["ident"], "name": "", "lat": lat, "lon": lon, "kind": "airway", "via": t})
                i += 2
                continue
            i += 1
            continue
        p = _find_point(conn, t, (points[-1]["lat"], points[-1]["lon"]) if points else ref)
        if p:
            points.append(p)
        else:
            unresolved.append(t)
        i += 1
    if d1 and (not points or points[-1]["code"] != d1["code"]):
        points.append(d1)
    # drop consecutive duplicates
    clean = []
    for p in points:
        if not clean or clean[-1]["code"] != p["code"]:
            clean.append(p)
    legs = [round(nm_between(a["lat"], a["lon"], b["lat"], b["lon"]), 1) for a, b in zip(clean, clean[1:])]
    info = [{"from": a["code"], "to": b["code"], "nm": n, "course": true_course(a["lat"], a["lon"], b["lat"], b["lon"]),
             "via": b.get("via")} for (a, b), n in zip(zip(clean, clean[1:]), legs)]
    return {"points": clean, "nm": round(sum(legs), 1), "legs": legs, "legs_info": info, "straight": not toks and len(clean) == 2,
            "unresolved": unresolved}


_VOR_TYPES = ("VOR", "VORTAC", "VOR/DME", "VOR-DME", "TACAN", "VORDME", "DVOR", "DVOR-DME")


def in_view(conn, south, west, north, east, zoom):
    """Symbols for the map: what is worth drawing at this zoom inside the box.
    Big airports first, then smaller ones, VORs, NDBs, and (zoomed in) waypoints."""
    out = {"airports": [], "navaids": [], "fixes": [], "airways": []}
    z = int(zoom)
    if z < 5:
        return out
    types = ["large_airport", "medium_airport"] if z < 9 else (["large_airport", "medium_airport", "small_airport", "seaplane_base"] if z < 12 else None)
    q = "SELECT * FROM airports WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?"
    args = [south, north, west, east]
    if types:
        q += " AND (type IN (%s) OR tower = 1)" % ",".join("?" * len(types))
        args += types
    q += " ORDER BY CASE type WHEN 'large_airport' THEN 0 WHEN 'medium_airport' THEN 1 ELSE 2 END, tower DESC LIMIT 400"
    for r in conn.execute(q, args):
        a = _row_airport(r)
        out["airports"].append({k: a[k] for k in ("code", "name", "city", "lat", "lon", "tower", "elev", "type")})
    if z >= 7:
        nq = "SELECT ident, type, name, freq, lat, lon FROM navaids WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?"
        if z < 9:
            nq += " AND (type LIKE 'VOR%' OR type LIKE 'TACAN%' OR type LIKE 'DVOR%')"
        for r in conn.execute(nq + " LIMIT 300", (south, north, west, east)):
            out["navaids"].append({"code": r["ident"], "name": r["name"] or "", "type": r["type"], "freq": r["freq"], "lat": r["lat"], "lon": r["lon"]})
    if z >= 8:   # airway segments with at least one end in view; low (V, T) and high (J, Q) are drawn differently
        for r in conn.execute("""SELECT a.airway, a.ident AS i1, a.lat AS la1, a.lon AS lo1, b.ident AS i2, b.lat AS la2, b.lon AS lo2
                                 FROM airway_pts a JOIN airway_pts b ON b.airway = a.airway AND b.loc IS a.loc AND b.seq = a.seq + 1
                                 WHERE a.lat IS NOT NULL AND b.lat IS NOT NULL
                                   AND ((a.lat BETWEEN ? AND ? AND a.lon BETWEEN ? AND ?) OR (b.lat BETWEEN ? AND ? AND b.lon BETWEEN ? AND ?))
                                 LIMIT 1500""", (south, north, west, east, south, north, west, east)):
            out["airways"].append({"id": r["airway"], "a": [r["la1"], r["lo1"]], "b": [r["la2"], r["lo2"]], "from": r["i1"], "to": r["i2"],
                                   "high": r["airway"][0] in "JQ"})
    if z >= 10:
        for r in conn.execute("SELECT ident, lat, lon FROM fixes WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? LIMIT 500", (south, north, west, east)):
            out["fixes"].append({"code": r["ident"], "lat": r["lat"], "lon": r["lon"]})
    return out
