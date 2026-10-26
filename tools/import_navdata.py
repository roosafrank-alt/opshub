#!/usr/bin/env python3
"""Build instance/navdata.db (airports, navaids, fixes, airways) for the logbook.

  python3 tools/import_navdata.py --ourairports DIR [--nasr DIR]

DIR for --ourairports holds airports.csv and navaids.csv (davidmegginson.github.io/ourairports-data).
DIR for --nasr holds the FAA NASR "CSV Data" files (APT_BASE.csv, FIX_BASE.csv, NAV_BASE.csv, AWY_SEG_ALT.csv).
Builds into a temp file and swaps it in only when it worked, so a bad import never
leaves the logbook without a database. Re-run every 28 days (NASR cycle).
"""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import navdb  # noqa: E402


def _rows(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            yield {(k or "").strip().upper(): (v or "").strip() for k, v in r.items()}


def _pick(r, *names):
    for n in names:
        if r.get(n):
            return r[n]
    return ""


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def import_ourairports(conn, d):
    n = 0
    for r in _rows(os.path.join(d, "airports.csv")):
        lat, lon = _f(r.get("LATITUDE_DEG")), _f(r.get("LONGITUDE_DEG"))
        if lat is None or lon is None or r.get("TYPE") == "closed":
            continue
        conn.execute("INSERT OR REPLACE INTO airports VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (r["IDENT"].upper(), (r.get("ICAO_CODE") or "").upper() or None, (r.get("IATA_CODE") or "").upper() or None,
                      (r.get("LOCAL_CODE") or "").upper() or None, r.get("NAME"), r.get("MUNICIPALITY"),
                      r.get("ISO_REGION"), r.get("ISO_COUNTRY"), r.get("TYPE"), lat, lon,
                      int(_f(r.get("ELEVATION_FT")) or 0)))
        n += 1
    m = 0
    p = os.path.join(d, "navaids.csv")
    if os.path.exists(p):
        for r in _rows(p):
            lat, lon = _f(r.get("LATITUDE_DEG")), _f(r.get("LONGITUDE_DEG"))
            if lat is None or lon is None or not r.get("IDENT"):
                continue
            conn.execute("INSERT INTO navaids VALUES (?,?,?,?,?,?,?)",
                         (r["IDENT"].upper(), r.get("TYPE"), r.get("NAME"), r.get("FREQUENCY_KHZ"), r.get("ISO_COUNTRY"), lat, lon))
            m += 1
    return n, m


US_SET = ("US", "PR", "GU", "VI", "AS", "MP")   # places the FAA list is the authority for
_ABBR = {"RGNL": "Regional", "INTL": "International", "MUNI": "Municipal", "ARPT": "Airport", "FLD": "Field",
         "CO": "County", "MEML": "Memorial", "NATL": "National", "AAF": "AAF", "AFB": "AFB", "ANGB": "ANGB",
         "NAS": "NAS", "NAF": "NAF", "HELIPORT": "Heliport", "STOLPORT": "STOLport"}


def _nice(name):
    out = []
    for w in (name or "").split():
        u = w.upper()
        if u in _ABBR:
            out.append(_ABBR[u])
        else:
            out.append("-".join(x.capitalize() for x in w.split("-")))
    return " ".join(out)


_SYN = {"rgnl": "regional", "intl": "international", "muni": "municipal", "arpt": "airport", "fld": "field",
        "meml": "memorial", "natl": "national", "co": "county", "afb": "afb"}


def _toks(name):
    import re
    return {_SYN.get(t, t) for t in re.findall(r"[a-z0-9]+", (name or "").lower()) if t not in ("airport", "the", "of", "and", "field", "airfield")}


def _best_name(faa_name, oa_name):
    """OurAirports names read better (John F. Kennedy International Airport), but can be an old name
    (Dutchess County Airport for what the FAA now calls Hudson Valley Regional). Keep the OurAirports
    name when it clearly describes the same place, else use the FAA's."""
    f, o = _toks(faa_name), _toks(oa_name)
    if oa_name and f and len(f & o) / len(f) >= 0.6:
        return oa_name
    return _nice(faa_name)


def import_nasr(conn, d):
    """FAA NASR 28-day CSV set (APT_BASE, FIX_BASE, NAV_BASE, AWY_SEG_ALT).
    The FAA list replaces OurAirports for US airports (current codes and names) and for US navaids."""
    import math
    ap = fx = nv = aw = 0
    p = os.path.join(d, "APT_BASE.csv")
    if os.path.exists(p):
        iata, oa_name = {}, {}
        for r in conn.execute("SELECT icao, local, ident, iata, name FROM airports WHERE country IN (%s)" % ",".join("?" * len(US_SET)), US_SET):
            for k in (r["icao"], r["local"], r["ident"]):
                if k:
                    oa_name.setdefault(k, r["name"])
                    if r["iata"]:
                        iata[k] = r["iata"]
        conn.execute("DELETE FROM airports WHERE country IN (%s)" % ",".join("?" * len(US_SET)), US_SET)
        for r in _rows(p):
            if r.get("COUNTRY_CODE") not in US_SET or r.get("ARPT_STATUS") != "O":
                continue
            lat, lon = _f(r.get("LAT_DECIMAL")), _f(r.get("LONG_DECIMAL"))
            lid, icao = r.get("ARPT_ID", "").upper(), (r.get("ICAO_ID") or "").upper()
            if lat is None or lon is None or not lid:
                continue
            kind = {"H": "heliport", "S": "seaplane_base", "B": "balloonport"}.get(r.get("SITE_TYPE_CODE"), "small_airport")
            if kind == "small_airport" and r.get("FAR_139_TYPE_CODE"):
                kind = "medium_airport"
            ident = icao or lid
            conn.execute("INSERT OR REPLACE INTO airports VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (ident, icao or None, iata.get(icao) or iata.get(lid), lid,
                          _best_name(r.get("ARPT_NAME"), oa_name.get(icao) or oa_name.get(lid)),
                          _nice(r.get("CITY")), "US-" + r.get("STATE_CODE", "") if r.get("COUNTRY_CODE") == "US" else r.get("STATE_CODE"),
                          r.get("COUNTRY_CODE"), kind, lat, lon, int(_f(r.get("ELEV")) or 0)))
            ap += 1
    p = os.path.join(d, "FIX_BASE.csv")
    if os.path.exists(p):
        for r in _rows(p):
            lat, lon = _f(r.get("LAT_DECIMAL")), _f(r.get("LONG_DECIMAL"))
            if lat is None or lon is None or not r.get("FIX_ID"):
                continue
            conn.execute("INSERT INTO fixes VALUES (?,?,?,?)", (r["FIX_ID"].upper(), r.get("STATE_CODE"), lat, lon))
            fx += 1
    p = os.path.join(d, "NAV_BASE.csv")
    if os.path.exists(p):
        conn.execute("DELETE FROM navaids WHERE country IN ('US','PR','GU','VI','AS','MP')")
        for r in _rows(p):
            lat, lon = _f(r.get("LAT_DECIMAL")), _f(r.get("LONG_DECIMAL"))
            if lat is None or lon is None or not r.get("NAV_ID"):
                continue
            conn.execute("INSERT INTO navaids VALUES (?,?,?,?,?,?,?)",
                         (r["NAV_ID"].upper(), r.get("NAV_TYPE"), _nice(r.get("NAME")), r.get("FREQ"), r.get("COUNTRY_CODE") or "US", lat, lon))
            nv += 1
    p = os.path.join(d, "AWY_SEG_ALT.csv")
    if os.path.exists(p):
        pts = {}
        for ident, lat, lon, kind in [(r["ident"], r["lat"], r["lon"], "fix") for r in conn.execute("SELECT ident, lat, lon FROM fixes")] + \
                                     [(r["ident"], r["lat"], r["lon"], "nav") for r in conn.execute("SELECT ident, lat, lon FROM navaids")]:
            pts.setdefault(ident, []).append((lat, lon, kind))
        groups = {}
        for r in _rows(p):
            seq = _f(r.get("POINT_SEQ"))
            if seq is None or not r.get("AWY_ID") or not r.get("FROM_POINT"):
                continue
            groups.setdefault((r["AWY_ID"].upper(), r.get("AWY_LOCATION") or ""), []).append((seq, r["FROM_POINT"].upper(), (r.get("FROM_PT_TYPE") or "").strip(), (r.get("TO_POINT") or "").upper()))

        def near(c, prev):
            if not c:
                return None
            if prev is None:
                return c[0]
            return min(c, key=lambda t: math.hypot(t[0] - prev[0], (t[1] - prev[1]) * math.cos(math.radians(prev[0]))))
        for (awy, loc), segs in groups.items():
            segs.sort()
            names = [x[1] for x in segs] + ([segs[-1][3]] if segs[-1][3] else [])   # the last segment's end point
            prev = None
            for i, nm in enumerate(names):
                hit = near(pts.get(nm), prev)
                if hit:
                    prev = (hit[0], hit[1])
                conn.execute("INSERT INTO airway_pts VALUES (?,?,?,?,?,?)", (awy, loc, i, nm, hit[0] if hit else None, hit[1] if hit else None))
                aw += 1
    return ap, fx, nv, aw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ourairports", required=True)
    ap.add_argument("--nasr")
    ap.add_argument("--out", default=navdb.NAV_DB_PATH)
    a = ap.parse_args()
    tmp = a.out + ".new"
    if os.path.exists(tmp):
        os.remove(tmp)
    conn = navdb.connect(tmp)
    n, m = import_ourairports(conn, a.ourairports)
    print(f"airports {n}, navaids {m}")
    if a.nasr:
        ap2, fx, nv, aw = import_nasr(conn, a.nasr)
        print(f"FAA airports {ap2}, fixes {fx}, navaids {nv}, airway points {aw}")
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('built', datetime('now'))")
    conn.commit()
    conn.close()
    if n < 1000:
        print("Too few airports - keeping the old database.")
        os.remove(tmp)
        sys.exit(1)
    os.replace(tmp, a.out)
    print("Done:", a.out)


if __name__ == "__main__":
    main()
