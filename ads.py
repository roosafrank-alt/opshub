"""Airworthiness Directive (AD) compliance per aircraft (QA
feat-ad-compliance-tracker).

Each aircraft page gets an ADs card: add an AD by number with a short
subject and whether it's one-time, recurring by hours, recurring by date or
not applicable (with a reason). Compliance is recorded with date, tach,
method, who signed and an optional photo of the logbook entry.

A recurring AD gets a linked maintenance_items row (named "AD <number>"),
so it shows in the aircraft's Maintenance list, the Maintenance tile and
calendar and the daily reminders exactly like oil changes and 100-hours;
recording compliance here signs that item off too. A "terminated" method
ends the recurring requirement.

When a new Annual project is started on an aircraft, every open one-time AD
and every recurring AD is added to the project's Job Sheet list
(add_ads_to_annual_project, called from app.project_new).
"""
from datetime import date, datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, session

from auth import shop_role_required
from db import get_db, now_iso, maintenance_status, asset_meter, allowed_image, save_upload

ads_bp = Blueprint("ads", __name__)

AD_KINDS = [
    ("one_time", "One-time"),
    ("recurring_hours", "Recurring by hours"),
    ("recurring_date", "Recurring by date"),
    ("not_applicable", "Not applicable"),
]
AD_METHODS = [
    ("inspection", "Inspection"),
    ("part_replaced", "Part replaced"),
    ("terminated", "Terminating action"),
    ("other", "Other"),
]


def _float_or_none(raw, positive=False):
    try:
        v = float((raw or "").strip())
    except ValueError:
        return None
    if v != v or v in (float("inf"), float("-inf")) or v < 0 or (positive and v == 0):
        return None
    return v


def ads_for_asset(conn, asset):
    """Every AD on this aircraft with its latest compliance and status:
    status is one of open / complied / due_soon / overdue / ok / terminated / na."""
    ads = []
    for r in conn.execute("SELECT * FROM ads WHERE asset_id = ? AND active = 1 ORDER BY ad_number", (asset["id"],)).fetchall():
        a = dict(r)
        a["history"] = conn.execute("SELECT * FROM ad_compliance WHERE ad_id = ? ORDER BY complied_date DESC, id DESC",
                                    (r["id"],)).fetchall()
        last = a["history"][0] if a["history"] else None
        a["last"] = last
        a["next_due"] = None
        if r["kind"] == "not_applicable":
            a["status"], a["status_label"] = "na", "Not applicable"
        elif last and last["method"] == "terminated":
            a["status"], a["status_label"] = "terminated", "Terminated"
        elif r["kind"] == "one_time":
            a["status"], a["status_label"] = ("complied", "Complied") if last else ("open", "Open")
        else:
            item = conn.execute("SELECT * FROM maintenance_items WHERE id = ?", (r["maintenance_item_id"],)).fetchone() \
                if r["maintenance_item_id"] else None
            if not item or not last:
                a["status"], a["status_label"] = "open", "No compliance recorded"
            else:
                st = maintenance_status(item, asset_meter(asset, item["hour_type"]))
                a["status"] = {"overdue": "overdue", "due_soon": "due_soon", "ok": "ok"}.get(st["urgency"], "unknown")
                a["status_label"] = st["label"]
                a["next_due"] = st["next_due"]
        ads.append(a)
    order = {"overdue": 0, "open": 1, "due_soon": 2, "unknown": 3, "ok": 3, "complied": 4, "terminated": 5, "na": 6}
    ads.sort(key=lambda a: (order.get(a["status"], 9), a["ad_number"]))
    return ads


def _link_maintenance_item(conn, ad_id):
    """Creates/updates/deactivates the maintenance item that tracks a
    recurring AD, so it feeds the normal due-soon tracking."""
    ad = conn.execute("SELECT * FROM ads WHERE id = ?", (ad_id,)).fetchone()
    recurring = ad["kind"] in ("recurring_hours", "recurring_date") and ad["active"]
    name = f"AD {ad['ad_number']}" + (f" - {ad['subject']}" if ad["subject"] else "")
    if not recurring:
        if ad["maintenance_item_id"]:
            conn.execute("UPDATE maintenance_items SET active = 0, updated_at = ? WHERE id = ?",
                         (now_iso(), ad["maintenance_item_id"]))
        return
    mtype = "hours" if ad["kind"] == "recurring_hours" else "calendar"
    if ad["maintenance_item_id"]:
        conn.execute("""UPDATE maintenance_items SET name = ?, type = ?, interval_hours = ?, interval_days = ?,
                         active = 1, updated_at = ? WHERE id = ?""",
                     (name[:120], mtype, ad["interval_hours"], ad["interval_days"], now_iso(), ad["maintenance_item_id"]))
    else:
        cur = conn.execute("""INSERT INTO maintenance_items (asset_id, name, type, category, hour_type, interval_hours,
                               interval_days, reference_info, active, created_at, updated_at)
                               VALUES (?, ?, ?, 'scheduled_maint', 'tach', ?, ?, ?, 1, ?, ?)""",
                           (ad["asset_id"], name[:120], mtype, ad["interval_hours"], ad["interval_days"],
                            "Recurring Airworthiness Directive - record compliance in the aircraft's ADs list.",
                            now_iso(), now_iso()))
        conn.execute("UPDATE ads SET maintenance_item_id = ? WHERE id = ?", (cur.lastrowid, ad_id))


def _back(asset_id):
    return redirect(url_for("asset_detail", asset_id=asset_id) + "#ads")


@ads_bp.route("/assets/<int:asset_id>/ads", methods=["POST"])
@shop_role_required('admin', 'tech', 'inspector')
def ad_new(asset_id):
    conn = get_db()
    asset = conn.execute("SELECT * FROM assets WHERE id = ? AND deleted_at IS NULL", (asset_id,)).fetchone()
    if not asset:
        conn.close()
        abort(404)
    number = (request.form.get("ad_number") or "").strip().upper()[:40]
    subject = (request.form.get("subject") or "").strip()[:200]
    kind = request.form.get("kind")
    interval_hours = _float_or_none(request.form.get("interval_hours"), positive=True)
    try:
        interval_days = int((request.form.get("interval_days") or "").strip())
    except ValueError:
        interval_days = None
    na_reason = (request.form.get("na_reason") or "").strip()[:300]
    error = None
    if not number:
        error = "Enter the AD number (e.g. 2011-10-09)."
    elif kind not in dict(AD_KINDS):
        error = "Pick what kind of AD it is."
    elif kind == "recurring_hours" and not interval_hours:
        error = "A recurring-by-hours AD needs its interval in hours."
    elif kind == "recurring_date" and not (interval_days and interval_days > 0):
        error = "A recurring-by-date AD needs its interval in days."
    elif kind == "not_applicable" and not na_reason:
        error = "Say why the AD doesn't apply (e.g. serial number not affected)."
    elif conn.execute("SELECT 1 FROM ads WHERE asset_id = ? AND ad_number = ? AND active = 1", (asset_id, number)).fetchone():
        error = f"AD {number} is already on this aircraft."
    if error:
        conn.close()
        flash(error, "danger")
        return _back(asset_id)
    cur = conn.execute("""INSERT INTO ads (asset_id, ad_number, subject, kind, interval_hours, interval_days, na_reason,
                           active, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                       (asset_id, number, subject, kind, interval_hours if kind == "recurring_hours" else None,
                        interval_days if kind == "recurring_date" else None,
                        na_reason if kind == "not_applicable" else None, now_iso(), session.get("user_name")))
    _link_maintenance_item(conn, cur.lastrowid)
    conn.commit()
    conn.close()
    flash(f"AD {number} added.", "success")
    return _back(asset_id)


@ads_bp.route("/ads/<int:ad_id>/comply", methods=["POST"])
@shop_role_required('admin', 'tech', 'inspector')
def ad_comply(ad_id):
    conn = get_db()
    ad = conn.execute("SELECT * FROM ads WHERE id = ? AND active = 1", (ad_id,)).fetchone()
    if not ad:
        conn.close()
        abort(404)
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (ad["asset_id"],)).fetchone()
    raw_date = (request.form.get("complied_date") or "").strip() or date.today().isoformat()
    try:
        complied_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
    except ValueError:
        complied_date = None
    raw_tach = (request.form.get("tach_hours") or "").strip()
    tach = _float_or_none(raw_tach) if raw_tach else asset["tach_hours"]
    method = request.form.get("method")
    signed_by = (request.form.get("signed_by") or "").strip()[:100] or session.get("user_name")
    note = (request.form.get("note") or "").strip()[:500] or None
    error = None
    if complied_date is None or complied_date > date.today():
        error = "Use a real compliance date, today or earlier."
    elif raw_tach and tach is None:
        error = "Tach must be a number, 0 or more."
    elif method not in dict(AD_METHODS):
        error = "Pick how the AD was complied with."
    if error:
        conn.close()
        flash(error, "danger")
        return _back(ad["asset_id"])
    photo = None
    f = request.files.get("photo")
    if f and f.filename:
        if not allowed_image(f.filename):
            conn.close()
            flash("The logbook photo has to be an image (jpg, png...).", "danger")
            return _back(ad["asset_id"])
        try:
            photo = save_upload(f)
        except OSError:
            flash("Couldn't save the photo - the Pi may be low on storage. Compliance saved without it.", "warning")
    conn.execute("""INSERT INTO ad_compliance (ad_id, complied_date, tach_hours, method, signed_by, note, photo,
                     created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                 (ad_id, complied_date.isoformat(), tach, method, signed_by, note, photo, now_iso()))
    if ad["maintenance_item_id"]:
        if method == "terminated":
            conn.execute("UPDATE maintenance_items SET active = 0, updated_at = ? WHERE id = ?",
                         (now_iso(), ad["maintenance_item_id"]))
        else:
            conn.execute("UPDATE maintenance_items SET last_done_hours = ?, last_done_date = ?, updated_at = ? WHERE id = ?",
                         (tach, complied_date.isoformat(), now_iso(), ad["maintenance_item_id"]))
            conn.execute("""INSERT INTO maintenance_log (item_id, completed_at, completed_hours, performed_by, note)
                             VALUES (?, ?, ?, ?, ?)""",
                         (ad["maintenance_item_id"], complied_date.isoformat() + " 12:00:00", tach, signed_by,
                          f"AD compliance ({dict(AD_METHODS)[method]})" + (f": {note}" if note else "")))
    conn.commit()
    conn.close()
    flash(f"Compliance recorded for AD {ad['ad_number']}.", "success")
    return _back(ad["asset_id"])


@ads_bp.route("/ads/<int:ad_id>/delete", methods=["POST"])
@shop_role_required('admin')
def ad_delete(ad_id):
    """Removes an AD added by mistake. Its compliance records are kept in
    the database (just hidden), and its maintenance item stops tracking."""
    conn = get_db()
    ad = conn.execute("SELECT * FROM ads WHERE id = ?", (ad_id,)).fetchone()
    if not ad:
        conn.close()
        abort(404)
    conn.execute("UPDATE ads SET active = 0 WHERE id = ?", (ad_id,))
    _link_maintenance_item(conn, ad_id)
    conn.commit()
    conn.close()
    flash(f"AD {ad['ad_number']} removed from this aircraft.", "success")
    return _back(ad["asset_id"])


@ads_bp.route("/assets/<int:asset_id>/ads/print")
@shop_role_required('admin', 'tech', 'inspector')
def ad_status_print(asset_id):
    """Printable AD Status sheet for the IA during the annual, and for the
    owner with their invoice."""
    conn = get_db()
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not asset:
        conn.close()
        abort(404)
    ads = ads_for_asset(conn, asset)
    conn.close()
    return render_template("ad_status_print.html", asset=asset, ads=ads, kinds=dict(AD_KINDS),
                           methods=dict(AD_METHODS), today=date.today().isoformat())


def add_ads_to_annual_project(conn, project_id, asset_id):
    """New Annual project: add each open one-time AD and each recurring AD
    to the project's Job Sheet list, so the IA checks them off there.
    Returns how many were added."""
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not asset:
        return 0
    lines = []
    for a in ads_for_asset(conn, asset):
        if a["status"] in ("na", "terminated", "complied"):
            continue
        detail = dict(AD_KINDS)[a["kind"]]
        if a["kind"] != "one_time" and a["status_label"]:
            detail += f", {a['status_label']}"
        lines.append(f"AD {a['ad_number']}" + (f" - {a['subject']}" if a["subject"] else "") + f" ({detail})")
    if not lines:
        return 0
    row = conn.execute("SELECT standard_items FROM projects WHERE id = ?", (project_id,)).fetchone()
    existing = (row["standard_items"] or "").strip() if row else ""
    new_text = (existing + "\n" if existing else "") + "\n".join(lines)
    conn.execute("UPDATE projects SET standard_items = ? WHERE id = ?", (new_text, project_id))
    return len(lines)
