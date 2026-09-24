"""Flight Academy leaderboard: points, achievements and rankings for every
student, built from two sources:

1. The school's own logged flights (flights table): hours (Hobbs, else
   Tach), solo hours, day/night landings, number of flights.
2. What students log for themselves on the Academy page (academy_entries):
   distance flown, extra landings, cross-country flights, night and
   instrument time - things the flight log doesn't capture.

Points are a simple, visible formula (POINT_RULES) plus a bonus for each
achievement unlocked (ACHIEVEMENTS) and for the student's certificate and
ratings (the badge from flight.pilot_badge_info). Everything is computed on
the fly - nothing is stored except the students' own entries.
"""
from datetime import date

from flight import _flight_hours, _FINISHED_FLIGHT_SQL, PILOT_CERTIFICATES, _pilot_ratings_list

# What students can log themselves: (code, label, unit, points per unit)
ENTRY_KINDS = [
    ("distance_nm", "Distance flown", "nm", 0.1),
    ("landings", "Landings", "landings", 1),
    ("night_landings", "Night landings", "landings", 3),
    ("xc_flights", "Cross-country flights", "flights", 15),
    ("night_hours", "Night hours", "hrs", 10),
    ("instrument_hours", "Instrument hours (actual/sim)", "hrs", 10),
]
ENTRY_KIND_MAP = {k[0]: k for k in ENTRY_KINDS}

# From the flight log: stat -> points each
POINT_RULES = [
    ("hours", "Flight hours", 10),
    ("solo_hours", "Solo hours (bonus)", 5),
    ("day_landings", "Day landings", 1),
    ("night_landings_log", "Night landings", 3),
]

# Certificate / rating bonus (the badge from Students > Edit)
CERT_POINTS = {"student": 0, "sport": 100, "recreational": 120, "private": 200, "commercial": 300, "atp": 500}
RATING_POINTS = 50

# (code, name, icon, description, stat, threshold, points)
ACHIEVEMENTS = [
    ("first_flight", "First Flight", "bi-airplane", "Log your first flight", "flights", 1, 10),
    ("first_solo", "First Solo", "bi-person-fill-up", "Fly your first solo", "solo_flights", 1, 50),
    ("hours_10", "10 Hours", "bi-clock", "10 hours in the logbook", "hours", 10, 25),
    ("hours_50", "50 Hours", "bi-clock-fill", "50 hours in the logbook", "hours", 50, 75),
    ("hours_100", "Century", "bi-trophy", "100 hours in the logbook", "hours", 100, 150),
    ("landings_100", "Greaser", "bi-arrow-down-circle", "100 landings", "all_landings", 100, 50),
    ("night_owl", "Night Owl", "bi-moon-stars", "10 night landings", "all_night_landings", 10, 40),
    ("xc_1", "Cross-Country", "bi-signpost-split", "Your first cross-country", "xc_flights", 1, 40),
    ("nm_1000", "Road Trip", "bi-geo-alt", "1,000 nm flown", "distance_nm", 1000, 60),
    ("ifr_10", "In the Soup", "bi-cloud-fog2", "10 instrument hours", "instrument_hours", 10, 40),
]

LEADERBOARDS = [
    ("points", "Points", "pts"),
    ("hours", "Hours", "hrs"),
    ("all_landings", "Landings", ""),
    ("distance_nm", "Distance", "nm"),
    ("achievement_count", "Achievements", ""),
]


def period_start(period):
    """First day (YYYY-MM-DD) counted for 'month' / 'year', else None (all time)."""
    today = date.today()
    if period == "month":
        return today.replace(day=1).isoformat()
    if period == "year":
        return today.replace(month=1, day=1).isoformat()
    return None


def student_stats(conn, since=None):
    """{student_id: stats dict} for every active, non-station student."""
    students = conn.execute("""SELECT * FROM students WHERE active = 1 AND COALESCE(is_station, 0) = 0
                               ORDER BY name""").fetchall()
    stats = {}
    for s in students:
        stats[s["id"]] = {"student": s, "id": s["id"], "name": s["name"], "flights": 0, "solo_flights": 0,
                          "hours": 0.0, "solo_hours": 0.0, "day_landings": 0, "night_landings_log": 0,
                          **{k[0]: 0.0 for k in ENTRY_KINDS}}
    sql = f"SELECT f.* FROM flights f WHERE {_FINISHED_FLIGHT_SQL}"
    params = []
    if since:
        sql += " AND f.flight_date >= ?"
        params.append(since)
    for f in conn.execute(sql, params).fetchall():
        st = stats.get(f["student_id"])
        if not st:
            continue
        h = _flight_hours(f)
        st["flights"] += 1
        st["hours"] += h
        if f["solo"]:
            st["solo_flights"] += 1
            st["solo_hours"] += h
        st["day_landings"] += (f["day_landings_fs"] or 0) + (f["day_landings_tg"] or 0)
        st["night_landings_log"] += (f["night_landings_fs"] or 0) + (f["night_landings_tg"] or 0)
    sql = "SELECT student_id, kind, SUM(value) v FROM academy_entries"
    params = []
    if since:
        sql += " WHERE entry_date >= ?"
        params.append(since)
    sql += " GROUP BY student_id, kind"
    for r in conn.execute(sql, params).fetchall():
        st = stats.get(r["student_id"])
        if st and r["kind"] in ENTRY_KIND_MAP:
            st[r["kind"]] += r["v"] or 0
    for st in stats.values():
        st["all_landings"] = int(st["day_landings"] + st["night_landings_log"] + st["landings"] + st["night_landings"])
        st["all_night_landings"] = int(st["night_landings_log"] + st["night_landings"])
        _score(st)
    return stats


def _score(st):
    """Fills in points (with a breakdown), achievements and cert bonus."""
    breakdown = []
    total = 0.0
    for key, label, per in POINT_RULES:
        pts = st[key] * per
        if pts:
            breakdown.append((label, pts))
        total += pts
    for code, label, unit, per in ENTRY_KINDS:
        pts = st[code] * per
        if pts:
            breakdown.append((label + " (logged)", pts))
        total += pts
    unlocked, locked = [], []
    for code, name, icon, desc, stat, threshold, pts in ACHIEVEMENTS:
        have = st.get(stat, 0) or 0
        a = {"code": code, "name": name, "icon": icon, "desc": desc, "points": pts,
             "progress": min(100, round(have / threshold * 100)) if threshold else 100,
             "have": have, "threshold": threshold}
        if have >= threshold:
            unlocked.append(a)
            total += pts
        else:
            locked.append(a)
    if unlocked:
        breakdown.append(("Achievements", sum(a["points"] for a in unlocked)))
    s = st["student"]
    cert = s["pilot_certificate"] if "pilot_certificate" in s.keys() else None
    cert_pts = CERT_POINTS.get(cert, 0) + RATING_POINTS * len(_pilot_ratings_list(s))
    if cert_pts:
        breakdown.append(("Certificate & ratings", cert_pts))
        total += cert_pts
    st["points"] = round(total)
    st["breakdown"] = breakdown
    st["achievements"] = unlocked
    st["locked"] = locked
    st["achievement_count"] = len(unlocked)


def leaderboards(stats, top=10):
    """{board_key: [(rank, stats), ...]} - ties share a rank; students with
    nothing on that board are left off it."""
    out = {}
    for key, label, unit in LEADERBOARDS:
        rows = sorted((st for st in stats.values() if st.get(key)), key=lambda st: -st[key])
        ranked, last, rank = [], None, 0
        for i, st in enumerate(rows):
            if st[key] != last:
                rank = i + 1
                last = st[key]
            ranked.append((rank, st))
        out[key] = ranked[:top]
    return out


def rank_of(stats, student_id, key="points"):
    """This student's rank on a board (1 = top), or None."""
    me = stats.get(student_id)
    if not me or not me.get(key):
        return None
    return 1 + sum(1 for st in stats.values() if (st.get(key) or 0) > me[key])
