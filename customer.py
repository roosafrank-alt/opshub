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
from datetime import date, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, session
from werkzeug.security import generate_password_hash

from db import get_db, now_iso, gen_project_code, asset_meter, maintenance_status, found_item_messages, allowed_image, save_upload
from auth import log_out_user, current_customer, customer_login_required

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


def _project_discrepancy_descriptions(conn, project_id):
    """Each discrepancy's owner-facing write-up for this job, for the "Job
    Costs" box - only ones the shop actually wrote a Description for (its
    internal Notes never show here, or anywhere else in the portal - see
    project_section_notes in app.py)."""
    return conn.execute(
        "SELECT name, description FROM project_sections "
        "WHERE project_id = ? AND description IS NOT NULL AND TRIM(description) != '' ORDER BY name",
        (project_id,)).fetchall()


@customer_bp.route("/login", methods=["GET", "POST"])
def customer_login():
    """SEAM-4: there is one login page, at '/'. The old My Aircraft login
    address just sends people there (owners log in with their email)."""
    return redirect(url_for("home_launcher"))


@customer_bp.route("/logout")
def customer_logout():
    """SEAM-5: one Log Out everywhere - says Logged out. and lands on the
    one login page, same as the staff logout."""
    log_out_user()
    flash("Logged out.", "success")
    return redirect(url_for("home_launcher"))


@customer_bp.route("/account", methods=["GET", "POST"])
@customer_login_required
def customer_account():
    """SEAM-7: a small My Account page for aircraft owners - name, email,
    phone and a new password (8+ characters, HUB-27). Someone who also has
    a staff login changes those on the shared My Account page instead."""
    if session.get("user_id"):
        return redirect(url_for("account_page", **{"from": "owner"}))
    conn = get_db()
    cust = current_customer(conn)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip() or None
        new_password = request.form.get("password", "")
        confirm = request.form.get("password_confirm", "")
        shown = request.form.get("password_shown") == "1"
        from app import password_problem  # late import: app registers this blueprint
        error = None
        if not name or not email:
            error = "Name and email are both required."
        elif conn.execute("SELECT id FROM customers WHERE lower(email) = ? AND id != ?",
                          (email.lower(), cust["id"])).fetchone():
            error = "That email is already used by another account."
        else:
            error = password_problem(new_password, confirm, shown)
        if error:
            flash(error, "danger")
            conn.close()
            return render_template("customer_account.html", customer=cust, name=name, email=email, phone=phone or "")
        if new_password:
            conn.execute("UPDATE customers SET name=?, email=?, phone=?, password_hash=?, password_plain=? WHERE id=?",
                         (name, email, phone, generate_password_hash(new_password, method="pbkdf2:sha256"),
                          new_password, cust["id"]))
        else:
            conn.execute("UPDATE customers SET name=?, email=?, phone=? WHERE id=?", (name, email, phone, cust["id"]))
        conn.commit()
        conn.close()
        session["customer_name"] = name
        flash("Your account has been updated.", "success")
        return redirect(url_for("customer.customer_account"))
    conn.close()
    return render_template("customer_account.html", customer=cust, name=cust["name"], email=cust["email"],
                           phone=cust["phone"] or "")


@customer_bp.route("/tour/seen", methods=["POST"])
@customer_login_required
def customer_tour_seen():
    """SEAM-19: the owner portal's three-stop walkthrough, remembered per
    customer account once finished or skipped."""
    conn = get_db()
    conn.execute("UPDATE customers SET tour_seen = 1 WHERE id = ?", (session["customer_id"],))
    conn.commit()
    conn.close()
    return ("", 204)


@customer_bp.route("/")
@customer_login_required
def customer_dashboard():
    conn = get_db()
    cust = current_customer(conn)
    if not cust:
        conn.close()
        return redirect(url_for("home_launcher"))
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
        SELECT * FROM projects WHERE asset_id = ? AND deleted_at IS NULL
              AND (scheduled_date IS NOT NULL OR customer_requested_week IS NOT NULL)
              AND status NOT IN ('completed', 'archived')
        ORDER BY (scheduled_date IS NULL) DESC, scheduled_date
    """, (asset_id,)).fetchall()
    # reminders already asked for (waiting on the shop to set a date)
    requested = {a["customer_requested_item_id"]: a["customer_requested_week"] for a in appointments
                 if a["scheduled_date"] is None and a["customer_requested_item_id"]}
    for r in reminders:
        r["requested_week"] = requested.get(r["item"]["id"])

    jobs = conn.execute("""
        SELECT * FROM projects WHERE asset_id = ? AND deleted_at IS NULL
        ORDER BY (status = 'active') DESC, created_at DESC LIMIT 10
    """, (asset_id,)).fetchall()
    bills = []
    for j in jobs:
        grouped, labor, total = _project_bill(conn, j["id"])
        discrepancies = _project_discrepancy_descriptions(conn, j["id"])
        if total > 0 or grouped or labor or discrepancies:
            bills.append({"project": j, "grouped": grouped, "labor": labor, "total": total,
                          "discrepancies": discrepancies})
    found_items = _found_items_for_asset(conn, asset_id)
    conn.close()
    return render_template("customer_asset.html", customer=cust, asset=asset, reminders=reminders,
                            appointments=appointments, bills=bills, multi_plane=multi_plane,
                            found_items=found_items, week_options=_week_options())


def _week_options(n=6):
    """The next n weeks as (Monday ISO date, label), starting next week."""
    today = date.today()
    monday = today - timedelta(days=today.weekday()) + timedelta(days=7)
    return [((monday + timedelta(weeks=i)).isoformat(), (monday + timedelta(weeks=i)).strftime("Week of %b %-d"))
            for i in range(n)]


@customer_bp.route("/aircraft/<int:asset_id>/book/<int:item_id>", methods=["POST"])
@customer_login_required
def customer_book_item(asset_id, item_id):
    """Owner taps "Book it" on a due reminder: makes a ready-made job on that
    plane (no date yet) carrying the week they'd like. The shop sets the date;
    until then it shows under Appointments as Requested."""
    conn = get_db()
    cust = current_customer(conn)
    asset = _require_owned_asset(conn, cust["id"], asset_id)
    item = conn.execute("SELECT * FROM maintenance_items WHERE id = ? AND asset_id = ? AND active = 1",
                        (item_id, asset_id)).fetchone()
    if not item:
        conn.close()
        abort(404)
    week = request.form.get("week", "")
    if week not in [w for w, _ in _week_options()]:
        week = _week_options()[0][0]
    dup = conn.execute("""SELECT 1 FROM projects WHERE asset_id = ? AND deleted_at IS NULL AND scheduled_date IS NULL
                          AND customer_requested_item_id = ? AND status NOT IN ('completed', 'archived')""",
                       (asset_id, item_id)).fetchone()
    if dup:
        conn.close()
        flash("Already requested - the shop will set the date.", "info")
        return redirect(url_for("customer.customer_asset", asset_id=asset_id))
    label = dict(_week_options())[week]
    status = maintenance_status(item, asset_meter(asset, item["hour_type"]))
    desc = f"Requested by owner {cust['name']} from My Aircraft ({status['label']}). Preferred: {label}."
    conn.execute("""INSERT INTO projects (code, name, description, status, asset_id, created_at,
                    customer_requested_week, customer_requested_item_id) VALUES (?, ?, ?, 'active', ?, ?, ?, ?)""",
                 (gen_project_code(conn), item["name"], desc, asset_id, now_iso(), week, item_id))
    conn.commit()
    conn.close()
    flash(f"Requested - {label}. The shop will confirm the date.", "success")
    return redirect(url_for("customer.customer_asset", asset_id=asset_id))


def _found_items_for_asset(conn, asset_id):
    """Extra work the shop found on this plane's jobs: waiting ones first
    (they need the owner's OK), then the last few answered ones."""
    rows = conn.execute("""
        SELECT fi.*, p.name as project_name FROM found_items fi
        JOIN projects p ON p.id = fi.project_id
        WHERE p.asset_id = ? AND p.deleted_at IS NULL
        ORDER BY (fi.status = 'waiting') DESC, COALESCE(fi.decided_at, fi.created_at) DESC LIMIT 20
    """, (asset_id,)).fetchall()
    items = []
    for r in rows:
        it = dict(r)
        it["photos"] = [p["filename"] for p in conn.execute(
            "SELECT filename FROM photos WHERE found_item_id = ? ORDER BY id", (r["id"],)).fetchall()]
        it["messages"] = found_item_messages(conn, r["id"])
        items.append(it)
    return items


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
    updated_by = f"Owner - {cust['name']}"
    if hobbs is not None:
        conn.execute("UPDATE assets SET hobbs_hours = ?, hobbs_updated_at = ?, hobbs_updated_by = ?, updated_at = ? WHERE id = ?",
                     (hobbs, now_iso(), updated_by, now_iso(), asset_id))
    if tach is not None:
        conn.execute("UPDATE assets SET tach_hours = ?, tach_updated_at = ?, tach_updated_by = ?, updated_at = ? WHERE id = ?",
                     (tach, now_iso(), updated_by, now_iso(), asset_id))
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


@customer_bp.route("/found-item/<int:item_id>/decide", methods=["POST"])
@customer_login_required
def customer_found_item_decide(item_id):
    """Owner approves or declines extra work the shop found (see the
    found_item_* routes in app.py). Approved items become a Sub Area on the
    job so the work and parts land there; either way the answer, who gave
    it and when are kept on the record."""
    conn = get_db()
    cust = current_customer(conn)
    item = conn.execute("""SELECT fi.*, p.asset_id FROM found_items fi JOIN projects p ON p.id = fi.project_id
                           WHERE fi.id = ? AND p.deleted_at IS NULL""", (item_id,)).fetchone()
    if not item or item["asset_id"] not in _owned_asset_ids(conn, cust["id"]):
        conn.close()
        abort(404)
    decision = request.form.get("decision")
    note = (request.form.get("note") or "").strip()[:500] or None
    if decision not in ("approve", "decline"):
        conn.close()
        abort(400)
    # Declining requires a note, so the shop knows why - approving doesn't
    # (the item's own description already says what it's for).
    if decision == "decline" and not note:
        conn.close()
        flash("Let the shop know why you're declining, so it's on record.", "danger")
        return redirect(url_for("customer.customer_asset", asset_id=item["asset_id"]))
    # An approved item already became a Sub Area and work may be underway,
    # so that decision is final. A declined item can be reconsidered -
    # re-deciding it (either way) just overwrites the old answer.
    if item["status"] == "approved":
        conn.close()
        flash("You already approved this one, and it's on the job.", "warning")
        return redirect(url_for("customer.customer_asset", asset_id=item["asset_id"]))
    section = None
    if decision == "approve":
        section = (item["description"].split("\n")[0].strip() or f"Found item {item_id}")[:60]
        conn.execute("INSERT OR IGNORE INTO project_sections (project_id, name, created_at) VALUES (?, ?, ?)",
                     (item["project_id"], section, now_iso()))
    claimed = conn.execute("""UPDATE found_items SET status = ?, decided_at = ?, decided_by = ?, decision_note = ?,
                              section_name = ? WHERE id = ? AND status IN ('waiting', 'declined')""",
                           ("approved" if decision == "approve" else "declined", now_iso(), cust["name"], note,
                            section, item_id)).rowcount
    if claimed and decision == "approve":
        # HUB-43 (Frank): the approval is final and is logged, time-stamped,
        # on the job - as a note in the item's thread, which the shop sees
        # on the project page alongside decided_at/decided_by above.
        conn.execute("""INSERT INTO found_item_messages (found_item_id, author_type, author_name, body, created_at)
                        VALUES (?, 'owner', ?, ?, ?)""",
                     (item_id, cust["name"], f"Approved this work (about ${(item['est_total'] or 0):,.2f}).", now_iso()))
    conn.commit()
    conn.close()
    if claimed:
        flash("Approved - thanks, we'll get it done." if decision == "approve" else
              "Declined - thanks for letting us know. It's noted on the job.", "success")
    return redirect(url_for("customer.customer_asset", asset_id=item["asset_id"]))


@customer_bp.route("/found-item/<int:item_id>/message", methods=["POST"])
@customer_login_required
def customer_found_item_message(item_id):
    """The owner's side of the back-and-forth on one found item - a note
    (with an optional photo) added to its conversation thread, visible to
    the shop on the project page. Works whatever the item's status is, so
    an owner can ask a question or explain a decline even after answering."""
    conn = get_db()
    cust = current_customer(conn)
    item = conn.execute("""SELECT fi.*, p.asset_id FROM found_items fi JOIN projects p ON p.id = fi.project_id
                           WHERE fi.id = ? AND p.deleted_at IS NULL""", (item_id,)).fetchone()
    if not item or item["asset_id"] not in _owned_asset_ids(conn, cust["id"]):
        conn.close()
        abort(404)
    body = (request.form.get("body") or "").strip()[:1000]
    if not body:
        conn.close()
        flash("Type a note before sending.", "danger")
        return redirect(url_for("customer.customer_asset", asset_id=item["asset_id"]) + "#found-items")
    cur = conn.execute("""INSERT INTO found_item_messages (found_item_id, author_type, author_name, body, created_at)
                          VALUES (?, 'owner', ?, ?, ?)""",
                       (item_id, cust["name"], body, now_iso()))
    conn.commit()
    saved = 0
    try:
        for f in request.files.getlist("photos"):
            if f and f.filename and allowed_image(f.filename):
                stored_name = save_upload(f)
                conn.execute("INSERT INTO photos (found_item_message_id, filename, created_at) VALUES (?, ?, ?)",
                             (cur.lastrowid, stored_name, now_iso()))
                saved += 1
        conn.commit()
    except OSError:
        conn.rollback()
        flash("Note sent, but a photo couldn't be saved.", "warning")
    conn.close()
    flash("Note sent.", "success")
    return redirect(url_for("customer.customer_asset", asset_id=item["asset_id"]) + "#found-items")
