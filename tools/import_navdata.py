#!/usr/bin/env python3
"""Build instance/navdata.db (airports, navaids, fixes, airways) for the logbook.

  python3 tools/import_navdata.py --ourairports DIR [--nasr DIR]

DIR for --ourairports holds airports.csv and navaids.csv (davidmegginson.github.io/ourairports-data).
DIR for --nasr holds the FAA NASR "CSV Data" files (FIX_BASE.csv, NAV_BASE.csv, AWY_SEG.csv).
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


def import_nasr(conn, d):
    """FAA NASR CSV set. Column names are matched loosely; run with --nasr and read the counts."""
    fx = nv = aw = 0
    p = os.path.join(d, "FIX_BASE.csv")
    if os.path.exists(p):
        for r in _rows(p):
            lat, lon = _f(_pick(r, "LAT_DECIMAL")), _f(_pick(r, "LONG_DECIMAL"))
            if lat is None or lon is None:
                continue
            conn.execute("INSERT INTO fixes VALUES (?,?,?,?)", (_pick(r, "FIX_ID").upper(), _pick(r, "STATE_CODE"), lat, lon))
            fx += 1
    p = os.path.join(d, "NAV_BASE.csv")
    if os.path.exists(p):
        for r in _rows(p):
            lat, lon = _f(_pick(r, "LAT_DECIMAL")), _f(_pick(r, "LONG_DECIMAL"))
            if lat is None or lon is None:
                continue
            conn.execute("INSERT INTO navaids VALUES (?,?,?,?,?,?,?)",
                         (_pick(r, "NAV_ID").upper(), _pick(r, "NAV_TYPE"), _pick(r, "NAME"), _pick(r, "FREQ"), "US", lat, lon))
            nv += 1
    p = os.path.join(d, "AWY_SEG.csv")
    if os.path.exists(p):
        for r in _rows(p):
            seq = _f(_pick(r, "POINT_SEQ"))
            ident = _pick(r, "FROM_POINT", "SEG_VALUE").upper()
            if seq is None or not ident or not _pick(r, "AWY_ID"):
                continue
            conn.execute("INSERT INTO airway_pts VALUES (?,?,?,NULL,NULL)", (_pick(r, "AWY_ID").upper(), int(seq), ident))
            aw += 1
        # coordinates for airway points come from the fix / navaid tables
        conn.execute("""UPDATE airway_pts SET lat=(SELECT lat FROM fixes f WHERE f.ident=airway_pts.ident LIMIT 1),
                                              lon=(SELECT lon FROM fixes f WHERE f.ident=airway_pts.ident LIMIT 1)""")
        conn.execute("""UPDATE airway_pts SET lat=(SELECT lat FROM navaids n WHERE n.ident=airway_pts.ident LIMIT 1),
                                              lon=(SELECT lon FROM navaids n WHERE n.ident=airway_pts.ident LIMIT 1)
                        WHERE lat IS NULL""")
    return fx, nv, aw


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
        fx, nv, aw = import_nasr(conn, a.nasr)
        print(f"FAA fixes {fx}, navaids {nv}, airway points {aw}")
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
