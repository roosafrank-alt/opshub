"""Payroll: one place for admins to see what's owed each week to every shop
worker (clocked labor) and every CFI (dual flights x their pay rate), mark
each person paid, look back over past weeks, and download it all as a
spreadsheet (CSV).

Amounts are always worked out from the same data as the existing pay pages
(Labor Pay and a CFI's Pay page), so the numbers match. Marking someone paid
saves the amount and hours at that moment in payroll_payments; if their
hours change afterwards (a late flight entry, a fixed timer) the page shows
both figures so the difference can be settled.

A week runs Monday to Sunday. A person's week is flagged when it's far from
their usual: 50% above or below their average over the previous 8 weeks
they were paid for (needs at least 3 such weeks, and a difference of $50 or
more, so small wobbles and brand-new people aren't flagged).
"""
import csv
import io
from datetime import date, datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, flash, session, Response

from auth import master_admin_required
from db import get_db, now_iso

payroll_bp = Blueprint("payroll", __name__)

HISTORY_WEEKS = 12      # weeks listed in the history table
BASELINE_WEEKS = 8      # weeks looked back on to decide what's "usual"
BASELINE_MIN = 3        # need this many paid weeks before flagging
FLAG_RATIO = 0.5        # 50% above or below the usual amount
FLAG_MIN_DOLLARS = 50.0


def week_start_of(d):
    return d - timedelta(days=d.weekday())


def _parse_week(raw):
    try:
        d = datetime.strptime((raw or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        d = date.today()
    return week_start_of(d)


def _owed_by_week(conn, start, end):
    """{(person_type, person_id): {week_start_iso: {"hours", "amount"}}} for
    every week touching [start, end] (dates)."""
    from flight import _LOG_ROW_SQL, _row_with_cost  # late import: flight imports app-level helpers

    owed = {}

    def add(key, day_iso, hours, amount):
        try:
            d = datetime.strptime((day_iso or "")[:10], "%Y-%m-%d").date()
        except ValueError:
            return
        wk = week_start_of(d).isoformat()
        slot = owed.setdefault(key, {}).setdefault(wk, {"hours": 0.0, "amount": 0.0})
        slot["hours"] += hours or 0
        slot["amount"] += amount or 0

    for r in conn.execute("""SELECT laborer_id, started_at, hours, cost FROM labor_sessions
                             WHERE ended_at IS NOT NULL AND date(started_at) BETWEEN ? AND ?""",
                          (start.isoformat(), end.isoformat())).fetchall():
        add(("laborer", r["laborer_id"]), r["started_at"], r["hours"], r["cost"])

    rates = {r["id"]: (r["pay_rate_per_hour"] or 0) for r in conn.execute("SELECT id, pay_rate_per_hour FROM cfis")}
    for r in conn.execute(_LOG_ROW_SQL + " WHERE f.cfi_id IS NOT NULL AND f.solo = 0 AND f.flight_date BETWEEN ? AND ?",
                          (start.isoformat(), end.isoformat())).fetchall():
        d = _row_with_cost(r)
        hrs = (d.get("instructor_hours") or 0) + (d.get("ground_hours") or 0)
        add(("cfi", r["cfi_id"]), r["flight_date"], hrs, hrs * rates.get(r["cfi_id"], 0))
    return owed


def _people(conn):
    """Everyone who can be on payroll: shop workers, then CFIs (not the
    shared station logins)."""
    people = []
    for r in conn.execute("SELECT id, name, active FROM laborers ORDER BY name").fetchall():
        people.append({"type": "laborer", "id": r["id"], "name": r["name"], "active": bool(r["active"]),
                       "kind": "Shop"})
    for r in conn.execute("SELECT id, name, active, is_station FROM cfis ORDER BY name").fetchall():
        if r["is_station"]:
            continue
        people.append({"type": "cfi", "id": r["id"], "name": r["name"], "active": bool(r["active"]),
                       "kind": "CFI"})
    return people


def _payments(conn, first_week, last_week):
    rows = conn.execute("""SELECT * FROM payroll_payments WHERE week_start BETWEEN ? AND ?""",
                        (first_week.isoformat(), last_week.isoformat())).fetchall()
    return {(r["person_type"], r["person_id"], r["week_start"]): r for r in rows}


def _flag(amount, history_amounts):
    """A short note if `amount` is far from this person's usual, else None."""
    past = [a for a in history_amounts if a > 0]
    if len(past) < BASELINE_MIN:
        return None
    usual = sum(past) / len(past)
    if abs(amount - usual) < FLAG_MIN_DOLLARS:
        return None
    if amount > usual * (1 + FLAG_RATIO):
        return f"{amount / usual:.1f}x their usual ${usual:,.0f}"
    if amount < usual * (1 - FLAG_RATIO):
        return f"Well below their usual ${usual:,.0f}" if amount else f"Nothing this week (usually ${usual:,.0f})"
    return None


def build_payroll(conn, week):
    """Everything the Payroll page shows for the week starting `week`."""
    first = week - timedelta(weeks=max(HISTORY_WEEKS, BASELINE_WEEKS + 1))
    last_day = week + timedelta(days=6)
    owed = _owed_by_week(conn, first, last_day)
    paid = _payments(conn, first, week)
    wk = week.isoformat()

    rows = []
    for p in _people(conn):
        key = (p["type"], p["id"])
        by_week = owed.get(key, {})
        this = by_week.get(wk, {"hours": 0.0, "amount": 0.0})
        payment = paid.get((p["type"], p["id"], wk))
        if not p["active"] and not this["amount"] and not payment:
            continue
        history = [by_week.get((week - timedelta(weeks=i)).isoformat(), {}).get("amount", 0.0)
                   for i in range(1, BASELINE_WEEKS + 1)]
        row = dict(p, hours=this["hours"], amount=round(this["amount"], 2), payment=payment,
                   flag=_flag(this["amount"], history))
        row["changed_since_paid"] = bool(payment and abs((payment["amount"] or 0) - row["amount"]) >= 0.01)
        if row["amount"] or payment or p["active"]:
            rows.append(row)

    totals = {
        "owed": sum(r["amount"] for r in rows),
        "hours": sum(r["hours"] for r in rows),
        "paid": sum((r["payment"]["amount"] or 0) for r in rows if r["payment"]),
        "unpaid": sum(r["amount"] for r in rows if not r["payment"]),
        "unpaid_people": sum(1 for r in rows if r["amount"] and not r["payment"]),
        "flags": sum(1 for r in rows if r["flag"]),
    }

    history = []
    for i in range(HISTORY_WEEKS):
        w = week - timedelta(weeks=i)
        wi = w.isoformat()
        owed_w = sum(v.get(wi, {}).get("amount", 0.0) for v in owed.values())
        paid_w = sum((r["amount"] or 0) for k, r in paid.items() if k[2] == wi)
        unpaid_n = sum(1 for key, v in owed.items()
                       if v.get(wi, {}).get("amount", 0) > 0 and (key[0], key[1], wi) not in paid)
        history.append({"week": w, "end": w + timedelta(days=6), "owed": owed_w, "paid": paid_w,
                        "unpaid_people": unpaid_n})
    return rows, totals, history


@payroll_bp.route("/payroll")
@master_admin_required
def payroll_page():
    week = _parse_week(request.args.get("week"))
    conn = get_db()
    rows, totals, history = build_payroll(conn, week)
    conn.close()
    this_week = week_start_of(date.today())
    return render_template("payroll.html", week=week, week_end=week + timedelta(days=6), rows=rows,
                           totals=totals, history=history,
                           prev_week=(week - timedelta(weeks=1)).isoformat(),
                           next_week=(week + timedelta(weeks=1)).isoformat() if week < this_week else None,
                           this_week=this_week.isoformat(), flag_ratio=int(FLAG_RATIO * 100),
                           baseline_weeks=BASELINE_WEEKS)


def _person_ok(conn, person_type, person_id):
    table = {"laborer": "laborers", "cfi": "cfis"}.get(person_type)
    if not table or person_id is None:
        return False
    return conn.execute(f"SELECT id FROM {table} WHERE id = ?", (person_id,)).fetchone() is not None


@payroll_bp.route("/payroll/mark-paid", methods=["POST"])
@master_admin_required
def payroll_mark_paid():
    week = _parse_week(request.form.get("week"))
    person_type = request.form.get("person_type")
    try:
        person_id = int(request.form.get("person_id") or "")
    except ValueError:
        person_id = None
    note = (request.form.get("note") or "").strip()[:200] or None
    conn = get_db()
    if not _person_ok(conn, person_type, person_id):
        conn.close()
        flash("That person wasn't found.", "danger")
        return redirect(url_for("payroll.payroll_page", week=week.isoformat()))
    owed = _owed_by_week(conn, week, week + timedelta(days=6)).get((person_type, person_id), {})
    this = owed.get(week.isoformat(), {"hours": 0.0, "amount": 0.0})
    cur = conn.execute("""INSERT OR IGNORE INTO payroll_payments
                          (person_type, person_id, week_start, hours, amount, paid_at, paid_by, note)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                       (person_type, person_id, week.isoformat(), round(this["hours"], 2),
                        round(this["amount"], 2), now_iso(), session.get("user_name"), note))
    conn.commit()
    conn.close()
    if cur.rowcount:
        flash(f"Marked paid: ${this['amount']:,.2f}.", "success")
    else:
        flash("Already marked paid for that week.", "warning")
    return redirect(url_for("payroll.payroll_page", week=week.isoformat()))


@payroll_bp.route("/payroll/unmark-paid", methods=["POST"])
@master_admin_required
def payroll_unmark_paid():
    week = _parse_week(request.form.get("week"))
    person_type = request.form.get("person_type")
    try:
        person_id = int(request.form.get("person_id") or "")
    except ValueError:
        person_id = None
    conn = get_db()
    n = conn.execute("DELETE FROM payroll_payments WHERE person_type = ? AND person_id = ? AND week_start = ?",
                     (person_type, person_id, week.isoformat())).rowcount
    conn.commit()
    conn.close()
    flash("Marked as not paid." if n else "That week wasn't marked paid.", "success" if n else "warning")
    return redirect(url_for("payroll.payroll_page", week=week.isoformat()))


def _csv_response(rows, filename):
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@payroll_bp.route("/payroll/export")
@master_admin_required
def payroll_export():
    """The selected week as a spreadsheet - one row per person."""
    week = _parse_week(request.args.get("week"))
    conn = get_db()
    rows, totals, _history = build_payroll(conn, week)
    conn.close()
    out = [["Week of", "Name", "Type", "Hours", "Owed", "Paid", "Paid amount", "Paid on", "Paid by", "Note", "Flag"]]
    for r in rows:
        p = r["payment"]
        out.append([week.isoformat(), r["name"], r["kind"], f"{r['hours']:.2f}", f"{r['amount']:.2f}",
                    "Yes" if p else "No", f"{p['amount']:.2f}" if p else "", (p["paid_at"] or "")[:10] if p else "",
                    (p["paid_by"] or "") if p else "", (p["note"] or "") if p else "", r["flag"] or ""])
    out.append(["", "Total", "", f"{totals['hours']:.2f}", f"{totals['owed']:.2f}", "", f"{totals['paid']:.2f}",
                "", "", "", ""])
    return _csv_response(out, f"payroll_week_{week.isoformat()}.csv")


@payroll_bp.route("/payroll/history.csv")
@master_admin_required
def payroll_history_export():
    """Every person x every week for the past year, with paid status - the
    running record in one spreadsheet."""
    this_week = week_start_of(date.today())
    first = this_week - timedelta(weeks=51)
    conn = get_db()
    owed = _owed_by_week(conn, first, this_week + timedelta(days=6))
    paid = _payments(conn, first, this_week)
    people = {(p["type"], p["id"]): p for p in _people(conn)}
    conn.close()
    out = [["Week of", "Name", "Type", "Hours", "Owed", "Paid", "Paid amount", "Paid on"]]
    for i in range(52):
        w = (this_week - timedelta(weeks=i)).isoformat()
        for key, p in people.items():
            v = owed.get(key, {}).get(w)
            pay = paid.get((key[0], key[1], w))
            if not v and not pay:
                continue
            v = v or {"hours": 0.0, "amount": 0.0}
            out.append([w, p["name"], p["kind"], f"{v['hours']:.2f}", f"{v['amount']:.2f}",
                        "Yes" if pay else "No", f"{pay['amount']:.2f}" if pay else "",
                        (pay["paid_at"] or "")[:10] if pay else ""])
    return _csv_response(out, f"payroll_history_{this_week.isoformat()}.csv")
