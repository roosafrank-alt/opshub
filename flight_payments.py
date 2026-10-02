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


def apply_funds_to_flights(conn, student_id, amount, flight_ids, method=None, note=None, created_by=None):
    """Add Funds, applied to flights: ``amount`` dollars came in; the
    flights in ``flight_ids`` (this student's, unpaid) are each marked paid
    with their own payment line, and whatever is left over goes on as a
    plain Funds Added line. The ledger therefore rises by exactly
    ``amount`` - never more.

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
    labels = []
    for f in wanted:
        r = record_flight_payment(conn, f["id"], method=method, note=note, created_by=created_by)
        labels.append(r["label"])
    remainder = round(amount - needed, 2)
    if remainder > 0.005:
        fl._ledger_entry(conn, student_id, "funds_added", remainder, note=note, created_by=created_by)
    return {"ok": True, "error": None, "paid": labels, "applied": needed, "remainder": remainder}
