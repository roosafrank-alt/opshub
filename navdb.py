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
    lat REAL NOT NULL, lon REAL NOT NULL, elev INTEGER
);
CREATE TABLE IF NOT EXISTS navaids (
    ident TEXT NOT NULL, type TEXT, name TEXT, freq TEXT, country TEXT,
    lat REAL NOT NULL, lon REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS navaids_ident ON navaids(ident);
CREATE TABLE IF NOT EXISTS fixes (
    ident TEXT NOT NULL, state TEXT, lat REAL NOT NULL, lon REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS fixes_ident ON fixes(ident);
CREATE TABLE IF NOT EXISTS airway_pts (
    airway TEXT NOT NULL, seq INTEGER NOT NULL, ident TEXT NOT NULL, lat REAL, lon REAL
);
CREATE INDEX IF NOT EXISTS airway_pts_aw ON airway_pts(airway, seq);
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


def _norm(code):
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def _row_airport(r):
    # Show the code pilots use: the ICAO code if it has one (KPOU), else the FAA/local code (N89).
    # OurAirports' own id for a field without an ICAO code is a made-up K+local (KN89).
    return {"code": r["icao"] or r["local"] or r["ident"], "icao": r["icao"], "iata": r["iata"], "name": r["name"] or "",
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
    if len(q) >= 3:
        words = "%" + q.replace("%", "") + "%"
        for r in conn.execute("""SELECT * FROM airports WHERE (UPPER(name) LIKE ? OR UPPER(city) LIKE ?)
                                 AND type != 'closed' LIMIT 400""", (words, words)):
            rows.setdefault(r["ident"], (1, r))
    if not rows and qn == HOME_CODE[:len(qn)]:
        return [dict(HOME)]

    def key(item):
        tier, r = item
        visits = freq.get(r["ident"], 0) + freq.get(r["icao"] or "", 0) + freq.get(r["local"] or "", 0)
        d = nm_between(near[0], near[1], r["lat"], r["lon"]) if near else 0
        # exact code match always first, then visited airports, then nearest
        exact = 0 if qn in (r["icao"], r["local"]) else (1 if qn == r["ident"] else 2)   # (IATA codes like KPO = Pohang must not beat KPOU)
        return (tier, exact, 0 if visits else 1, -visits, _TYPE_RANK.get(r["type"], 6) if d == 0 else 0, d, r["ident"])

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
    pts = conn.execute("SELECT seq, ident, lat, lon FROM airway_pts WHERE airway=? ORDER BY seq", (awy,)).fetchall()
    names = [p["ident"] for p in pts]
    if a_ident not in names or b_ident not in names:
        return None
    i, j = names.index(a_ident), names.index(b_ident)
    seg = pts[i:j + 1] if i <= j else list(reversed(pts[j:i + 1]))
    return [p for p in seg]


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
                    points.append({"code": p["ident"], "name": "", "lat": lat, "lon": lon, "kind": "airway"})
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
    return {"points": clean, "nm": round(sum(legs), 1), "legs": legs, "straight": not toks and len(clean) == 2,
            "unresolved": unresolved}
