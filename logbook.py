"""Maintenance logbook entries: a "logbook sticker starter" built from a
project's sub areas and the parts scanned into them, a combined entry for
the whole project from a per-project-type template (Annual, 100-Hour, Oil
Change, Repair), and a searchable Logbook (Manage > Logbook, plus each
plane's own Airframe / Engine / Propeller logs).

Nothing here is signed or final by itself - the app drafts the wording from
what's already in the database, a mechanic edits it, saves it, and prints
it as a sticker for the paper logbook.
"""
import re
from datetime import date

from flask import Blueprint, render_template, request, redirect, url_for, session, flash, abort

from db import get_db, now_iso
from auth import shop_role_required

logbook_bp = Blueprint("logbook", __name__)

LOG_TYPES = [("airframe", "Airframe"), ("engine", "Engine"), ("propeller", "Propeller")]
LOG_TYPE_LABELS = dict(LOG_TYPES)

PROJECT_TYPES = [("annual", "Annual Inspection"), ("100hour", "100-Hour Inspection"),
                 ("oil_change", "Oil Change"), ("repair", "Repair / Maintenance")]
PROJECT_TYPE_LABELS = dict(PROJECT_TYPES)

# Which logbooks each kind of project normally writes a combined entry in.
PROJECT_TYPE_LOGS = {
    "annual": ["airframe", "engine", "propeller"],
    "100hour": ["airframe", "engine", "propeller"],
    "oil_change": ["engine"],
    "repair": [],  # whichever logs the sub areas landed in
}

# A sub area's name decides which logbook its entry goes in by default
# (the mechanic can change it on the page).
_ENGINE_WORDS = ("engine", "oil", "cylinder", "valve", "magneto", "mag ", "mags", "spark", "plug", "exhaust",
                 "carb", "induction", "intake", "fuel injection", "injector", "alternator", "starter",
                 "compression", "baffle", "muffler", "turbo", "crank", "cam", "piston", "ignition", "harness")
_PROP_WORDS = ("prop", "spinner", "governor", "hub", "blade")

RETURN_TO_SERVICE = ("The work described above was performed in accordance with 14 CFR Part 43 and current "
                     "manufacturer's data, and the {what} is approved for return to service with respect to "
                     "the work performed.")
SIGNATURE_LINE = "Signature: ______________________  Cert #: ______________"

DEFAULT_TEMPLATES = {
    ("annual", "airframe"): (
        "{date}  Tach {tach}  Hobbs {hobbs}\n"
        "{tail}  {make_model}  S/N {serial}\n"
        "Performed an annual inspection in accordance with 14 CFR Part 43 Appendix D (project {project_code}).\n"
        "{work}\n"
        "Parts installed:\n{parts}\n"
        "I certify that this aircraft has been inspected in accordance with an annual inspection and was "
        "determined to be in airworthy condition.\n"
        "Signature: ______________________  IA #: ______________"),
    ("annual", "engine"): (
        "{date}  Tach {tach}\n"
        "Engine {engine}  S/N {engine_serial}  ({tail})\n"
        "Performed the engine portion of an annual inspection in accordance with 14 CFR Part 43 Appendix D "
        "(project {project_code}).\n"
        "{work}\n"
        "Parts installed:\n{parts}\n"
        "I certify that this engine has been inspected in accordance with an annual inspection and was "
        "determined to be in airworthy condition.\n"
        "Signature: ______________________  IA #: ______________"),
    ("annual", "propeller"): (
        "{date}  Tach {tach}\n"
        "Propeller {prop}  S/N {prop_serial}  ({tail})\n"
        "Performed the propeller portion of an annual inspection in accordance with 14 CFR Part 43 Appendix D "
        "(project {project_code}).\n"
        "{work}\n"
        "Parts installed:\n{parts}\n"
        "I certify that this propeller has been inspected in accordance with an annual inspection and was "
        "determined to be in airworthy condition.\n"
        "Signature: ______________________  IA #: ______________"),
    ("100hour", "airframe"): (
        "{date}  Tach {tach}  Hobbs {hobbs}\n"
        "{tail}  {make_model}  S/N {serial}\n"
        "Performed a 100-hour inspection in accordance with 14 CFR Part 43 Appendix D (project {project_code}).\n"
        "{work}\n"
        "Parts installed:\n{parts}\n"
        "I certify that this aircraft has been inspected in accordance with a 100-hour inspection and was "
        "determined to be in airworthy condition.\n"
        + SIGNATURE_LINE),
    ("100hour", "engine"): (
        "{date}  Tach {tach}\n"
        "Engine {engine}  S/N {engine_serial}  ({tail})\n"
        "Performed the engine portion of a 100-hour inspection in accordance with 14 CFR Part 43 Appendix D "
        "(project {project_code}).\n"
        "{work}\n"
        "Parts installed:\n{parts}\n"
        "I certify that this engine has been inspected in accordance with a 100-hour inspection and was "
        "determined to be in airworthy condition.\n"
        + SIGNATURE_LINE),
    ("100hour", "propeller"): (
        "{date}  Tach {tach}\n"
        "Propeller {prop}  S/N {prop_serial}  ({tail})\n"
        "Performed the propeller portion of a 100-hour inspection in accordance with 14 CFR Part 43 Appendix D "
        "(project {project_code}).\n"
        "{work}\n"
        "Parts installed:\n{parts}\n"
        "I certify that this propeller has been inspected in accordance with a 100-hour inspection and was "
        "determined to be in airworthy condition.\n"
        + SIGNATURE_LINE),
    ("oil_change", "engine"): (
        "{date}  Tach {tach}\n"
        "Engine {engine}  S/N {engine_serial}  ({tail})\n"
        "Drained engine oil and replaced oil and filter. Inspected filter element - no abnormal metal found. "
        "Ran engine, no leaks (project {project_code}).\n"
        "{work}\n"
        "Parts installed:\n{parts}\n"
        + RETURN_TO_SERVICE.format(what="engine") + "\n" + SIGNATURE_LINE),
}
# Anything without its own template (e.g. an oil change's airframe log, or
# a general repair) uses this one.
GENERIC_TEMPLATE = (
    "{date}  Tach {tach}  Hobbs {hobbs}\n"
    "{what_line}\n"
    "{work}\n"
    "Parts installed:\n{parts}\n"
    "{return_to_service}\n"
    + SIGNATURE_LINE)

PLACEHOLDERS = [
    ("{date}", "entry date"), ("{tail}", "N number"), ("{make_model}", "make / model / year"),
    ("{serial}", "airframe S/N"), ("{engine}", "engine make/model"), ("{engine_serial}", "engine S/N"),
    ("{prop}", "prop make/model"), ("{prop_serial}", "prop S/N"), ("{tach}", "tach time"),
    ("{hobbs}", "Hobbs time"), ("{project_code}", "project number"), ("{project_name}", "project name"),
    ("{work}", "one line per discrepancy's work"), ("{parts}", "parts list with P/N"),
    ("{return_to_service}", "standard return-to-service sentence"),
]


def _can_edit():
    return bool(session.get("is_master_admin") or session.get("shop_role") in ("admin", "tech"))


def _is_admin():
    return bool(session.get("is_master_admin") or session.get("shop_role") == "admin")


def guess_log_type(section_name):
    s = f" {(section_name or '').lower()} "
    if any(w in s for w in _PROP_WORDS):
        return "propeller"
    if any(w in s for w in _ENGINE_WORDS):
        return "engine"
    return "airframe"


def guess_project_type(conn, project):
    """From maintenance items this project completed (their category), else
    from words in the project's name/description; 'repair' otherwise."""
    row = conn.execute("""SELECT mi.category FROM maintenance_log ml JOIN maintenance_items mi ON mi.id = ml.item_id
                          WHERE ml.project_id = ? ORDER BY ml.completed_at DESC LIMIT 1""",
                       (project["id"],)).fetchone()
    if row and row["category"] in PROJECT_TYPE_LABELS:
        return row["category"]
    text = f"{project['name'] or ''} {project['description'] or ''}".lower()
    if "annual" in text:
        return "annual"
    if "100" in text and ("hour" in text or "hr" in text):
        return "100hour"
    if "oil" in text:
        return "oil_change"
    return "repair"


def part_number(row):
    """A part's P/N for the logbook: its Part Number if one is saved, else
    its barcode when that's a real (manufacturer) barcode, else blank for
    the mechanic to fill in."""
    pn = (row["part_number"] if "part_number" in row.keys() else None) or ""
    if pn.strip():
        return pn.strip()
    bc = (row["barcode"] or "").strip()
    if bc and not bc.upper().startswith("SHOP-"):
        return bc
    return "________"


def _fmt_qty(q):
    q = q or 0
    return str(int(q)) if float(q).is_integer() else f"{q:g}"


def _part_line(p):
    return f"{p['name']}, P/N {part_number(p)}, qty {_fmt_qty(p['qty_used'])}{'' if (p['unit'] or 'ea') == 'ea' else ' ' + p['unit']}"


def _fmt_hours(v):
    return "______" if v is None else f"{v:.1f}"


def _project_parts(conn, project_id):
    return conn.execute("""
        SELECT p.id as part_id, p.name, p.barcode, p.part_number, p.unit, t.section as section,
               SUM(CASE WHEN t.type='out' THEN t.qty ELSE -t.qty END) as qty_used
        FROM transactions t JOIN parts p ON p.id = t.part_id
        WHERE t.project_id = ?
        GROUP BY p.id, t.section
        HAVING qty_used > 0
        ORDER BY p.name""", (project_id,)).fetchall()


def _load_template(conn, project_type, log_type):
    row = conn.execute("SELECT body FROM logbook_templates WHERE project_type = ? AND log_type = ?",
                       (project_type, log_type)).fetchone()
    if row and (row["body"] or "").strip():
        return row["body"], True
    return DEFAULT_TEMPLATES.get((project_type, log_type), GENERIC_TEMPLATE), False


def _fill(template, values):
    # Only swap known {placeholders}; anything else in braces is left as-is
    # so an uploaded template with stray braces can't break the page.
    return re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1), m.group(0))), template)


def build_starters(conn, project, asset, project_type, entry_date, tach, hobbs):
    """Returns (section_starters, combined_starters).

    section_starters: one per sub area (plus General for parts scanned with
    no sub area) - the work line, parts with P/N, and the return-to-service
    endorsement.
    combined_starters: one per logbook the whole project writes in, from
    that project type's template."""
    parts = _project_parts(conn, project["id"])
    sections = {}
    for p in parts:
        sections.setdefault(p["section"] or "General", []).append(p)
    for r in conn.execute("SELECT name FROM project_sections WHERE project_id = ? ORDER BY name",
                          (project["id"],)).fetchall():
        sections.setdefault(r["name"], [])
    ordered = sorted(sections.items(), key=lambda kv: (kv[0] == "General", kv[0].lower()))

    date_txt = entry_date
    head = f"{date_txt}  Tach {_fmt_hours(tach)}"
    section_starters = []
    for name, rows in ordered:
        log_type = guess_log_type(name)
        what = {"engine": "engine", "propeller": "propeller"}.get(log_type, "aircraft")
        lines = [head]
        area = project["name"] if name == "General" else name
        if rows:
            for p in rows:
                lines.append(f"{area}: Replaced {p['name']} using P/N {part_number(p)} "
                             f"(qty {_fmt_qty(p['qty_used'])}).")
        else:
            lines.append(f"{area}: ______________________________________________")
        lines.append(RETURN_TO_SERVICE.format(what=what))
        lines.append(SIGNATURE_LINE)
        section_starters.append({"section": name, "log_type": log_type, "body": "\n".join(lines),
                                 "part_count": len(rows)})

    # Combined entries: one per log the project type calls for, plus any
    # log a sub area landed in.
    logs = list(PROJECT_TYPE_LOGS.get(project_type, []))
    for s in section_starters:
        if s["log_type"] not in logs:
            logs.append(s["log_type"])
    if not logs:
        logs = ["airframe"]
    logs = [lt for lt, _l in LOG_TYPES if lt in logs]

    make_model = " ".join(x for x in [asset["year"], asset["make"], asset["model"]] if x) if asset else ""
    combined = []
    for lt in logs:
        in_log = [s for s in section_starters if s["log_type"] == lt]
        work = "\n".join(f"- {s['section'] if s['section'] != 'General' else 'General'}: "
                         + (", ".join(f"replaced {p['name']}" for p in sections[s['section']]) or "see work order")
                         for s in in_log) or "- ______________________________________________"
        part_rows = [p for s in in_log for p in sections[s["section"]]]
        parts_txt = "\n".join(f"- {_part_line(p)}" for p in part_rows) or "- None"
        what = {"engine": "engine", "propeller": "propeller"}.get(lt, "aircraft")
        what_line = {
            "airframe": f"{asset['tag'] if asset else ''}  {make_model}  S/N {asset['serial_number'] or '______' if asset else '______'}",
            "engine": f"Engine {' '.join(x for x in [asset['engine_make'], asset['engine_model']] if x) or '______' if asset else '______'}  S/N {asset['engine_serial'] or '______' if asset else '______'}",
            "propeller": f"Propeller {' '.join(x for x in [asset['prop_make'], asset['prop_model']] if x) or '______' if asset else '______'}  S/N {asset['prop_serial'] or '______' if asset else '______'}",
        }[lt] + f"  (project {project['code']} - {project['name']})"
        values = {
            "date": date_txt, "tach": _fmt_hours(tach), "hobbs": _fmt_hours(hobbs),
            "tail": asset["tag"] if asset else "______", "make_model": make_model or "______",
            "serial": (asset["serial_number"] if asset else None) or "______",
            "engine": (" ".join(x for x in [asset["engine_make"], asset["engine_model"]] if x) if asset else "") or "______",
            "engine_serial": (asset["engine_serial"] if asset else None) or "______",
            "prop": (" ".join(x for x in [asset["prop_make"], asset["prop_model"]] if x) if asset else "") or "______",
            "prop_serial": (asset["prop_serial"] if asset else None) or "______",
            "project_code": project["code"] or "", "project_name": project["name"] or "",
            "work": work, "parts": parts_txt, "what_line": what_line,
            "return_to_service": RETURN_TO_SERVICE.format(what=what),
        }
        tpl, custom = _load_template(conn, project_type, lt)
        combined.append({"log_type": lt, "body": _fill(tpl, values), "custom_template": custom})
    return section_starters, combined


def _parse_hours(v):
    try:
        return round(float(v), 1) if str(v or "").strip() else None
    except ValueError:
        return None


def _parse_date(v):
    v = (v or "").strip()
    try:
        return date.fromisoformat(v).isoformat()
    except ValueError:
        return None


# ---------------------------------------------------------------- starter

@logbook_bp.route("/projects/<int:project_id>/logbook")
@shop_role_required('admin', 'tech', 'inspector')
def project_logbook(project_id):
    conn = get_db()
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        conn.close()
        abort(404)
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (project["asset_id"],)).fetchone() \
        if project["asset_id"] else None
    project_type = request.args.get("type") or guess_project_type(conn, project)
    if project_type not in PROJECT_TYPE_LABELS:
        project_type = "repair"
    entry_date = _parse_date(request.args.get("date")) or date.today().isoformat()
    tach = _parse_hours(request.args.get("tach")) if request.args.get("tach") else (asset["tach_hours"] if asset else None)
    hobbs = _parse_hours(request.args.get("hobbs")) if request.args.get("hobbs") else (asset["hobbs_hours"] if asset else None)
    section_starters, combined = build_starters(conn, project, asset, project_type, entry_date, tach, hobbs)
    saved = conn.execute("""SELECT * FROM logbook_entries WHERE project_id = ? AND deleted_at IS NULL
                            ORDER BY entry_date DESC, id DESC""", (project_id,)).fetchall()
    conn.close()
    return render_template("logbook_starter.html", project=project, asset=asset, project_type=project_type,
                           project_types=PROJECT_TYPES, log_types=LOG_TYPES, log_type_labels=LOG_TYPE_LABELS,
                           entry_date=entry_date, tach=tach, hobbs=hobbs, section_starters=section_starters,
                           combined=combined, saved=saved, can_edit=_can_edit())


@logbook_bp.route("/projects/<int:project_id>/logbook/save", methods=["POST"])
@shop_role_required('admin', 'tech')
def project_logbook_save(project_id):
    conn = get_db()
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        conn.close()
        abort(404)
    body = (request.form.get("body") or "").strip()
    log_type = request.form.get("log_type")
    if log_type not in LOG_TYPE_LABELS:
        log_type = "airframe"
    project_type = request.form.get("project_type")
    if project_type not in PROJECT_TYPE_LABELS:
        project_type = None
    entry_date = _parse_date(request.form.get("entry_date")) or date.today().isoformat()
    back = url_for("logbook.project_logbook", project_id=project_id, type=project_type or None,
                   date=entry_date, tach=request.form.get("tach") or None, hobbs=request.form.get("hobbs") or None)
    if not body:
        conn.close()
        flash("That entry is empty - nothing saved.", "danger")
        return redirect(back)
    section = (request.form.get("section") or "").strip() or None
    conn.execute("""INSERT INTO logbook_entries (asset_id, project_id, section, log_type, project_type,
                          entry_date, tach_hours, hobbs_hours, body, created_by, created_at, updated_at)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       (project["asset_id"], project_id, section, log_type, project_type, entry_date,
                        _parse_hours(request.form.get("tach")), _parse_hours(request.form.get("hobbs")), body,
                        session.get("user_name"), now_iso(), now_iso()))
    conn.commit()
    conn.close()
    what = f"'{section}'" if section else "Combined project"
    flash(f"{what} entry saved to the {LOG_TYPE_LABELS[log_type]} log - print its sticker from Saved Entries below.", "success")
    return redirect(back)


# ---------------------------------------------------------------- browse

@logbook_bp.route("/logbook")
@shop_role_required('admin', 'tech', 'inspector')
def logbook_list():
    conn = get_db()
    q = (request.args.get("q") or "").strip()
    asset_id = request.args.get("asset_id", type=int)
    project_q = (request.args.get("project") or "").strip()
    log_type = request.args.get("log_type") or ""
    date_from = _parse_date(request.args.get("from"))
    date_to = _parse_date(request.args.get("to"))
    where, args = ["e.deleted_at IS NULL"], []
    if asset_id:
        where.append("e.asset_id = ?")
        args.append(asset_id)
    if log_type in LOG_TYPE_LABELS:
        where.append("e.log_type = ?")
        args.append(log_type)
    if date_from:
        where.append("e.entry_date >= ?")
        args.append(date_from)
    if date_to:
        where.append("e.entry_date <= ?")
        args.append(date_to)
    if project_q:
        where.append("(pr.code LIKE ? OR pr.name LIKE ?)")
        args += [f"%{project_q}%", f"%{project_q}%"]
    if q:
        where.append("(e.body LIKE ? OR e.section LIKE ? OR a.tag LIKE ?)")
        args += [f"%{q}%", f"%{q}%", f"%{q}%"]
    entries = conn.execute(f"""SELECT e.*, a.tag as asset_tag, pr.code as project_code, pr.name as project_name
                               FROM logbook_entries e
                               LEFT JOIN assets a ON a.id = e.asset_id
                               LEFT JOIN projects pr ON pr.id = e.project_id
                               WHERE {' AND '.join(where)}
                               ORDER BY e.entry_date DESC, e.id DESC LIMIT 500""", args).fetchall()
    planes = conn.execute("SELECT id, tag, name FROM assets WHERE deleted_at IS NULL ORDER BY tag").fetchall()
    conn.close()
    filtered = bool(q or asset_id or project_q or log_type or date_from or date_to)
    return render_template("logbook_list.html", entries=entries, planes=planes, log_types=LOG_TYPES,
                           log_type_labels=LOG_TYPE_LABELS, project_type_labels=PROJECT_TYPE_LABELS,
                           filters={"q": q, "asset_id": asset_id, "project": project_q, "log_type": log_type,
                                    "from": date_from or "", "to": date_to or ""}, filtered=filtered)


@logbook_bp.route("/assets/<int:asset_id>/logbook")
@shop_role_required('admin', 'tech', 'inspector')
def asset_logbook(asset_id):
    conn = get_db()
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not asset:
        conn.close()
        abort(404)
    rows = conn.execute("""SELECT e.*, pr.code as project_code, pr.name as project_name
                           FROM logbook_entries e LEFT JOIN projects pr ON pr.id = e.project_id
                           WHERE e.asset_id = ? AND e.deleted_at IS NULL
                           ORDER BY e.entry_date DESC, e.id DESC""", (asset_id,)).fetchall()
    conn.close()
    by_log = {lt: [] for lt, _l in LOG_TYPES}
    for r in rows:
        by_log.setdefault(r["log_type"], []).append(r)
    tab = request.args.get("log") if request.args.get("log") in by_log else \
        next((lt for lt, _l in LOG_TYPES if by_log[lt]), "airframe")
    return render_template("logbook_asset.html", asset=asset, by_log=by_log, log_types=LOG_TYPES, tab=tab,
                           project_type_labels=PROJECT_TYPE_LABELS)


@logbook_bp.app_template_global()
def logbook_counts(asset_id):
    """{log_type: count, 'last': newest entry_date} for the plane page card."""
    conn = get_db()
    out = {lt: 0 for lt, _l in LOG_TYPES}
    try:
        for r in conn.execute("""SELECT log_type, COUNT(*) c FROM logbook_entries
                                 WHERE asset_id = ? AND deleted_at IS NULL GROUP BY log_type""", (asset_id,)):
            out[r["log_type"]] = r["c"]
        last = conn.execute("SELECT MAX(entry_date) d FROM logbook_entries WHERE asset_id = ? AND deleted_at IS NULL",
                            (asset_id,)).fetchone()
        out["last"] = last["d"] if last else None
    finally:
        conn.close()
    return out


# ---------------------------------------------------------------- one entry

def _get_entry(conn, entry_id):
    e = conn.execute("""SELECT e.*, a.tag as asset_tag, a.make as asset_make, a.model as asset_model,
                               pr.code as project_code, pr.name as project_name
                        FROM logbook_entries e
                        LEFT JOIN assets a ON a.id = e.asset_id
                        LEFT JOIN projects pr ON pr.id = e.project_id
                        WHERE e.id = ? AND e.deleted_at IS NULL""", (entry_id,)).fetchone()
    if not e:
        conn.close()
        abort(404)
    return e


@logbook_bp.route("/logbook/<int:entry_id>", methods=["GET", "POST"])
@shop_role_required('admin', 'tech', 'inspector')
def entry_detail(entry_id):
    conn = get_db()
    e = _get_entry(conn, entry_id)
    if request.method == "POST":
        # JOBS-16: an inspector can read and print entries; saving is admin/tech.
        if not _can_edit():
            conn.close()
            flash("You don't have access to that part of Shop Inventory.", "danger")
            return redirect(url_for("logbook.entry_detail", entry_id=entry_id))
        body = (request.form.get("body") or "").strip()
        if not body:
            flash("The entry text can't be empty.", "danger")
        else:
            log_type = request.form.get("log_type") if request.form.get("log_type") in LOG_TYPE_LABELS else e["log_type"]
            conn.execute("""UPDATE logbook_entries SET body=?, log_type=?, entry_date=?, tach_hours=?, hobbs_hours=?,
                            signed_by=?, cert_number=?, updated_at=? WHERE id=?""",
                         (body, log_type, _parse_date(request.form.get("entry_date")) or e["entry_date"],
                          _parse_hours(request.form.get("tach")), _parse_hours(request.form.get("hobbs")),
                          (request.form.get("signed_by") or "").strip() or None,
                          (request.form.get("cert_number") or "").strip() or None, now_iso(), entry_id))
            conn.commit()
            flash("Logbook entry updated.", "success")
        conn.close()
        return redirect(url_for("logbook.entry_detail", entry_id=entry_id))
    conn.close()
    return render_template("logbook_entry.html", e=e, log_types=LOG_TYPES, log_type_labels=LOG_TYPE_LABELS,
                           project_type_labels=PROJECT_TYPE_LABELS, is_admin=_is_admin(), can_edit=_can_edit())


@logbook_bp.route("/logbook/<int:entry_id>/print")
@shop_role_required('admin', 'tech', 'inspector')
def entry_print(entry_id):
    conn = get_db()
    e = _get_entry(conn, entry_id)
    conn.close()
    return render_template("logbook_print.html", e=e, log_type_labels=LOG_TYPE_LABELS)


@logbook_bp.route("/logbook/<int:entry_id>/delete", methods=["POST"])
@shop_role_required('admin')
def entry_delete(entry_id):
    conn = get_db()
    e = _get_entry(conn, entry_id)
    conn.execute("UPDATE logbook_entries SET deleted_at = ? WHERE id = ?", (now_iso(), entry_id))
    conn.commit()
    conn.close()
    flash("Logbook entry deleted forever.", "success")
    if e["asset_id"]:
        return redirect(url_for("logbook.asset_logbook", asset_id=e["asset_id"], log=e["log_type"]))
    return redirect(url_for("logbook.logbook_list"))


# ---------------------------------------------------------------- templates

@logbook_bp.route("/logbook/templates", methods=["GET", "POST"])
@shop_role_required('admin')
def logbook_templates():
    conn = get_db()
    if request.method == "POST":
        project_type = request.form.get("project_type")
        log_type = request.form.get("log_type")
        if project_type not in PROJECT_TYPE_LABELS or log_type not in LOG_TYPE_LABELS:
            conn.close()
            abort(400)
        body = request.form.get("body") or ""
        upload = request.files.get("file")
        if upload and upload.filename:
            raw = upload.read(200_000)
            try:
                body = raw.decode("utf-8")
            except UnicodeDecodeError:
                body = raw.decode("latin-1")
        label = f"{PROJECT_TYPE_LABELS[project_type]} - {LOG_TYPE_LABELS[log_type]}"
        if request.form.get("action") == "reset" or not body.strip():
            conn.execute("DELETE FROM logbook_templates WHERE project_type = ? AND log_type = ?",
                         (project_type, log_type))
            flash(f"{label} template set back to the built-in wording.", "success")
        else:
            conn.execute("""INSERT INTO logbook_templates (project_type, log_type, body, updated_by, updated_at)
                            VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(project_type, log_type) DO UPDATE SET body=excluded.body,
                            updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
                         (project_type, log_type, body.replace("\r\n", "\n").strip(), session.get("user_name"), now_iso()))
            flash(f"{label} template saved.", "success")
        conn.commit()
        conn.close()
        return redirect(url_for("logbook.logbook_templates", _anchor=f"t-{project_type}-{log_type}"))
    templates = []
    for pt, pl in PROJECT_TYPES:
        for lt, ll in LOG_TYPES:
            body, custom = _load_template(conn, pt, lt)
            templates.append({"project_type": pt, "project_label": pl, "log_type": lt, "log_label": ll,
                              "body": body, "custom": custom})
    conn.close()
    return render_template("logbook_templates.html", templates=templates, placeholders=PLACEHOLDERS)
