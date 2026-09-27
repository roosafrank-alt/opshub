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

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, session, send_file
from markupsafe import Markup, escape

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


def _save_resource_pdf(file_storage):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    alphabet = string.ascii_lowercase + string.digits
    unique = "".join(random.choices(alphabet, k=12))
    stored_name = f"resource_{unique}.pdf"
    file_storage.save(os.path.join(UPLOAD_DIR, stored_name))
    return stored_name


def _index_resource_pages(conn, resource_id, pdf_path):
    """Extracts each page's text so a citation token can be found later -
    see _find_resource_page(). Best-effort: works the same regardless of the
    document's own section-numbering style, since it's just a text search."""
    import pdfplumber
    # Extract all page text first, without touching the DB - this is the
    # slow, CPU-bound part (can take a minute+ for a large handbook) and
    # must not happen while holding an open write transaction, or every
    # other page/request in the app gets "database is locked" for as long
    # as extraction runs. Only the final write below needs the DB open,
    # and it's a single fast batch insert.
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            pages.append((resource_id, i, page.extract_text() or ""))
    conn.execute("DELETE FROM acs_resource_pages WHERE resource_id = ?", (resource_id,))
    conn.executemany(
        "INSERT INTO acs_resource_pages (resource_id, page_num, text) VALUES (?, ?, ?)", pages)
    conn.commit()
    return len(pages)


_REF_TOKEN_RE = re.compile(
    r"\bAC\s?\d+-\d+[A-Za-z]?\b|\bFAA-[HSG]-\d{4}-\d+[A-Za-z]?\b|\b\d{2}\.\d+[A-Za-z]?\b",
    re.IGNORECASE)


def _find_resource_page(conn, token):
    row = conn.execute(
        "SELECT resource_id, page_num FROM acs_resource_pages WHERE text LIKE ? "
        "ORDER BY resource_id, page_num LIMIT 1",
        (f"%{token}%",)).fetchone()
    return (row["resource_id"], row["page_num"]) if row else None


def _excerpt_pdf_bytes(pdf_path, page_nums):
    """Builds a small standalone PDF of just the given 1-indexed pages, so a
    citation link opens a few-page excerpt instead of the entire source
    document - large FAA handbooks can be hundreds of pages / tens of MB,
    which is slow to open on its own and pointless when only one page is
    relevant."""
    import pypdf
    from io import BytesIO
    reader = pypdf.PdfReader(pdf_path)
    writer = pypdf.PdfWriter()
    n = len(reader.pages)
    for p in page_nums:
        if 1 <= p <= n:
            writer.add_page(reader.pages[p - 1])
    buf = BytesIO()
    writer.write(buf)
    buf.seek(0)
    return buf


# Words too generic to help match an ACS element's text against resource
# page content - filtering these out keeps _find_element_pages() focused on
# the subject-specific terms that actually distinguish one page from another.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "is", "are", "was",
    "were", "be", "by", "as", "that", "this", "it", "at", "from", "which", "will", "shall",
    "not", "may", "student", "applicant", "exhibits", "knowledge", "risk", "management",
    "associated", "including", "appropriate", "their", "these", "those", "have", "has",
}


def _find_element_pages(conn, task, el, limit=2):
    """Best-effort: among the resources this Task's References already cite,
    rank that resource's indexed pages by how many of the element's own
    keywords they contain, and return the top matches. This is a text-
    overlap heuristic (not true content understanding) - it surfaces pages
    worth checking for this specific K/R/S item, rather than just the
    task-level document citation."""
    references_text = task["acs_references"] if task else None
    if not references_text:
        return []
    resource_ids = set()
    for m in _REF_TOKEN_RE.finditer(references_text):
        hit = _find_resource_page(conn, m.group(0))
        if hit:
            resource_ids.add(hit[0])
    if not resource_ids:
        return []
    words = re.findall(r"[a-z]{4,}", (el["text"] or "").lower())
    keywords = [w for w in words if w not in _STOPWORDS][:12]
    if not keywords:
        return []
    placeholders = ",".join("?" * len(resource_ids))
    ids = list(resource_ids)
    pages = conn.execute(
        f"SELECT resource_id, page_num, text FROM acs_resource_pages WHERE resource_id IN ({placeholders})",
        ids).fetchall()
    scored = []
    for p in pages:
        lower = (p["text"] or "").lower()
        score = sum(lower.count(k) for k in keywords)
        if score > 0:
            scored.append((score, p["resource_id"], p["page_num"]))
    scored.sort(key=lambda t: -t[0])
    resource_titles = {r["id"]: r["title"] for r in conn.execute(
        f"SELECT id, title FROM acs_resources WHERE id IN ({placeholders})", ids).fetchall()}
    out = []
    for score, resource_id, page_num in scored:
        if len(out) >= limit:
            break
        out.append({"resource_id": resource_id, "title": resource_titles.get(resource_id, "Resource"),
                     "page_num": page_num})
    return out


def linked_references_html(conn, references_text, task_id=None):
    """Renders a Task's References string with any recognized citation
    linked to the uploaded Resource's complete document (best-effort text
    search - see _index_resource_pages) - the whole document, not just the
    page it was found on, since a Task's own References is the "go read the
    real source" link (see task_reference_click/task_mark_read for the
    student's per-task reading tracker); an element's own "Related Reading"
    still links to a short excerpt around the matched page instead (see
    _find_element_pages/element_detail.html), since that's meant as a quick
    pointer to the relevant spot, not the whole reading assignment.
    Unmatched tokens render as plain text. task_id, when given, tags each
    link so the page's JS can mark the reading opened once a student clicks
    one (class="task-ref-link" data-task-id)."""
    if not references_text:
        return Markup("")
    out = []
    pos = 0
    link_attrs = f' class="task-ref-link" data-task-id="{task_id}"' if task_id else ""
    for m in _REF_TOKEN_RE.finditer(references_text):
        out.append(escape(references_text[pos:m.start()]))
        token = m.group(0)
        hit = _find_resource_page(conn, token)
        if hit:
            resource_id, _page_num = hit
            href = url_for("groundschool.resource_file", resource_id=resource_id)
            out.append(Markup('<a href="%s" target="_blank" rel="noopener"%s>%s</a>') % (href, Markup(link_attrs), token))
        else:
            out.append(escape(token))
        pos = m.end()
    out.append(escape(references_text[pos:]))
    return Markup("").join(out)


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
# Progress
# ---------------------------------------------------------------------------

def _rating_progress(conn, rating_id, student_id):
    rows = conn.execute("""
        SELECT a.id AS area_id, t.id AS task_id,
               CASE WHEN c.self_completed_at IS NOT NULL AND c.cfi_verified_at IS NOT NULL
                    THEN 1 ELSE 0 END AS done
        FROM acs_areas a
        JOIN acs_tasks t ON t.area_id = a.id
        JOIN acs_task_elements e ON e.task_id = t.id
        LEFT JOIN acs_element_completion c ON c.element_id = e.id AND c.student_id = ?
        WHERE a.rating_id = ?
    """, (student_id, rating_id)).fetchall()
    areas = {}
    total = done = 0
    for r in rows:
        a = areas.setdefault(r["area_id"], {"done": 0, "total": 0, "tasks": {}})
        t = a["tasks"].setdefault(r["task_id"], {"done": 0, "total": 0})
        a["total"] += 1
        t["total"] += 1
        total += 1
        if r["done"]:
            a["done"] += 1
            t["done"] += 1
            done += 1
    for a in areas.values():
        a["pct"] = round(100 * a["done"] / a["total"]) if a["total"] else 0
        for t in a["tasks"].values():
            t["pct"] = round(100 * t["done"] / t["total"]) if t["total"] else 0
    return {"done": done, "total": total, "pct": round(100 * done / total) if total else 0, "areas": areas}


def _element_done_map(conn, rating_id, student_id):
    rows = conn.execute("""
        SELECT e.id AS element_id,
               CASE WHEN c.self_completed_at IS NOT NULL AND c.cfi_verified_at IS NOT NULL
                    THEN 1 ELSE 0 END AS done
        FROM acs_areas a
        JOIN acs_tasks t ON t.area_id = a.id
        JOIN acs_task_elements e ON e.task_id = t.id
        LEFT JOIN acs_element_completion c ON c.element_id = e.id AND c.student_id = ?
        WHERE a.rating_id = ?
    """, (student_id, rating_id)).fetchall()
    return {r["element_id"]: bool(r["done"]) for r in rows}


def _resolve_viewing_student(conn):
    if session.get("student_id"):
        return session["student_id"], []
    if session.get("cfi_id") or session.get("is_master_admin"):
        students = conn.execute("SELECT id, name FROM students WHERE active = 1 ORDER BY name").fetchall()
        return request.args.get("student_id", type=int), students
    return None, []


def _check_self_complete(conn, student_id, element_id):
    items = conn.execute("SELECT id, required FROM acs_element_lesson_items WHERE element_id = ?",
                          (element_id,)).fetchall()
    required_ids = [it["id"] for it in items if it["required"]]
    if required_ids:
        placeholders = ",".join("?" * len(required_ids))
        done_count = conn.execute(
            f"SELECT COUNT(*) AS c FROM acs_element_item_progress WHERE student_id = ? AND item_id IN ({placeholders})",
            [student_id] + required_ids).fetchone()["c"]
        done = done_count == len(required_ids)
    else:
        done = True
    existing = conn.execute("SELECT * FROM acs_element_completion WHERE student_id = ? AND element_id = ?",
                             (student_id, element_id)).fetchone()
    if done and not (existing and existing["self_completed_at"]):
        if existing:
            conn.execute("UPDATE acs_element_completion SET self_completed_at = ? "
                         "WHERE student_id = ? AND element_id = ?", (now_iso(), student_id, element_id))
        else:
            conn.execute("INSERT INTO acs_element_completion (student_id, element_id, self_completed_at) "
                         "VALUES (?, ?, ?)", (student_id, element_id, now_iso()))
        conn.commit()
    elif not done and existing and existing["self_completed_at"]:
        conn.execute("UPDATE acs_element_completion SET self_completed_at = NULL "
                     "WHERE student_id = ? AND element_id = ?", (student_id, element_id))
        conn.commit()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@groundschool_bp.route("")
@login_required
def rating_list():
    conn = get_db()
    ratings = conn.execute("SELECT * FROM acs_ratings ORDER BY name").fetchall()
    viewing_student_id, students = _resolve_viewing_student(conn)
    progress_by_rating = {}
    if viewing_student_id:
        for r in ratings:
            if r["status"] == "parsed":
                progress_by_rating[r["id"]] = _rating_progress(conn, r["id"], viewing_student_id)
    conn.close()
    return render_template("flight/groundschool_list.html", ratings=ratings, tab="groundschool",
                            progress_by_rating=progress_by_rating, students=students,
                            viewing_student_id=viewing_student_id)


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
    elements_by_task = {}
    references_html_by_task = {}
    for area in areas:
        tasks = conn.execute(
            "SELECT * FROM acs_tasks WHERE area_id = ? ORDER BY order_index", (area["id"],)).fetchall()
        tasks_by_area[area["id"]] = tasks
        for task in tasks:
            elements_by_task[task["id"]] = {
                "knowledge": conn.execute(
                    "SELECT * FROM acs_task_elements WHERE task_id = ? AND kind = 'knowledge' ORDER BY order_index",
                    (task["id"],)).fetchall(),
                "risk_management": conn.execute(
                    "SELECT * FROM acs_task_elements WHERE task_id = ? AND kind = 'risk_management' ORDER BY order_index",
                    (task["id"],)).fetchall(),
                "skills": conn.execute(
                    "SELECT * FROM acs_task_elements WHERE task_id = ? AND kind = 'skills' ORDER BY order_index",
                    (task["id"],)).fetchall(),
            }
            references_html_by_task[task["id"]] = linked_references_html(conn, task["acs_references"], task_id=task["id"])
    viewing_student_id, students = _resolve_viewing_student(conn)
    progress = _rating_progress(conn, rating_id, viewing_student_id) if viewing_student_id else None
    element_done = _element_done_map(conn, rating_id, viewing_student_id) if viewing_student_id else {}
    conn.close()
    return render_template("flight/groundschool_rating.html", rating=rating, areas=areas,
                            tasks_by_area=tasks_by_area, elements_by_task=elements_by_task,
                            references_html_by_task=references_html_by_task, tab="groundschool",
                            students=students, viewing_student_id=viewing_student_id,
                            progress=progress, element_done=element_done)


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
    references_html = linked_references_html(conn, task["acs_references"], task_id=task_id)
    viewing_student_id, _ = _resolve_viewing_student(conn)
    element_ids = [e["id"] for e in knowledge] + [e["id"] for e in risk_management] + [e["id"] for e in skills]
    element_done = {}
    if viewing_student_id and element_ids:
        placeholders = ",".join("?" * len(element_ids))
        rows = conn.execute(
            f"SELECT element_id, self_completed_at, cfi_verified_at FROM acs_element_completion "
            f"WHERE student_id = ? AND element_id IN ({placeholders})",
            [viewing_student_id] + element_ids).fetchall()
        element_done = {r["element_id"]: bool(r["self_completed_at"] and r["cfi_verified_at"]) for r in rows}
    task_done = sum(1 for v in element_done.values() if v)
    task_total = len(element_ids)
    reading_progress = None
    if viewing_student_id:
        reading_progress = conn.execute("""
            SELECT acs_task_reading_progress.*, cfis.name AS cfi_name
            FROM acs_task_reading_progress
            LEFT JOIN cfis ON cfis.id = acs_task_reading_progress.cfi_id
            WHERE acs_task_reading_progress.student_id = ? AND acs_task_reading_progress.task_id = ?
        """, (viewing_student_id, task_id)).fetchone()
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
                            students=students, prev_task_id=prev_task_id, next_task_id=next_task_id,
                            tab="groundschool", references_html=references_html,
                            element_done=element_done, task_done=task_done, task_total=task_total,
                            reading_progress=reading_progress, viewing_student_id=viewing_student_id)


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


# ---------------------------------------------------------------------------
# Resources library
# ---------------------------------------------------------------------------

@groundschool_bp.route("/resources")
@login_required
def resources_list():
    conn = get_db()
    resources = conn.execute(
        "SELECT r.*, (SELECT COUNT(*) FROM acs_resource_pages p WHERE p.resource_id = r.id) AS page_count "
        "FROM acs_resources r ORDER BY r.doc_type, r.title").fetchall()
    conn.close()
    return render_template("flight/groundschool_resources.html", resources=resources, tab="resources")


@groundschool_bp.route("/resources/upload", methods=["POST"])
@admin_required
def resources_upload():
    f = request.files.get("resource_file")
    title = request.form.get("title", "").strip()
    doc_type = request.form.get("doc_type", "").strip() or None
    if not f or not f.filename or not f.filename.lower().endswith(".pdf"):
        flash("Choose a PDF to upload.", "danger")
        return redirect(url_for("groundschool.resources_list"))
    if not title:
        title = f.filename.rsplit(".", 1)[0]
    stored_name = _save_resource_pdf(f)
    pdf_path = os.path.join(UPLOAD_DIR, stored_name)
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO acs_resources (title, doc_type, filename, uploaded_by, uploaded_at) VALUES (?, ?, ?, ?, ?)",
        (title, doc_type, stored_name, session.get("cfi_id"), now_iso()))
    resource_id = cur.lastrowid
    conn.commit()
    try:
        n_pages = _index_resource_pages(conn, resource_id, pdf_path)
        flash(f"Uploaded '{title}' - indexed {n_pages} pages, so matching References will auto-link to it.",
              "success")
    except Exception as e:
        flash(f"Uploaded '{title}', but page indexing failed ({e}) - it's still available to open, "
              "just not auto-linked from References yet.", "warning")
    conn.close()
    return redirect(url_for("groundschool.resources_list"))


@groundschool_bp.route("/resources/<int:resource_id>/delete", methods=["POST"])
@admin_required
def resources_delete(resource_id):
    conn = get_db()
    r = conn.execute("SELECT * FROM acs_resources WHERE id = ?", (resource_id,)).fetchone()
    if r:
        conn.execute("DELETE FROM acs_resource_pages WHERE resource_id = ?", (resource_id,))
        conn.execute("DELETE FROM acs_resources WHERE id = ?", (resource_id,))
        conn.commit()
        try:
            os.remove(os.path.join(UPLOAD_DIR, r["filename"]))
        except OSError:
            pass
        flash(f"Deleted '{r['title']}'.", "success")
    conn.close()
    return redirect(url_for("groundschool.resources_list"))


@groundschool_bp.route("/resources/<int:resource_id>/file")
@login_required
def resource_file(resource_id):
    conn = get_db()
    r = conn.execute("SELECT * FROM acs_resources WHERE id = ?", (resource_id,)).fetchone()
    conn.close()
    if not r:
        abort(404)
    return send_file(os.path.join(UPLOAD_DIR, r["filename"]), mimetype="application/pdf")


@groundschool_bp.route("/resources/<int:resource_id>/excerpt")
@login_required
def resource_excerpt(resource_id):
    """Serves just a few pages of a resource as a small standalone PDF,
    instead of the whole (often huge) document - used by every auto-linked
    citation so opening one is fast regardless of how big the source
    handbook is."""
    conn = get_db()
    r = conn.execute("SELECT * FROM acs_resources WHERE id = ?", (resource_id,)).fetchone()
    conn.close()
    if not r:
        abort(404)
    raw = request.args.get("pages", "")
    try:
        pages = sorted({int(p) for p in raw.split(",") if p.strip()})
    except ValueError:
        abort(400)
    if not pages:
        abort(400)
    pages = pages[:5]
    pdf_path = os.path.join(UPLOAD_DIR, r["filename"])
    try:
        buf = _excerpt_pdf_bytes(pdf_path, pages)
    except Exception:
        abort(500)
    return send_file(buf, mimetype="application/pdf",
                      download_name=f"{_slugify(r['title'])}-p{pages[0]}.pdf")


# ---------------------------------------------------------------------------
# Element lessons
# ---------------------------------------------------------------------------

@groundschool_bp.route("/element/<int:element_id>")
@login_required
def element_detail(element_id):
    conn = get_db()
    el = conn.execute("SELECT * FROM acs_task_elements WHERE id = ?", (element_id,)).fetchone()
    if not el:
        conn.close()
        abort(404)
    task = conn.execute("SELECT * FROM acs_tasks WHERE id = ?", (el["task_id"],)).fetchone()
    area = conn.execute("SELECT * FROM acs_areas WHERE id = ?", (task["area_id"],)).fetchone()
    rating = conn.execute("SELECT * FROM acs_ratings WHERE id = ?", (area["rating_id"],)).fetchone()
    items = conn.execute("SELECT * FROM acs_element_lesson_items WHERE element_id = ? ORDER BY order_index",
                          (element_id,)).fetchall()
    viewing_student_id, students = _resolve_viewing_student(conn)
    done_item_ids = set()
    completion = None
    if viewing_student_id:
        if items:
            placeholders = ",".join("?" * len(items))
            done_item_ids = {r["item_id"] for r in conn.execute(
                f"SELECT item_id FROM acs_element_item_progress WHERE student_id = ? AND item_id IN ({placeholders})",
                [viewing_student_id] + [it["id"] for it in items]).fetchall()}
        completion = conn.execute(
            "SELECT * FROM acs_element_completion WHERE student_id = ? AND element_id = ?",
            (viewing_student_id, element_id)).fetchone()
    required_ids = [it["id"] for it in items if it["required"]]
    all_required_done = all(i in done_item_ids for i in required_ids) if required_ids else True
    related_pages = _find_element_pages(conn, task, el)
    conn.close()
    return render_template("flight/groundschool_element.html", el=el, task=task, area=area, rating=rating,
                            items=items, done_item_ids=done_item_ids, completion=completion,
                            students=students, viewing_student_id=viewing_student_id,
                            all_required_done=all_required_done, related_pages=related_pages,
                            tab="groundschool")


@groundschool_bp.route("/element/<int:element_id>/item/add", methods=["POST"])
@admin_required
def element_item_add(element_id):
    conn = get_db()
    el = conn.execute("SELECT id FROM acs_task_elements WHERE id = ?", (element_id,)).fetchone()
    if not el:
        conn.close()
        abort(404)
    kind = request.form.get("kind", "text")
    if kind not in ("text", "link", "video"):
        kind = "text"
    title = request.form.get("title", "").strip() or None
    body = request.form.get("body", "").strip() or None
    url_val = request.form.get("url", "").strip() or None
    required = 1 if request.form.get("required") and kind != "text" else 0
    max_idx = conn.execute(
        "SELECT COALESCE(MAX(order_index), -1) AS m FROM acs_element_lesson_items WHERE element_id = ?",
        (element_id,)).fetchone()["m"]
    conn.execute("INSERT INTO acs_element_lesson_items (element_id, order_index, kind, title, body, url, required) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)", (element_id, max_idx + 1, kind, title, body, url_val, required))
    conn.commit()
    conn.close()
    flash("Lesson item added.", "success")
    return redirect(url_for("groundschool.element_detail", element_id=element_id))


@groundschool_bp.route("/element/<int:element_id>/item/<int:item_id>/delete", methods=["POST"])
@admin_required
def element_item_delete(element_id, item_id):
    conn = get_db()
    conn.execute("DELETE FROM acs_element_item_progress WHERE item_id = ?", (item_id,))
    conn.execute("DELETE FROM acs_element_lesson_items WHERE id = ? AND element_id = ?", (item_id, element_id))
    conn.commit()
    conn.close()
    flash("Lesson item removed.", "success")
    return redirect(url_for("groundschool.element_detail", element_id=element_id))


@groundschool_bp.route("/task/<int:task_id>/reference-click", methods=["POST"])
@login_required
def task_reference_click(task_id):
    """Records that this student opened one of the Task's own References
    links (the full source document, not an element's short excerpt) - the
    prerequisite task_mark_read checks for before letting them mark the
    Task's reading as done."""
    student_id = session.get("student_id")
    if not student_id:
        return {"ok": False, "error": "Only a student's own reading progress is tracked."}, 403
    conn = get_db()
    existing = conn.execute("SELECT 1 FROM acs_task_reading_progress WHERE student_id = ? AND task_id = ?",
                             (student_id, task_id)).fetchone()
    if existing:
        conn.execute("UPDATE acs_task_reading_progress SET reference_opened_at = ? "
                     "WHERE student_id = ? AND task_id = ? AND reference_opened_at IS NULL",
                     (now_iso(), student_id, task_id))
    else:
        conn.execute("INSERT INTO acs_task_reading_progress (student_id, task_id, reference_opened_at) "
                     "VALUES (?, ?, ?)", (student_id, task_id, now_iso()))
    conn.commit()
    conn.close()
    return {"ok": True}


@groundschool_bp.route("/task/<int:task_id>/mark-read", methods=["POST"])
@login_required
def task_mark_read(task_id):
    """A student marks a Task's reading as done - only allowed once they've
    actually opened one of its Reference links (task_reference_click),
    checked server-side too since the button's disabled state is just UI."""
    student_id = session.get("student_id")
    if not student_id:
        flash("Only a student can mark their own reading as done.", "danger")
        return redirect(url_for("groundschool.task_detail", task_id=task_id))
    conn = get_db()
    progress = conn.execute("SELECT * FROM acs_task_reading_progress WHERE student_id = ? AND task_id = ?",
                             (student_id, task_id)).fetchone()
    if not progress or not progress["reference_opened_at"]:
        conn.close()
        flash("Open one of the References links first so you've actually read it.", "danger")
        return redirect(url_for("groundschool.task_detail", task_id=task_id))
    conn.execute("UPDATE acs_task_reading_progress SET marked_read_at = ? WHERE student_id = ? AND task_id = ?",
                 (now_iso(), student_id, task_id))
    conn.commit()
    conn.close()
    flash("Marked as read.", "success")
    return redirect(url_for("groundschool.task_detail", task_id=task_id))


@groundschool_bp.route("/task/<int:task_id>/verify-reading", methods=["POST"])
@cfi_required
def task_verify_reading(task_id):
    """A CFI checking off that they've reviewed this Task's knowledge area
    with the student - only shown once the student has checked their own
    Mark as Read box (see groundschool_task.html), same self-report-then-CFI-
    verify shape as acs_element_completion's self_completed_at/cfi_verified_at."""
    student_id = request.form.get("student_id", type=int)
    conn = get_db()
    student = conn.execute("SELECT id, name FROM students WHERE id = ?", (student_id,)).fetchone()
    if not student:
        conn.close()
        flash("Choose a student to verify.", "danger")
        return redirect(url_for("groundschool.task_detail", task_id=task_id))
    progress = conn.execute("SELECT * FROM acs_task_reading_progress WHERE student_id = ? AND task_id = ?",
                             (student_id, task_id)).fetchone()
    if not progress or not progress["marked_read_at"]:
        conn.close()
        flash(f"{student['name']} hasn't marked this Task's reading as done yet.", "danger")
        return redirect(url_for("groundschool.task_detail", task_id=task_id))
    conn.execute("UPDATE acs_task_reading_progress SET cfi_id = ?, cfi_verified_at = ? "
                 "WHERE student_id = ? AND task_id = ?",
                 (session.get("cfi_id"), now_iso(), student_id, task_id))
    conn.commit()
    conn.close()
    flash(f"Reviewed this Task's knowledge area with {student['name']}.", "success")
    return redirect(url_for("groundschool.task_detail", task_id=task_id))


@groundschool_bp.route("/element/<int:element_id>/item/<int:item_id>/interact", methods=["POST"])
@login_required
def element_item_interact(element_id, item_id):
    student_id = session.get("student_id")
    if not student_id:
        return {"ok": False, "error": "Only a student's own lesson progress is tracked."}, 403
    conn = get_db()
    item = conn.execute("SELECT * FROM acs_element_lesson_items WHERE id = ? AND element_id = ?",
                         (item_id, element_id)).fetchone()
    if not item:
        conn.close()
        return {"ok": False, "error": "not found"}, 404
    existing = conn.execute("SELECT 1 FROM acs_element_item_progress WHERE student_id = ? AND item_id = ?",
                             (student_id, item_id)).fetchone()
    if not existing:
        conn.execute("INSERT INTO acs_element_item_progress (student_id, item_id, done_at) VALUES (?, ?, ?)",
                     (student_id, item_id, now_iso()))
        conn.commit()
    _check_self_complete(conn, student_id, element_id)
    completion = conn.execute("SELECT * FROM acs_element_completion WHERE student_id = ? AND element_id = ?",
                               (student_id, element_id)).fetchone()
    conn.close()
    return {"ok": True, "self_completed": bool(completion and completion["self_completed_at"])}


@groundschool_bp.route("/element/<int:element_id>/verify", methods=["POST"])
@cfi_required
def element_verify(element_id):
    student_id = request.form.get("student_id", type=int)
    conn = get_db()
    student = conn.execute("SELECT id, name FROM students WHERE id = ?", (student_id,)).fetchone() \
        if student_id else None
    if not student:
        conn.close()
        flash("Choose a student to verify.", "danger")
        return redirect(url_for("groundschool.element_detail", element_id=element_id))
    existing = conn.execute("SELECT * FROM acs_element_completion WHERE student_id = ? AND element_id = ?",
                             (student_id, element_id)).fetchone()
    if existing:
        conn.execute("UPDATE acs_element_completion SET cfi_id = ?, cfi_verified_at = ? "
                     "WHERE student_id = ? AND element_id = ?",
                     (session.get("cfi_id"), now_iso(), student_id, element_id))
    else:
        conn.execute("INSERT INTO acs_element_completion (student_id, element_id, cfi_id, cfi_verified_at) "
                     "VALUES (?, ?, ?, ?)", (student_id, element_id, session.get("cfi_id"), now_iso()))
    conn.commit()
    conn.close()
    flash(f"Verified {student['name']} on this element.", "success")
    return redirect(url_for("groundschool.element_detail", element_id=element_id, student_id=student_id))
