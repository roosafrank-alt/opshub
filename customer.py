"""Customer portal: a completely separate, scoped-down login for aircraft
owners (see auth.py's customer_* helpers and schema.sql's customers /
customer_assets tables). A customer sees only the aircraft they're linked
to - never the shop's full inventory, other customers' planes, or anything
staff-only. From here they can:
  - see maintenance reminders due/overdue on their plane(s)
  - log a current Hobbs/tach reading
  - see upcoming appointments (projects with a scheduled_date) and either
    confirm one or ask to reschedule it (which just flags it for admin -
    see customer_reschedule_requested_at/_note on projects; the appointment
    itself doesn't move until admin actually reschedules it)
  - see a running cost summary (parts + labor) for their open/recent jobs

Admin manages customer accounts and which aircraft they're linked to from
the regular Manage menu (see /customers routes in app.py).
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort

from db import get_db, now_iso, asset_meter, maintenance_status
from auth import (authenticate_customer, log_in_customer, log_out_customer,
                   current_customer, customer_login_required)

customer_bp = Blueprint("customer", __name__, url_prefix="/portal")


def _group_usage_by_section(usage_rows):
    """Same grouping app.py's project invoice/export use - named sections
    first (alphabetical), untagged "General" last. Kept as its own copy
    here rather than imported from app.py, which would create a circular
    import (app.py registers this blueprint)."""
    grouped = {}
    for row in usage_rows:
        label = row["section"] or "General"
        grouped.setdefault(label, []).append(row)
    return dict(sorted(grouped.items(), key=lambda kv: (kv[0] == "General", kv[0])))


def _owned_asset_ids(conn, customer_id):
    return [r["asset_id"] for r in conn.execute(
        "SELECT asset_id FROM customer_assets WHERE customer_id = ?", (customer_id,)).fetchall()]


def _require_owned_asset(conn, customer_id, asset_id):
    """Returns the asset row if it belongs to this customer, else aborts
    404 - a plain 404 (not 403) so a guessed/mistyped id doesn't confirm
    whether that asset id exists at all."""
    owned = _owned_asset_ids(conn, customer_id)
    if asset_id not in owned:
        abort(404)
    asset = conn.execute("SELECT * FROM assets WHERE id = ? AND deleted_at IS NULL", (asset_id,)).fetchone()
    if not asset:
        abort(404)
    return asset


def _project_bill(conn, project_id):
    """Same parts+labor computation as app.py's project_invoice_csv, minus
    the CSV formatting - returns (grouped parts-by-section, labor rows,
    grand_total)."""
    usage = conn.execute("""
        SELECT p.name, p.unit, p.sell_price, t.section as section,
               SUM(CASE WHEN t.type='out' THEN t.qty ELSE -t.qty END) as qty_used
        FROM transactions t JOIN parts p ON p.id = t.part_id
        WHERE t.project_id = ?
        GROUP BY p.id, t.section
        HAVING qty_used > 0
        ORDER BY p.name
    """, (project_id,)).fetchall()
    labor = conn.execute("""
        SELECT ls.*, l.name as laborer_name
        FROM labor_sessions ls JOIN laborers l ON l.id = ls.laborer_id
        WHERE ls.project_id = ? AND ls.ended_at IS NOT NULL
        ORDER BY ls.started_at
    """, (project_id,)).fetchall()
    grouped = _group_usage_by_section(usage)
    grand_total = sum((u["qty_used"] or 0) * (u["sell_price"] or 0) for rows in grouped.values() for u in rows)
    grand_total += sum(s["cost"] or 0 for s in labor)
    return grouped, labor, round(grand_total, 2)


@customer_bp.route("/login", methods=["GET", "POST"])
def customer_login():
    if request.method == "POST":
        email = request.form.get("email", "")
        password = request.form.get("password", "")
        row = authenticate_customer(email, password)
        if not row:
            flash("Incorrect email or password.", "danger")
            return render_template("customer_login.html", email=email)
        log_in_customer(row)
        return redirect(url_for("customer.customer_dashboard"))
    return render_template("customer_login.html", email="")


@customer_bp.route("/logout")
def customer_logout():
    log_out_customer()
    return redirect(url_for("customer.customer_login"))


@customer_bp.route("/")
@customer_login_required
def customer_dashboard():
    conn = get_db()
    cust = current_customer(conn)
    if not cust:
        conn.close()
        return redirect(url_for("customer.customer_login"))
    asset_ids = _owned_asset_ids(conn, cust["id"])
    assets = []
    if asset_ids:
        ph = ",".join("?" * len(asset_ids))
        assets = conn.execute(
            f"SELECT * FROM assets WHERE id IN ({ph}) AND deleted_at IS NULL ORDER BY tag", asset_ids).fetchall()
    # A quick per-plane summary: how many reminders are due/overdue, how
    # many upcoming appointments need a response.
    summaries = []
    for a in assets:
        items = conn.execute(
            "SELECT * FROM maintenance_items WHERE asset_id = ? AND active = 1", (a["id"],)).fetchall()
        due_count = sum(1 for m in items
                         if maintenance_status(m, asset_meter(a, m["hour_type"]))["urgency"] in ("overdue", "due_soon"))
        upcoming_count = conn.execute("""
            SELECT COUNT(*) c FROM projects WHERE asset_id = ? AND deleted_at IS NULL
                AND scheduled_date IS NOT NULL AND status NOT IN ('completed', 'archived')
                AND customer_confirmed_at IS NULL AND customer_reschedule_requested_at IS NULL
        """, (a["id"],)).fetchone()["c"]
        summaries.append({"asset": a, "due_count": due_count, "needs_response_count": upcoming_count})
    conn.close()
    # A single-plane owner is dropped straight into their plane instead of
    # a list-of-one; anyone with more than one plane picks first.
    if len(summaries) == 1:
        return redirect(url_for("customer.customer_asset", asset_id=summaries[0]["asset"]["id"]))
    return render_template("customer_dashboard.html", customer=cust, summaries=summaries)


@customer_bp.route("/aircraft/<int:asset_id>")
@customer_login_required
def customer_asset(asset_id):
    conn = get_db()
    cust = current_customer(conn)
    asset = _require_owned_asset(conn, cust["id"], asset_id)
    multi_plane = len(_owned_asset_ids(conn, cust["id"])) > 1

    items = conn.execute(
        "SELECT * FROM maintenance_items WHERE asset_id = ? AND active = 1 ORDER BY name", (asset_id,)).fetchall()
    reminders = [{"item": m, "status": maintenance_status(m, asset_meter(asset, m["hour_type"]))} for m in items]
    reminders.sort(key=lambda r: {"overdue": 0, "due_soon": 1, "ok": 2, "unknown": 3}.get(r["status"]["urgency"], 4))

    appointments = conn.execute("""
        SELECT * FROM projects WHERE asset_id = ? AND deleted_at IS NULL AND scheduled_date IS NOT NULL
              AND status NOT IN ('completed', 'archived')
        ORDER BY scheduled_date
    """, (asset_id,)).fetchall()

    jobs = conn.execute("""
        SELECT * FROM projects WHERE asset_id = ? AND deleted_at IS NULL
        ORDER BY (status = 'active') DESC, created_at DESC LIMIT 10
    """, (asset_id,)).fetchall()
    bills = []
    for j in jobs:
        grouped, labor, total = _project_bill(conn, j["id"])
        if total > 0 or grouped or labor:
            bills.append({"project": j, "grouped": grouped, "labor": labor, "total": total})
    conn.close()
    return render_template("customer_asset.html", customer=cust, asset=asset, reminders=reminders,
                            appointments=appointments, bills=bills, multi_plane=multi_plane)


@customer_bp.route("/aircraft/<int:asset_id>/hours", methods=["POST"])
@customer_login_required
def customer_update_hours(asset_id):
    conn = get_db()
    cust = current_customer(conn)
    _require_owned_asset(conn, cust["id"], asset_id)
    def _parse_float(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    hobbs = _parse_float(request.form.get("hobbs_hours"))
    tach = _parse_float(request.form.get("tach_hours"))
    if hobbs is None and tach is None:
        flash("Enter a Hobbs and/or Tach reading.", "danger")
        conn.close()
        return redirect(url_for("customer.customer_asset", asset_id=asset_id))
    if hobbs is not None:
        conn.execute("UPDATE assets SET hobbs_hours = ?, hobbs_updated_at = ?, updated_at = ? WHERE id = ?",
                     (hobbs, now_iso(), now_iso(), asset_id))
    if tach is not None:
        conn.execute("UPDATE assets SET tach_hours = ?, tach_updated_at = ?, updated_at = ? WHERE id = ?",
                     (tach, now_iso(), now_iso(), asset_id))
    conn.commit()
    conn.close()
    flash("Reading logged - thanks!", "success")
    return redirect(url_for("customer.customer_asset", asset_id=asset_id))


@customer_bp.route("/project/<int:project_id>/confirm", methods=["POST"])
@customer_login_required
def customer_confirm_appointment(project_id):
    conn = get_db()
    cust = current_customer(conn)
    project = conn.execute("SELECT * FROM projects WHERE id = ? AND deleted_at IS NULL", (project_id,)).fetchone()
    if not project or project["asset_id"] not in _owned_asset_ids(conn, cust["id"]):
        conn.close()
        abort(404)
    conn.execute("""UPDATE projects SET customer_confirmed_at = ?,
                     customer_reschedule_requested_at = NULL, customer_reschedule_note = NULL WHERE id = ?""",
                 (now_iso(), project_id))
    conn.commit()
    conn.close()
    flash("Confirmed - see you then!", "success")
    return redirect(url_for("customer.customer_asset", asset_id=project["asset_id"]))


@customer_bp.route("/project/<int:project_id>/reschedule", methods=["POST"])
@customer_login_required
def customer_request_reschedule(project_id):
    conn = get_db()
    cust = current_customer(conn)
    project = conn.execute("SELECT * FROM projects WHERE id = ? AND deleted_at IS NULL", (project_id,)).fetchone()
    if not project or project["asset_id"] not in _owned_asset_ids(conn, cust["id"]):
        conn.close()
        abort(404)
    note = request.form.get("note", "").strip()
    if not note:
        flash("Let us know why / what works better, so we can rebook it right.", "danger")
        conn.close()
        return redirect(url_for("customer.customer_asset", asset_id=project["asset_id"]))
    conn.execute("""UPDATE projects SET customer_reschedule_requested_at = ?, customer_reschedule_note = ?,
                     customer_confirmed_at = NULL WHERE id = ?""",
                 (now_iso(), note, project_id))
    conn.commit()
    conn.close()
    flash("Got it - we'll be in touch to reschedule.", "success")
    return redirect(url_for("customer.customer_asset", asset_id=project["asset_id"]))
