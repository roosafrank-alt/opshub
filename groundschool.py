"""Ground School: FAA Airman Certification Standards (ACS) library and
lesson-plan builder.

An admin uploads the official ACS PDF for a rating (Private Pilot,
Instrument, etc.); acs_parser.parse_acs() extracts its Areas of Operation,
Tasks, and each Task's References/Objective/Notes/Knowledge/Risk
Management/Skills elements (with their PA.x.x.x-style codes), which get
stored so the rating can be browsed without re-reading the PDF. Clicking a
Task shows that verbatim ACS content plus two things the ACS itself
doesn't have: an admin-editable lesson body (talking points, materials,
links - see acs_lesson_content), and per-student sign-off tracking (see
acs_task_signoff) that a CFI can check off once a student's covered that
Task on the ground or in the air.
"""
import os
import re
import string
import random

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, session

from db import get_db, now_iso, UPLOAD_DIR
from flight import login_required, cfi_required, admin_required
from acs_parser import parse_acs

groundschool_bp = Blueprint("groundschool", __name__, url_prefix="/flight/groundschool")

_DOC_NUMBER_RE = re.compile(r"\bFAA-[SG]-ACS-\S+\b")


def _slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "rating"


def _unique_slug(conn, name, exclude_id=None):
    base = _slugify(name)
    slug = base
    n = 2
    while True:
        row = conn.execute("SELECT id FROM acs_ratings WHERE slug = ?", (slug,)).fetchone()
        if not row or row["id"] == exclude_id:
            return slug
        slug = f"{base}-{n}"
        n += 1


def _save_pdf(file_storage):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    alphabet = string.ascii_lowercase + string.digits
    unique = "".join(random.choices(alphabet, k=12))
    stored_name = f"acs_{unique}.pdf"
    file_storage.save(os.path.join(UPLOAD_DIR, stored_name))
    return stored_name


def _detect_doc_number(pdf_path):
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            text = (pdf.pages[0].extract_text() or "") if pdf.pages else ""
    except Exception:
        return None
    m = _DOC_NUMBER_RE.search(text)
    return m.group(0) if m else None


def _parse_and_store(conn, rating_id, pdf_path):
    """Runs the parser and replaces this rating's areas/tasks/elements with
    the result. Existing lesson content and sign-offs are keyed off task id,
    so a re-parse (same PDF re-uploaded, or the parser improved) would lose
    them if task ids shift - acceptable for v1 since this only runs right
    after upload, before anyone's built lessons against it yet; a Task's
    lesson content and sign-offs are deleted along with it."""
    old_task_ids = [r["id"] for r in conn.execute(
        "SELECT t.id FROM acs_tasks t JOIN acs_areas a ON a.id = t.area_id WHERE a.rating_id = ?",
        (rating_id,)).fetchall()]
    for tid in old_task_ids:
        conn.execute("DELETE FROM acs_task_signoff WHERE task_id = ?", (tid,))
        conn.execute("DELETE FROM acs_lesson_content WHERE task_id = ?", (tid,))
        conn.execute("DELETE FROM acs_task_elements WHERE task_id = ?", (tid,))
        conn.execute("DELETE FROM acs_task_notes WHERE task_id = ?", (tid,))
    conn.execute("DELETE FROM acs_tasks WHERE area_id IN (SELECT id FROM acs_areas WHERE rating_id = ?)",
                 (rating_id,))
    conn.execute("DELETE FROM acs_areas WHERE rating_id = ?", (rating_id,))
    conn.commit()

    areas = parse_acs(pdf_path)
    if not areas or not any(a["tasks"] for a in areas):
        raise ValueError("No Areas of Operation with Tasks were found in this PDF - "
                          "is it the right document, and does it follow the standard ACS layout?")
    for area_idx, area in enumerate(areas):
        cur = conn.execute(
            "INSERT INTO acs_areas (rating_id, code, title, order_index) VALUES (?, ?, ?, ?)",
            (rating_id, area["code"], area["title"], area_idx))
        area_id = cur.lastrowid
        for task_idx, task in enumerate(area["tasks"]):
            cur = conn.execute(
                "INSERT INTO acs_tasks (area_id, code, title, acs_references, objective, order_index) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (area_id, task["code"], task["title"], task["references"], task["objective"], task_idx))
            task_id = cur.lastrowid
            for note_idx, note in enumerate(task["notes"]):
                conn.execute("INSERT INTO acs_task_notes (task_id, note, order_index) VALUES (?, ?, ?)",
                             (task_id, note, note_idx))
            order_index = 0
            for kind in ("knowledge", "risk_management", "skills"):
                for el in task[kind]:
                    conn.execute(
                        "INSERT INTO acs_task_elements (task_id, kind, code, text, order_index) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (task_id, kind, el["code"], el["text"], order_index))
                    order_index += 1
    conn.commit()
    n_tasks = sum(len(a["tasks"]) for a in areas)
    n_elements = sum(len(t["knowledge"]) + len(t["risk_management"]) + len(t["skills"])
                      for a in areas for t in a["tasks"])
    return len(areas), n_tasks, n_elements


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@groundschool_bp.route("")
@login_required
def rating_list():
    conn = get_db()
    ratings = conn.execute("SELECT * FROM acs_ratings ORDER BY name").fetchall()
    conn.close()
    return render_template("flight/groundschool_list.html", ratings=ratings)


@groundschool_bp.route("/upload", methods=["POST"])
@admin_required
def upload():
    f = request.files.get("acs_file")
    name = request.form.get("name", "").strip()
    if not f or not f.filename or not f.filename.lower().endswith(".pdf"):
        flash("Choose a PDF to upload.", "danger")
        return redirect(url_for("groundschool.rating_list"))
    if not name:
        name = f.filename.rsplit(".", 1)[0]

    stored_name = _save_pdf(f)
    pdf_path = os.path.join(UPLOAD_DIR, stored_name)
    conn = get_db()
    slug = _unique_slug(conn, name)
    doc_number = _detect_doc_number(pdf_path)
    cur = conn.execute(
        "INSERT INTO acs_ratings (name, slug, doc_number, pdf_filename, uploaded_by, uploaded_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, 'empty')",
        (name, slug, doc_number, stored_name, session.get("cfi_id"), now_iso()))
    rating_id = cur.lastrowid
    conn.commit()
    try:
        n_areas, n_tasks, n_elements = _parse_and_store(conn, rating_id, pdf_path)
        conn.execute("UPDATE acs_ratings SET status = 'parsed', parsed_at = ?, parse_error = NULL WHERE id = ?",
                     (now_iso(), rating_id))
        conn.commit()
        flash(f"Uploaded '{name}' - found {n_areas} Areas of Operation, {n_tasks} Tasks, "
              f"{n_elements} Knowledge/Risk Management/Skills elements.", "success")
    except Exception as e:
        conn.execute("UPDATE acs_ratings SET status = 'error', parse_error = ? WHERE id = ?",
                     (str(e), rating_id))
        conn.commit()
        flash(f"Uploaded '{name}', but parsing failed: {e}", "danger")
    conn.close()
    return redirect(url_for("groundschool.rating_detail", rating_id=rating_id))


@groundschool_bp.route("/<int:rating_id>/reparse", methods=["POST"])
@admin_required
def reparse(rating_id):
    conn = get_db()
    rating = conn.execute("SELECT * FROM acs_ratings WHERE id = ?", (rating_id,)).fetchone()
    if not rating:
        conn.close()
        abort(404)
    pdf_path = os.path.join(UPLOAD_DIR, rating["pdf_filename"])
    try:
        n_areas, n_tasks, n_elements = _parse_and_store(conn, rating_id, pdf_path)
        conn.execute("UPDATE acs_ratings SET status = 'parsed', parsed_at = ?, parse_error = NULL WHERE id = ?",
                     (now_iso(), rating_id))
        conn.commit()
        flash(f"Re-parsed '{rating['name']}' - {n_areas} Areas, {n_tasks} Tasks, {n_elements} elements. "
              "Note: any lesson content or student sign-offs on this rating's Tasks were cleared by the re-parse.",
              "warning")
    except Exception as e:
        conn.execute("UPDATE acs_ratings SET status = 'error', parse_error = ? WHERE id = ?",
                     (str(e), rating_id))
        conn.commit()
        flash(f"Re-parse failed: {e}", "danger")
    conn.close()
    return redirect(url_for("groundschool.rating_detail", rating_id=rating_id))


@groundschool_bp.route("/<int:rating_id>/delete", methods=["POST"])
@admin_required
def rating_delete(rating_id):
    conn = get_db()
    rating = conn.execute("SELECT * FROM acs_ratings WHERE id = ?", (rating_id,)).fetchone()
    if rating:
        task_ids = [r["id"] for r in conn.execute(
            "SELECT t.id FROM acs_tasks t JOIN acs_areas a ON a.id = t.area_id WHERE a.rating_id = ?",
            (rating_id,)).fetchall()]
        for tid in task_ids:
            conn.execute("DELETE FROM acs_task_signoff WHERE task_id = ?", (tid,))
            conn.execute("DELETE FROM acs_lesson_content WHERE task_id = ?", (tid,))
            conn.execute("DELETE FROM acs_task_elements WHERE task_id = ?", (tid,))
            conn.execute("DELETE FROM acs_task_notes WHERE task_id = ?", (tid,))
        conn.execute("DELETE FROM acs_tasks WHERE area_id IN (SELECT id FROM acs_areas WHERE rating_id = ?)",
                     (rating_id,))
        conn.execute("DELETE FROM acs_areas WHERE rating_id = ?", (rating_id,))
        conn.execute("DELETE FROM acs_ratings WHERE id = ?", (rating_id,))
        conn.commit()
        try:
            os.remove(os.path.join(UPLOAD_DIR, rating["pdf_filename"]))
        except OSError:
            pass
        flash(f"Deleted '{rating['name']}'.", "success")
    conn.close()
    return redirect(url_for("groundschool.rating_list"))


@groundschool_bp.route("/<int:rating_id>")
@login_required
def rating_detail(rating_id):
    conn = get_db()
    rating = conn.execute("SELECT * FROM acs_ratings WHERE id = ?", (rating_id,)).fetchone()
    if not rating:
        conn.close()
        abort(404)
    areas = conn.execute("SELECT * FROM acs_areas WHERE rating_id = ? ORDER BY order_index",
                          (rating_id,)).fetchall()
    tasks_by_area = {}
    for area in areas:
        tasks_by_area[area["id"]] = conn.execute(
            "SELECT * FROM acs_tasks WHERE area_id = ? ORDER BY order_index", (area["id"],)).fetchall()
    conn.close()
    return render_template("flight/groundschool_rating.html", rating=rating, areas=areas,
                            tasks_by_area=tasks_by_area)


@groundschool_bp.route("/task/<int:task_id>")
@login_required
def task_detail(task_id):
    conn = get_db()
    task = conn.execute("SELECT * FROM acs_tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        conn.close()
        abort(404)
    area = conn.execute("SELECT * FROM acs_areas WHERE id = ?", (task["area_id"],)).fetchone()
    rating = conn.execute("SELECT * FROM acs_ratings WHERE id = ?", (area["rating_id"],)).fetchone()
    notes = conn.execute("SELECT * FROM acs_task_notes WHERE task_id = ? ORDER BY order_index",
                          (task_id,)).fetchall()
    knowledge = conn.execute(
        "SELECT * FROM acs_task_elements WHERE task_id = ? AND kind = 'knowledge' ORDER BY order_index",
        (task_id,)).fetchall()
    risk_management = conn.execute(
        "SELECT * FROM acs_task_elements WHERE task_id = ? AND kind = 'risk_management' ORDER BY order_index",
        (task_id,)).fetchall()
    skills = conn.execute(
        "SELECT * FROM acs_task_elements WHERE task_id = ? AND kind = 'skills' ORDER BY order_index",
        (task_id,)).fetchall()
    lesson = conn.execute("SELECT * FROM acs_lesson_content WHERE task_id = ?", (task_id,)).fetchone()
    signoffs = conn.execute("""
        SELECT acs_task_signoff.*, students.name AS student_name, cfis.name AS cfi_name
        FROM acs_task_signoff
        JOIN students ON students.id = acs_task_signoff.student_id
        LEFT JOIN cfis ON cfis.id = acs_task_signoff.cfi_id
        WHERE acs_task_signoff.task_id = ?
        ORDER BY acs_task_signoff.signed_off_at DESC
    """, (task_id,)).fetchall()
    my_signoff = None
    if session.get("student_id"):
        my_signoff = conn.execute("SELECT * FROM acs_task_signoff WHERE task_id = ? AND student_id = ?",
                                   (task_id, session["student_id"])).fetchone()
    students = []
    if session.get("cfi_id"):
        students = conn.execute("SELECT id, name FROM students WHERE active = 1 ORDER BY name").fetchall()
    # Sibling tasks in this area, for Prev/Next - and the next task overall
    # (rolling into the next area) so browsing doesn't dead-end at an area
    # boundary.
    area_tasks = conn.execute("SELECT id FROM acs_tasks WHERE area_id = ? ORDER BY order_index",
                               (task["area_id"],)).fetchall()
    conn.close()
    ids = [t["id"] for t in area_tasks]
    idx = ids.index(task_id) if task_id in ids else -1
    prev_task_id = ids[idx - 1] if idx > 0 else None
    next_task_id = ids[idx + 1] if 0 <= idx < len(ids) - 1 else None
    return render_template("flight/groundschool_task.html", task=task, area=area, rating=rating,
                            notes=notes, knowledge=knowledge, risk_management=risk_management,
                            skills=skills, lesson=lesson, signoffs=signoffs, my_signoff=my_signoff,
                            students=students, prev_task_id=prev_task_id, next_task_id=next_task_id)


@groundschool_bp.route("/task/<int:task_id>/lesson", methods=["POST"])
@admin_required
def lesson_save(task_id):
    conn = get_db()
    task = conn.execute("SELECT id FROM acs_tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        conn.close()
        abort(404)
    content = request.form.get("content", "").strip()
    existing = conn.execute("SELECT task_id FROM acs_lesson_content WHERE task_id = ?", (task_id,)).fetchone()
    if existing:
        conn.execute("UPDATE acs_lesson_content SET content = ?, updated_by = ?, updated_at = ? WHERE task_id = ?",
                     (content, session.get("cfi_id"), now_iso(), task_id))
    else:
        conn.execute("INSERT INTO acs_lesson_content (task_id, content, updated_by, updated_at) "
                     "VALUES (?, ?, ?, ?)", (task_id, content, session.get("cfi_id"), now_iso()))
    conn.commit()
    conn.close()
    flash("Lesson content saved.", "success")
    return redirect(url_for("groundschool.task_detail", task_id=task_id))


@groundschool_bp.route("/task/<int:task_id>/signoff", methods=["POST"])
@cfi_required
def signoff_add(task_id):
    conn = get_db()
    task = conn.execute("SELECT id FROM acs_tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        conn.close()
        abort(404)
    student_id = request.form.get("student_id", type=int)
    student = conn.execute("SELECT id, name FROM students WHERE id = ?", (student_id,)).fetchone()
    if not student:
        conn.close()
        flash("Choose a student to sign off.", "danger")
        return redirect(url_for("groundschool.task_detail", task_id=task_id))
    notes = request.form.get("notes", "").strip() or None
    existing = conn.execute("SELECT id FROM acs_task_signoff WHERE task_id = ? AND student_id = ?",
                             (task_id, student_id)).fetchone()
    if existing:
        conn.execute("UPDATE acs_task_signoff SET cfi_id = ?, signed_off_at = ?, notes = ? WHERE id = ?",
                     (session.get("cfi_id"), now_iso(), notes, existing["id"]))
    else:
        conn.execute("INSERT INTO acs_task_signoff (student_id, task_id, cfi_id, signed_off_at, notes) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (student_id, task_id, session.get("cfi_id"), now_iso(), notes))
    conn.commit()
    conn.close()
    flash(f"Signed off {student['name']} on this Task.", "success")
    return redirect(url_for("groundschool.task_detail", task_id=task_id))


@groundschool_bp.route("/task/<int:task_id>/signoff/<int:signoff_id>/remove", methods=["POST"])
@cfi_required
def signoff_remove(task_id, signoff_id):
    conn = get_db()
    conn.execute("DELETE FROM acs_task_signoff WHERE id = ? AND task_id = ?", (signoff_id, task_id))
    conn.commit()
    conn.close()
    flash("Sign-off removed.", "success")
    return redirect(url_for("groundschool.task_detail", task_id=task_id))
