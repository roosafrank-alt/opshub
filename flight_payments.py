"""Fly with Kate! - one shared way to record a payment against a flight.

SCHOOL-01: OpsHub tracks what a student owes two ways - the per-flight
``flights.paid`` tick (Billing, My Account, Flight History) and the
``student_ledger`` / ``students.balance`` running total (Add Funds, the
balance hold, the Account page). Before this module the two never talked:
"Mark Paid" only flipped the tick, so a student who had paid for three
flights still showed "$450.00 owed" everywhere and the Billing Cross-Check
page lit up.

Every place that marks a flight paid or unpaid goes through here so the two
systems always agree:

* ``record_flight_payment``  - Billing's Mark Paid, the per-flight Mark paid
  link on Flight History / Flight Detail (FLY-11), Pay with Card, Wave
  saying an invoice was paid, and "Mark N selected paid".
* ``reverse_flight_payment`` - Mark Unpaid (reverses only the ledger lines
  this flight's own payment put there).
* ``apply_funds_to_flights`` - Add Funds with "apply to these unpaid
  flights" ticked: the money added is split into one payment line per
  flight (each marked paid) plus a plain Funds Added line for what's left,
  so the ledger still adds up to exactly what was added.

Idempotent: marking an already-paid flight paid again records nothing, and
the amount recorded is always "what this flight still needs" (its total
minus any payment lines already tied to it, minus credit applied when it
was logged), so nothing is ever counted twice.

The ledger entry types used here are ``payment`` (positive, tied to a
flight_id - the same type End Session / Log a Past Session already use for
money collected on the spot) and ``payment_reversal`` (negative, tied to the
flight_id, written by Mark Unpaid). ``flight_payments_net(conn, flight_id)``
sums both for one flight.

Imports from flight.py happen lazily inside the functions: flight.py imports
this module at the top, so a top-level import here would be circular.
"""


def _flight():
    import flight  # noqa: WPS433 - lazy, see module docstring
    return flight


def flight_payments_net(conn, flight_id):
    """Net money recorded against this flight so far (payments minus
    reversals), in dollars."""
    row = conn.execute("""SELECT COALESCE(SUM(amount), 0) AS t FROM student_ledger
                          WHERE flight_id = ? AND entry_type IN ('payment', 'payment_reversal')""",
                       (flight_id,)).fetchone()
    return round(float(row["t"] or 0.0), 2)


def flight_label(f):
    """'10/02/2026 N123 ($250.00)' - what the flash messages and ledger
    notes call a flight. ``f`` is a dict from flight._row_with_cost."""
    fl = _flight()
    return f"{fl._us_date(f['flight_date'])} {f['plane_tag']} (${f['total']:.2f})"


def load_flight(conn, flight_id):
    """The flight as Billing sees it (costs worked out), or None."""
    fl = _flight()
    row = conn.execute(fl._LOG_ROW_SQL + " WHERE f.id = ?", (flight_id,)).fetchone()
    return fl._row_with_cost(row) if row else None


def amount_due(conn, f):
    """What this flight still needs to be fully paid: its total minus any
    payment lines already tied to it and minus credit applied to it when
    it was logged. Never negative."""
    credit = float(f.get("credit_applied") or 0.0)
    return round(max(0.0, f["total"] - flight_payments_net(conn, f["id"]) - credit), 2)


def record_flight_payment(conn, flight_id, method=None, note=None, created_by=None, amount=None):
    """Mark one flight paid AND put the matching payment on the student's
    ledger, in one step.

    Args:
        conn: open sqlite connection (caller commits).
        flight_id: flights.id
        method: how it was paid ("Cash", "Card", "Check", "Venmo/Zelle",
            "Wave", "Other" or None when nobody said).
        note: extra text for the ledger line (e.g. "simulated charge ending
            in 4242"). The line always starts with "Paid - <date> <plane>".
        created_by: user name for the ledger line.
        amount: money collected for this flight right now. Default (None)
            is everything the flight still needs (see amount_due); anything
            above that is clamped so the flight is never over-credited. Pass
            0 to mark it paid with no new ledger line (e.g. the money was
            already added as funds and sits in the balance).

    Returns a dict:
        {"ok": bool, "already_paid": bool, "flight": <cost dict or None>,
         "recorded": dollars actually written to the ledger,
         "label": "10/02/2026 N123 ($250.00)", "student_name": ...}
    ``ok`` is False only when the flight does not exist. Calling this on a
    flight already marked paid changes nothing (already_paid=True).
    """
    fl = _flight()
    f = load_flight(conn, flight_id)
    if not f:
        return {"ok": False, "already_paid": False, "flight": None, "recorded": 0.0, "label": "", "student_name": ""}
    result = {"ok": True, "already_paid": bool(f["paid"]), "flight": f, "recorded": 0.0,
              "label": flight_label(f), "student_name": f["student_name"]}
    if f["paid"]:
        clear_unneeded_charge_requests(conn, flight_id=flight_id)
        return result
    # The flight's own charge has to be on the ledger before a payment for
    # it makes sense (guarded inside - never deducts twice). A flight still
    # in the air isn't charged yet - End Session does that.
    if f.get("ended_at") or not f.get("started_at"):
        fl._deduct_flight_cost(conn, f, created_by=created_by)
    due = amount_due(conn, f)
    pay = due if amount is None else round(max(0.0, min(float(amount), due)), 2)
    if pay > 0.005:
        text = f"Paid - {fl._us_date(f['flight_date'])} {f['plane_tag']}"
        if method:
            text += f" ({method})"
        if note:
            text += f" - {note}"
        fl._ledger_entry(conn, f["student_id"], "payment", pay, note=text, flight_id=flight_id, created_by=created_by)
        result["recorded"] = pay
    conn.execute("""UPDATE flights SET paid = 1, payment_method = COALESCE(?, payment_method),
                    payment_amount = COALESCE(payment_amount, 0) + ? WHERE id = ?""",
                 (method, pay, flight_id))
    clear_unneeded_charge_requests(conn, flight_id=flight_id)
    return result


def reverse_flight_payment(conn, flight_id, created_by=None):
    """Mark one flight unpaid and take back ONLY the payment lines that
    were recorded against this flight (a negative ``payment_reversal``
    line, so the account history still shows what happened). Credit that
    was applied from the student's existing balance when the flight was
    logged is left as it was - that money was never "collected", so there
    is nothing to hand back.

    Returns the same dict shape as record_flight_payment, with
    ``already_unpaid`` instead of ``already_paid`` and ``recorded`` the
    (positive) dollars reversed.
    """
    fl = _flight()
    f = load_flight(conn, flight_id)
    if not f:
        return {"ok": False, "already_unpaid": False, "flight": None, "recorded": 0.0, "label": "", "student_name": ""}
    result = {"ok": True, "already_unpaid": not f["paid"], "flight": f, "recorded": 0.0,
              "label": flight_label(f), "student_name": f["student_name"]}
    if not f["paid"]:
        return result
    net = flight_payments_net(conn, flight_id)
    if net > 0.005:
        text = f"Mark Unpaid - {fl._us_date(f['flight_date'])} {f['plane_tag']}"
        fl._ledger_entry(conn, f["student_id"], "payment_reversal", -net, note=text, flight_id=flight_id,
                         created_by=created_by)
        result["recorded"] = net
    conn.execute("""UPDATE flights SET paid = 0, payment_method = NULL, payment_amount = NULL,
                    card_last4 = NULL, card_charge_id = NULL WHERE id = ?""", (flight_id,))
    return result


def unpaid_flights_for_student(conn, student_id):
    """The student's unpaid flights, oldest first, each with ``due`` (what
    it still needs) - what Add Funds offers to apply money to."""
    fl = _flight()
    rows = conn.execute(fl._LOG_ROW_SQL + " WHERE f.student_id = ? AND f.paid = 0 ORDER BY f.flight_date, f.id",
                        (student_id,)).fetchall()
    out = []
    for r in rows:
        f = fl._row_with_cost(r)
        f["due"] = amount_due(conn, f)
        if f["due"] > 0.005:
            out.append(f)
    return out


def apply_funds_to_flights(conn, student_id, amount, flight_ids, method=None, note=None, created_by=None,
                           can_confirm=True, user_id=None, ledger_note=None):
    """Add Funds, applied to flights: ``amount`` dollars came in; the
    flights in ``flight_ids`` (this student's, unpaid) are each marked paid
    with their own payment line, and whatever is left over goes on as a
    plain Funds Added line. The ledger therefore rises by exactly
    ``amount`` - never more.

    Cash/check taken by someone who is not a master admin / Flight Admin
    (``can_confirm`` False) is paid as usual and also logged for office
    verification (``logged_ids``).

    Returns {"ok": bool, "error": str or None, "paid": [labels...],
    "applied": dollars put on flights, "remainder": dollars left as funds}.
    Refuses (ok=False, nothing written) when the ticked flights need more
    than ``amount``, or one of them is not this student's unpaid flight.
    """
    fl = _flight()
    amount = round(float(amount or 0), 2)
    wanted = []
    for fid in flight_ids:
        f = load_flight(conn, fid)
        if not f or f["student_id"] != student_id:
            return {"ok": False, "error": "One of the ticked flights isn't this student's.", "paid": [], "applied": 0.0, "remainder": 0.0}
        if f["paid"]:
            continue  # already paid meanwhile - nothing to apply
        f["due"] = amount_due(conn, f)
        wanted.append(f)
    needed = round(sum(f["due"] for f in wanted), 2)
    if needed > amount + 0.005:
        return {"ok": False,
                "error": f"The flights you ticked come to ${needed:,.2f} but you're adding ${amount:,.2f} - untick some or add more.",
                "paid": [], "applied": 0.0, "remainder": 0.0}
    labels, logged_ids = [], []
    if wanted:
        # The whole prepayment gets its own line first ($0.00 change: the money
        # reaches the balance through the flight payments and the credit left
        # over below), so the history shows the full amount that was received.
        how = ledger_note if ledger_note is not None else note
        summary = f"Prepayment received ${amount:,.2f}" + (f" ({how})" if how else "")
        summary += f": ${needed:,.2f} paid past-due flights"
        if amount - needed > 0.005:
            summary += f", ${amount - needed:,.2f} kept as credit"
        fl._ledger_entry(conn, student_id, "payment_received", 0.0, note=summary + ".", created_by=created_by)
    for f in wanted:
        r = record_and_log(conn, f["id"], method=method, note=note, created_by=created_by,
                           can_confirm=can_confirm, user_id=user_id)
        labels.append(r["label"])
        if r.get("confirmation_id"):
            logged_ids.append(r["confirmation_id"])
    remainder = round(amount - needed, 2)
    if remainder > 0.005:
        fl._ledger_entry(conn, student_id, "funds_added", remainder, note=ledger_note if ledger_note is not None else note, created_by=created_by)
    return {"ok": True, "error": None, "paid": labels, "logged_ids": logged_ids, "applied": needed, "remainder": remainder}


# ---------------------------------------------------------------------------
# Cash / check collected by staff: a verification log
# ---------------------------------------------------------------------------
# A CFI (anyone who is not a master admin / Flight Admin) can mark a flight
# Paid right away when they collect cash or a check - it posts exactly as
# any other payment. Each such payment also gets a row in
# flight_payment_confirmations so an admin can cross-check that the office
# really received the money. status: 'pending' = collected, not yet verified
# at the office; 'verified' = "Received at office"; 'missing' = flagged "Not
# received" (the payment is NOT reversed - the admin decides what to do).
# Nothing here ever posts or reverses a payment.

CONFIRM_METHODS = ("cash", "check")


def needs_confirmation(method, can_confirm):
    """True when this payment should be logged for office verification:
    Cash or Check recorded by someone who is not a master admin / Flight Admin."""
    return (not can_confirm) and (method or "").strip().lower() in CONFIRM_METHODS


def log_collected(conn, flight_id, method, amount, note=None, collected_by=None, user_id=None):
    """Adds the verification row (status 'pending') for money just collected.
    Returns its id, or None when nothing was collected. Doesn't commit."""
    fl = _flight()
    amount = round(float(amount or 0), 2)
    if amount <= 0.005:
        return None
    cur = conn.execute("""INSERT INTO flight_payment_confirmations
                          (flight_id, amount, method, note, recorded_by, recorded_by_user_id, recorded_at, status)
                          VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
                       (flight_id, amount, method, note, collected_by, user_id, fl.now_iso()))
    fl._log_field_change(conn, "flight_payment_confirmation", cur.lastrowid, "status", "",
                         f"collected, not yet verified (${amount:,.2f}{' ' + method if method else ''})", collected_by)
    return cur.lastrowid


def verification_map(conn):
    """{flight_id: 'missing' | 'pending' | 'verified'} for the small badge shown to admins/billers."""
    out = {}
    rank = {"verified": 1, "pending": 2, "missing": 3}
    for r in conn.execute("SELECT flight_id, status FROM flight_payment_confirmations").fetchall():
        if rank.get(r["status"], 0) > rank.get(out.get(r["flight_id"]), 0):
            out[r["flight_id"]] = r["status"]
    return out


def pending_confirmation_count(conn):
    """Collected but not yet verified at the office (the red count)."""
    return conn.execute("SELECT COUNT(*) AS c FROM flight_payment_confirmations WHERE status = 'pending'").fetchone()["c"]


def record_and_log(conn, flight_id, method=None, note=None, created_by=None, amount=None, can_confirm=True, user_id=None):
    """record_flight_payment, plus a verification row when a non-admin took
    cash/check. The flight is Paid immediately either way. The result gets
    ``confirmation_id`` (the new row's id or None) and ``error`` (None)."""
    r = record_flight_payment(conn, flight_id, method=method, note=note, created_by=created_by, amount=amount)
    r["error"] = None
    r["confirmation_id"] = None
    if r["ok"] and not r["already_paid"] and needs_confirmation(method, can_confirm):
        r["confirmation_id"] = log_collected(conn, flight_id, method, r["recorded"], note, created_by, user_id)
    return r


def verify_received(conn, conf_id, who):
    """Admin taps "Received at office". Marks the entry verified (also clears a
    'missing' flag). Never posts a payment. Returns True if it changed."""
    fl = _flight()
    old = conn.execute("SELECT status FROM flight_payment_confirmations WHERE id = ?", (conf_id,)).fetchone()
    n = conn.execute("UPDATE flight_payment_confirmations SET status = 'verified', decided_by = ?, decided_at = ? "
                     "WHERE id = ? AND status IN ('pending', 'missing')", (who, fl.now_iso(), conf_id)).rowcount
    if n == 1:
        fl._log_field_change(conn, "flight_payment_confirmation", conf_id, "status", old["status"], "verified", who)
    return n == 1


def flag_missing(conn, conf_id, who, note):
    """Admin taps "Not received" (with a note): flags the entry. The payment
    stays posted. Returns the row dict, or None if it was already handled."""
    fl = _flight()
    n = conn.execute("UPDATE flight_payment_confirmations SET status = 'missing', decided_by = ?, decided_at = ?, "
                     "reject_reason = ? WHERE id = ? AND status = 'pending'", (who, fl.now_iso(), note, conf_id)).rowcount
    if n != 1:
        return None
    fl._log_field_change(conn, "flight_payment_confirmation", conf_id, "status", "pending", "missing: " + note, who)
    return dict(conn.execute("SELECT * FROM flight_payment_confirmations WHERE id = ?", (conf_id,)).fetchone())


# ---------------------------------------------------------------------------
# Wave card requests that no longer need doing
# ---------------------------------------------------------------------------

def clear_unneeded_charge_requests(conn, flight_id=None, who="OpsHub"):
    """A pending card request whose flight has since been paid some other way,
    has nothing due, or is already on a Wave invoice clears itself: status
    'not_needed' (note kept in decline_reason), logged in field_change_log.
    Run whenever the queue / badge / count is worked out and right after a
    flight is marked paid. Doesn't commit. Returns how many were cleared."""
    fl = _flight()
    sql = """SELECT r.id, r.flight_id, f.paid, f.wave_invoice_id FROM flight_charge_requests r
             JOIN flights f ON f.id = r.flight_id WHERE r.status = 'pending'"""
    params = ()
    if flight_id is not None:
        sql += " AND r.flight_id = ?"
        params = (flight_id,)
    cleared = 0
    for r in conn.execute(sql, params).fetchall():
        if r["paid"]:
            why = "Flight was paid another way"
        elif r["wave_invoice_id"]:
            why = "Flight is already on a Wave invoice"
        else:
            f = load_flight(conn, r["flight_id"])
            if not f or amount_due(conn, f) > 0.005:
                continue
            why = "Nothing is due on this flight any more"
        n = conn.execute("UPDATE flight_charge_requests SET status = 'not_needed', decided_by = ?, decided_at = ?, "
                         "decline_reason = ? WHERE id = ? AND status = 'pending'", (who, fl.now_iso(), why, r["id"])).rowcount
        if n == 1:
            fl._log_field_change(conn, "flight_charge_request", r["id"], "status", "pending", "not_needed: " + why, who)
            cleared += 1
    return cleared
