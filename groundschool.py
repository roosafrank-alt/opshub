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
import shutil
import threading
from datetime import date, datetime, timedelta, timezone

import acs_sync
import resource_scan
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, session, send_file
from markupsafe import Markup, escape

from db import get_db, now_iso, UPLOAD_DIR
import ground_lessons
from flight import login_required, cfi_required, admin_required
from auth import student_course_grants, groundschool_restricted, is_flight_admin
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
    """SCHOOL-07: an element counts as done once a CFI has verified it; the
    student's own self-completion is shown as information, not a gate."""
    rows = conn.execute("""
        SELECT a.id AS area_id, t.id AS task_id,
               CASE WHEN c.cfi_verified_at IS NOT NULL THEN 1 ELSE 0 END AS done
        FROM acs_areas a
        JOIN acs_tasks t ON t.area_id = a.id
        JOIN acs_task_elements e ON e.task_id = t.id
        LEFT JOIN acs_element_completion c ON c.element_id = e.id AND c.student_id = ?
        WHERE a.rating_id = ? AND t.retired_at IS NULL AND e.retired_at IS NULL
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
               CASE WHEN c.cfi_verified_at IS NOT NULL THEN 1 ELSE 0 END AS done
        FROM acs_areas a
        JOIN acs_tasks t ON t.area_id = a.id
        JOIN acs_task_elements e ON e.task_id = t.id
        LEFT JOIN acs_element_completion c ON c.element_id = e.id AND c.student_id = ?
        WHERE a.rating_id = ? AND t.retired_at IS NULL AND e.retired_at IS NULL
    """, (student_id, rating_id)).fetchall()
    return {r["element_id"]: bool(r["done"]) for r in rows}


def _element_self_done(conn, student_id, element_ids):
    """GS-LESSONS: elements the student finished but a CFI hasn't verified yet (shown as a
    green outline with a check; CFI-verified stays solid green)."""
    if not student_id or not element_ids:
        return set()
    ids = list(element_ids)
    ph = ",".join("?" * len(ids))
    return {r["element_id"] for r in conn.execute(
        f"SELECT element_id FROM acs_element_completion WHERE student_id = ? AND element_id IN ({ph}) "
        f"AND self_completed_at IS NOT NULL AND cfi_verified_at IS NULL", [student_id] + ids).fetchall()}


def next_lesson(conn, rating_id, student_id):
    """Where a student picks up again in a course: the first task (in course
    order) with an element they have not finished (CFI-verified or marked done
    by themselves). Returns None when the course has no tasks, else a dict with
    task_id, label ("Task A. Pilot Qualifications"), next_up (first unfinished
    element, "PA.I.A.K4, Documents you must have"), done/total tasks, pct and
    finished (every task done; then task_id is the course's first task)."""
    rows = conn.execute("""
        SELECT t.id AS task_id, t.code AS task_code, t.title AS task_title,
               e.code AS el_code, e.text AS el_text,
               CASE WHEN c.cfi_verified_at IS NOT NULL OR c.self_completed_at IS NOT NULL THEN 1 ELSE 0 END AS done
        FROM acs_areas a
        JOIN acs_tasks t ON t.area_id = a.id
        JOIN acs_task_elements e ON e.task_id = t.id
        LEFT JOIN acs_element_completion c ON c.element_id = e.id AND c.student_id = ?
        WHERE a.rating_id = ? AND t.retired_at IS NULL AND e.retired_at IS NULL
        ORDER BY a.order_index, t.order_index, e.kind, e.order_index
    """, (student_id, rating_id)).fetchall()
    tasks = {}
    for r in rows:
        t = tasks.setdefault(r["task_id"], {"row": r, "open": None})
        if not r["done"] and t["open"] is None:
            t["open"] = r
    if not tasks:
        return None
    done = sum(1 for t in tasks.values() if t["open"] is None)
    pick = next((t for t in tasks.values() if t["open"] is not None), None)
    first = pick or next(iter(tasks.values()))
    r = first["row"]
    el = pick["open"] if pick else None
    text = (el["el_text"] or "").strip().split("\n")[0] if el else ""
    if len(text) > 60:
        text = text[:57].rstrip() + "..."
    total = len(tasks)
    return {"task_id": r["task_id"], "label": f"Task {r['task_code']}. {r['task_title']}",
            "next_up": (f"{el['el_code']}, {text}" if text else el["el_code"]) if el else "",
            "done": done, "total": total, "pct": round(100 * done / total), "finished": pick is None}


GS_STUDENT_KEY = "gs_student_id"


def _resolve_viewing_student(conn):
    """SCHOOL-08: whose progress the Ground School pages show. A student
    always sees themselves (no picker). Staff get ONE picker (rendered by
    flight/_groundschool_picker.html at the top of every Ground School
    page) whose choice is remembered in the session, so Prev / Next, the
    back links and every redirect keep it. ?student_id= (a link or the
    picker) sets it; ?student_id=0 clears it."""
    if session.get("student_id"):
        return session["student_id"], []
    if session.get("cfi_id") or is_flight_admin():
        students = conn.execute("SELECT id, name FROM students WHERE active = 1 AND COALESCE(is_station, 0) = 0 ORDER BY name").fetchall()
        if "student_id" in request.args:
            sid = request.args.get("student_id", type=int) or None
            session[GS_STUDENT_KEY] = sid
        else:
            sid = session.get(GS_STUDENT_KEY)
        if sid and not any(st["id"] == sid for st in students):
            sid = None
        return sid, students
    return None, []


def _student_url(endpoint, **values):
    """url_for that carries the chosen student (staff only) - SCHOOL-08."""
    if not session.get("student_id") and session.get(GS_STUDENT_KEY):
        values.setdefault("student_id", session[GS_STUDENT_KEY])
    return url_for(endpoint, **values)


groundschool_bp.add_app_template_global(_student_url, "gs_url")
# GS-LESSONS: lesson plan helpers for templates/flight/_ground_lesson.html
groundschool_bp.add_app_template_global(ground_lessons.rich, "gs_rich")
groundschool_bp.add_app_template_global(ground_lessons.cite_url, "gs_cite_url")
groundschool_bp.add_app_template_global(ground_lessons.references, "gs_refs")
groundschool_bp.add_app_template_global(ground_lessons.element_kind, "gs_kind")


def _gone(what):
    """SCHOOL-40: a stale link to a deleted rating / task / element gets a
    message and the Ground School list, not a bare Not Found page."""
    flash(f"That {what} is no longer here.", "warning")
    return redirect(url_for("groundschool.rating_list"))


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

@groundschool_bp.before_request
def _require_groundschool_access():
    """Ground School (lesson plans & resources) used to be reachable by any
    logged-in flight account that typed the address - the only links to it
    were the Flight Academy tabs, which most CFIs/students don't have. Now
    that there's a Ground School button in the Fly with Kate! menu (see
    base_flight.html), it needs the same admin-assigned gate Flight Academy
    already has, same as academy_page() in app.py."""
    if not session.get("user_id"):
        return None
    if not (is_flight_admin() or session.get("groundschool_access") or student_course_grants()):
        if session.get("academy_access") and (request.endpoint or "").split(".")[-1] in _RESOURCE_VIEW_ENDPOINTS:
            return None  # Flight Academy accounts keep the Resources library
        flash("You don't have access to Ground School yet. Ask an admin.", "danger")
        return redirect(url_for("flight.dashboard"))
    if groundschool_restricted():
        # A student who only has granted courses can read those and use the
        # reading-progress actions - nothing else, whatever the route's own
        # decorator would say.
        endpoint = (request.endpoint or "").split(".")[-1]
        if endpoint not in _GRANTED_STUDENT_ENDPOINTS:
            return _not_granted()
    return None


_RESOURCE_VIEW_ENDPOINTS = {"resources_list", "resource_file", "resource_excerpt", "resource_previous"}

_GRANTED_STUDENT_ENDPOINTS = {
    "rating_list", "rating_detail", "task_detail", "element_detail", "task_reference_click",
    "task_mark_read", "element_item_interact", "element_lesson_done", "resource_file", "resource_excerpt", "resources_list", "resource_previous",
}


def _not_granted():
    flash("That course isn't one of your Flight Academy courses. Ask an admin.", "danger")
    return redirect(url_for("groundschool.rating_list"))


def _course_blocked(rating_id):
    """True when the signed-in student may not open this course (granted
    courses only apply to a student limited to them; everyone else is never
    blocked here)."""
    return groundschool_restricted() and rating_id not in student_course_grants()


def _rating_id_of_task(conn, task_id):
    row = conn.execute("SELECT a.rating_id FROM acs_tasks t JOIN acs_areas a ON a.id = t.area_id "
                       "WHERE t.id = ?", (task_id,)).fetchone()
    return row["rating_id"] if row else None


def _rating_id_of_element(conn, element_id):
    row = conn.execute("SELECT a.rating_id FROM acs_task_elements e JOIN acs_tasks t ON t.id = e.task_id "
                       "JOIN acs_areas a ON a.id = t.area_id WHERE e.id = ?", (element_id,)).fetchone()
    return row["rating_id"] if row else None


def _granted_resource_ids(conn):
    """Resources a granted course cites in its tasks' References."""
    ids = set()
    seen = set()
    for rating_id in student_course_grants():
        for t in conn.execute("SELECT t.acs_references FROM acs_tasks t JOIN acs_areas a ON a.id = t.area_id "
                              "WHERE a.rating_id = ?", (rating_id,)).fetchall():
            for m in _REF_TOKEN_RE.finditer(t["acs_references"] or ""):
                token = m.group(0)
                if token in seen:
                    continue
                seen.add(token)
                hit = _find_resource_page(conn, token)
                if hit:
                    ids.add(hit[0])
    return ids


def _sync_acs_resources(conn):
    """ACS-RESOURCES: every ACS course also has a row in Resources (same PDF file), with the FAA's own
    address as its source when we know it, so the normal update check watches it. Safe to call often:
    it only adds what is missing. Page text is indexed in the background (it is slow on the Pi)."""
    added = []
    for rt in conn.execute("SELECT * FROM acs_ratings WHERE pdf_filename IS NOT NULL").fetchall():
        res = conn.execute("SELECT * FROM acs_resources WHERE acs_rating_id = ?", (rt["id"],)).fetchone()
        if not res:
            rid = conn.execute(
                "INSERT INTO acs_resources (title, doc_type, filename, uploaded_by, uploaded_at, designation, "
                "categories, acs_rating_id) VALUES (?, 'ACS', ?, ?, ?, ?, ?, ?)",
                (rt["name"], rt["pdf_filename"], rt["uploaded_by"], rt["uploaded_at"] or now_iso(),
                 rt["doc_number"], acs_sync.categories_for(rt["name"]), rt["id"])).lastrowid
            added.append((rid, rt["pdf_filename"]))
        else:
            rid = res["id"]
        if not conn.execute("SELECT 1 FROM resource_sources WHERE resource_id = ?", (rid,)).fetchone():
            url = acs_sync.default_source_url(rt["name"], rt["doc_number"])
            if url:
                conn.execute("INSERT INTO resource_sources (resource_id, source_url, source_kind) VALUES (?, ?, ?)",
                             (rid, url, resource_scan.detect_kind(url)))
    conn.commit()
    todo = list(added) + [(r["id"], r["filename"]) for r in conn.execute(
        "SELECT r.id, r.filename FROM acs_resources r WHERE r.acs_rating_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM acs_resource_pages p WHERE p.resource_id = r.id)").fetchall() if (r["id"], r["filename"]) not in added]
    todo = [(rid, fn) for rid, fn in todo
            if os.path.isfile(os.path.join(UPLOAD_DIR, fn)) and rid not in _INDEXING]
    if todo:
        _INDEXING.update(rid for rid, _fn in todo)
        if SCAN_ASYNC:
            threading.Thread(target=_index_acs_resources, args=(todo,), daemon=True).start()
        else:
            _index_acs_resources(todo)
    return len(added)


_INDEXING = set()   # Resources rows whose page text is being indexed right now


def _index_acs_resources(todo):
    conn = get_db()
    try:
        for rid, filename in todo:
            try:
                _index_resource_pages(conn, rid, os.path.join(UPLOAD_DIR, filename))
            except Exception as e:
                print(f"ACS page indexing failed for resource {rid}: {e}")
            finally:
                _INDEXING.discard(rid)
    finally:
        conn.close()


@groundschool_bp.route("")
@login_required
def rating_list():
    conn = get_db()
    ratings = conn.execute("SELECT * FROM acs_ratings ORDER BY name").fetchall()
    if groundschool_restricted():
        granted = student_course_grants()
        ratings = [r for r in ratings if r["id"] in granted]
    viewing_student_id, students = _resolve_viewing_student(conn)
    progress_by_rating = {}
    next_by_rating = {}
    if viewing_student_id:
        for r in ratings:
            if r["status"] == "parsed":
                progress_by_rating[r["id"]] = _rating_progress(conn, r["id"], viewing_student_id)
                next_by_rating[r["id"]] = next_lesson(conn, r["id"], viewing_student_id)
    conn.close()
    return render_template("flight/groundschool_list.html", ratings=ratings, tab="groundschool",
                            progress_by_rating=progress_by_rating, next_by_rating=next_by_rating, gs_students=students,
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
        _sync_acs_resources(conn)
        flash(f"Uploaded '{name}' - found {n_areas} Areas of Operation, {n_tasks} Tasks, "
              f"{n_elements} Knowledge/Risk Management/Skills elements. It is also in Resources now, "
              "where new FAA versions will be queued for you.", "success")
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
        # ACS-RESOURCES: merge in place (same ids), so lessons and sign-offs are kept.
        areas = acs_sync.parse_pdf(pdf_path)
        report = acs_sync.apply_acs(conn, rating_id, areas)
        conn.execute("UPDATE acs_ratings SET status = 'parsed', parsed_at = ?, parse_error = NULL WHERE id = ?",
                     (now_iso(), rating_id))
        conn.commit()
        n_changed = len(report["tasks"]) + len(report["added_tasks"]) + len(report["removed_tasks"])
        flash(f"Re-read '{rating['name']}'. {n_changed} Task{'s' if n_changed != 1 else ''} changed. "
              "Lesson content and sign-offs were kept.", "success")
    except Exception as e:
        conn.rollback()
        # The course keeps working as it was; only the note is saved.
        conn.execute("UPDATE acs_ratings SET parse_error = ? WHERE id = ?", (str(e), rating_id))
        conn.commit()
        flash(f"Re-read failed, nothing was changed: {e}", "danger")
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
        acs_sync.purge_tasks(conn, task_ids)
        conn.execute("DELETE FROM academy_course_grants WHERE rating_id = ?", (rating_id,))
        conn.execute("DELETE FROM acs_areas WHERE rating_id = ?", (rating_id,))
        conn.execute("DELETE FROM acs_task_changes WHERE rating_id = ?", (rating_id,))
        for res in conn.execute("SELECT id FROM acs_resources WHERE acs_rating_id = ?", (rating_id,)).fetchall():
            conn.execute("DELETE FROM acs_resource_pages WHERE resource_id = ?", (res["id"],))
            conn.execute("DELETE FROM resource_update_pages WHERE update_id IN "
                         "(SELECT id FROM resource_updates WHERE resource_id = ?)", (res["id"],))
            for t in ("resource_updates", "resource_sources", "resource_seen"):
                conn.execute(f"DELETE FROM {t} WHERE resource_id = ?", (res["id"],))
            conn.execute("DELETE FROM acs_resources WHERE id = ?", (res["id"],))
        conn.execute("DELETE FROM acs_ratings WHERE id = ?", (rating_id,))
        conn.commit()
        try:
            os.remove(os.path.join(UPLOAD_DIR, rating["pdf_filename"]))
        except OSError:
            pass
        flash(f"Deleted '{rating['name']}' forever.", "success")
    conn.close()
    return redirect(url_for("groundschool.rating_list"))


@groundschool_bp.route("/<int:rating_id>")
@login_required
def rating_detail(rating_id):
    if _course_blocked(rating_id):
        return _not_granted()
    conn = get_db()
    rating = conn.execute("SELECT * FROM acs_ratings WHERE id = ?", (rating_id,)).fetchone()
    if not rating:
        conn.close()
        return _gone("rating")
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
    changes_by_task = {r["task_id"]: r["n"] for r in conn.execute(
        "SELECT task_id, COUNT(*) AS n FROM acs_task_changes WHERE rating_id = ? AND resolved_at IS NULL "
        "GROUP BY task_id", (rating_id,)).fetchall()} if is_flight_admin() else {}
    viewing_student_id, students = _resolve_viewing_student(conn)
    progress = _rating_progress(conn, rating_id, viewing_student_id) if viewing_student_id else None
    element_done = _element_done_map(conn, rating_id, viewing_student_id) if viewing_student_id else {}
    element_self = _element_self_done(conn, viewing_student_id, element_done.keys())
    conn.close()
    return render_template("flight/groundschool_rating.html", rating=rating, areas=areas,
                            tasks_by_area=tasks_by_area, elements_by_task=elements_by_task,
                            references_html_by_task=references_html_by_task, tab="groundschool",
                            changes_by_task=changes_by_task,
                            gs_students=students, viewing_student_id=viewing_student_id,
                            progress=progress, element_done=element_done, element_self=element_self)


@groundschool_bp.route("/task/<int:task_id>")
@login_required
def task_detail(task_id):
    conn = get_db()
    task = conn.execute("SELECT * FROM acs_tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        conn.close()
        return _gone("task")
    area = conn.execute("SELECT * FROM acs_areas WHERE id = ?", (task["area_id"],)).fetchone()
    if _course_blocked(area["rating_id"]):
        conn.close()
        return _not_granted()
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
    acs_changes = conn.execute("SELECT * FROM acs_task_changes WHERE task_id = ? AND resolved_at IS NULL "
                               "ORDER BY id", (task_id,)).fetchall() if is_flight_admin() else []
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
    # SCHOOL-41: the chosen student's sign-off on this task (shown with its
    # note next to the progress bar for staff).
    viewing_signoff = None
    references_html = linked_references_html(conn, task["acs_references"], task_id=task_id)
    # SCHOOL-08: the one picker at the top of the page is the student every
    # box below (Reading Review, Sign-Off) acts on.
    viewing_student_id, students = _resolve_viewing_student(conn)
    element_ids = [e["id"] for e in knowledge] + [e["id"] for e in risk_management] + [e["id"] for e in skills]
    element_done = {}
    if viewing_student_id and element_ids:
        placeholders = ",".join("?" * len(element_ids))
        rows = conn.execute(
            f"SELECT element_id, self_completed_at, cfi_verified_at FROM acs_element_completion "
            f"WHERE student_id = ? AND element_id IN ({placeholders})",
            [viewing_student_id] + element_ids).fetchall()
        element_done = {r["element_id"]: bool(r["cfi_verified_at"]) for r in rows}
    task_done = sum(1 for v in element_done.values() if v)
    element_self = _element_self_done(conn, viewing_student_id, element_ids)
    task_total = len(element_ids)
    if viewing_student_id:
        viewing_signoff = next((so for so in signoffs if so["student_id"] == viewing_student_id), None)
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
    area_tasks = conn.execute("SELECT id, code, title FROM acs_tasks WHERE area_id = ? ORDER BY order_index",
                               (task["area_id"],)).fetchall()
    conn.close()
    ids = [t["id"] for t in area_tasks]
    gs_lesson = ground_lessons.load(ground_lessons.task_code(
        [e["code"] for e in list(knowledge) + list(risk_management) + list(skills)]))
    idx = ids.index(task_id) if task_id in ids else -1
    prev_task_id = ids[idx - 1] if idx > 0 else None
    next_task_id = ids[idx + 1] if 0 <= idx < len(ids) - 1 else None
    next_task = area_tasks[idx + 1] if next_task_id else None
    return render_template("flight/groundschool_task.html", task=task, next_task=next_task, area=area, rating=rating,
                            notes=notes, knowledge=knowledge, risk_management=risk_management,
                            skills=skills, lesson=lesson, acs_changes=acs_changes, signoffs=signoffs, my_signoff=my_signoff,
                            gs_students=students, prev_task_id=prev_task_id, next_task_id=next_task_id,
                            tab="groundschool", references_html=references_html,
                            element_done=element_done, task_done=task_done, task_total=task_total,
                            reading_progress=reading_progress, viewing_student_id=viewing_student_id,
                            viewing_signoff=viewing_signoff, gs_lesson=gs_lesson,
                            element_self=element_self,
                            gs_el_ids={e["code"]: e["id"] for e in list(knowledge) + list(risk_management) + list(skills)})


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


@groundschool_bp.route("/task/<int:task_id>/changes/resolve", methods=["POST"])
@admin_required
def task_changes_resolve(task_id):
    """ACS-RESOURCES: the admin has refreshed this Task's lesson plan for the ACS change."""
    conn = get_db()
    if conn.execute("SELECT 1 FROM acs_tasks WHERE id = ?", (task_id,)).fetchone():
        conn.execute("UPDATE acs_task_changes SET resolved_at = ?, resolved_by = ? "
                     "WHERE task_id = ? AND resolved_at IS NULL", (now_iso(), session.get("user_id"), task_id))
        conn.commit()
        flash("Marked as refreshed.", "success")
    conn.close()
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
    # SCHOOL-41: signing off the task verifies every one of its elements for
    # this student, so the progress bars agree with the sign-off.
    verified = 0
    for e in conn.execute("SELECT id FROM acs_task_elements WHERE task_id = ?", (task_id,)).fetchall():
        if _verify_element(conn, student_id, e["id"]):
            verified += 1
    conn.commit()
    conn.close()
    flash(f"Signed off {student['name']} on this Task" + (f" - {verified} element{'s' if verified != 1 else ''} verified." if verified else "."),
          "success")
    return redirect(url_for("groundschool.task_detail", task_id=task_id, student_id=student_id))


@groundschool_bp.route("/task/<int:task_id>/signoff/<int:signoff_id>/remove", methods=["POST"])
@cfi_required
def signoff_remove(task_id, signoff_id):
    conn = get_db()
    conn.execute("DELETE FROM acs_task_signoff WHERE id = ? AND task_id = ?", (signoff_id, task_id))
    conn.commit()
    conn.close()
    flash("Sign-off removed.", "success")
    return redirect(_student_url("groundschool.task_detail", task_id=task_id))


# ---------------------------------------------------------------------------
# Resources library
# ---------------------------------------------------------------------------

@groundschool_bp.route("/resources")
@login_required
def resources_list():
    conn = get_db()
    _sync_acs_resources(conn)
    resources = conn.execute(
        "SELECT r.*, (SELECT COUNT(*) FROM acs_resource_pages p WHERE p.resource_id = r.id) AS page_count "
        "FROM acs_resources r").fetchall()
    uid = session.get("user_id")
    seen = {x["resource_id"] for x in conn.execute(
        "SELECT resource_id FROM resource_seen WHERE user_id = ?", (uid,)).fetchall()}
    today = date.today().isoformat()
    new_ids = {r["id"] for r in resources if r["new_until"] and r["new_until"] >= today and r["id"] not in seen}
    extra = {"sources": {}, "pending": [], "last_run": None, "scanning": False}
    if is_flight_admin():
        extra["sources"] = {s["resource_id"]: s for s in conn.execute(
            "SELECT * FROM resource_sources ORDER BY id DESC").fetchall()}
        extra["pending"] = conn.execute(
            "SELECT u.*, r.title AS r_title, r.designation AS r_designation, r.doc_date AS r_doc_date, "
            "r.acs_rating_id AS r_acs_rating_id, "
            "r.revision AS r_revision, r.effectivity AS r_effectivity FROM resource_updates u "
            "JOIN acs_resources r ON r.id = u.resource_id WHERE u.status = 'pending' ORDER BY u.id").fetchall()
        extra["last_run"] = conn.execute("SELECT * FROM resource_scan_runs ORDER BY id DESC LIMIT 1").fetchone()
        extra["scanning"] = _scan_running(conn)
    whats_new = conn.execute(
        "SELECT n.id, n.sent_at, n.text, n.resource_id, rc.read_at FROM resource_notice_recipients rc "
        "JOIN resource_update_notices n ON n.id = rc.notice_id WHERE rc.user_id = ? "
        "ORDER BY n.id DESC LIMIT 10", (uid,)).fetchall()
    conn.execute("UPDATE resource_notice_recipients SET read_at = ? WHERE user_id = ? AND read_at IS NULL",
                 (now_iso(), uid))  # opening Resources marks them read (this page still shows them as new)
    conn.commit()
    extra["whats_new"] = whats_new
    conn.close()
    return render_template("flight/groundschool_resources.html", resources=resources, tab="resources",
                           sections=group_resources(resources), categories=RESOURCE_CATEGORIES,
                           new_ids=new_ids, kind_labels=resource_scan.KIND_LABELS, source_href=resource_scan.source_href,
                           doc_types=sorted({r["doc_type"] for r in resources if r["doc_type"]}), **extra)


RESOURCE_CATEGORIES = ["Sport", "Private", "Instrument", "Commercial", "High Performance", "Complex", "CFI", "CFII"]
OTHER_CATEGORY = "Other / General"


def natural_key(text):
    """Alphabetical, but digit runs compare by value: 'Part 7' < 'Part 61' < 'Part 91'."""
    return [(0, int(t), "") if t.isdigit() else (1, 0, t.lower())
            for t in re.split(r"(\d+)", text or "") if t != ""]


def resource_label(r):
    return r["designation"] or r["title"] or ""


def resource_categories(r):
    """The known categories a resource is tagged with, in display order."""
    tags = {c.strip().lower() for c in (r["categories"] or "").split(",")}
    return [c for c in RESOURCE_CATEGORIES if c.lower() in tags]


def group_resources(resources):
    """[(section name, [resources])] in display order; a resource shows under each of its
    categories, untagged ones under Other / General; each section is naturally sorted."""
    buckets = {}
    for r in resources:
        for c in resource_categories(r) or [OTHER_CATEGORY]:
            buckets.setdefault(c, []).append(r)
    return [(c, sorted(buckets[c], key=lambda r: (natural_key(resource_label(r)), natural_key(r["title"]))))
            for c in RESOURCE_CATEGORIES + [OTHER_CATEGORY] if c in buckets]


def _form_categories():
    picked = set(request.form.getlist("categories"))
    return ",".join(c for c in RESOURCE_CATEGORIES if c in picked) or None


_RESOURCE_META = ("designation", "doc_date", "revision", "effectivity")


@groundschool_bp.route("/resources/<int:resource_id>/edit", methods=["POST"])
@admin_required
def resources_edit(resource_id):
    """Correct a resource's labels only: the PDF and its page index are untouched."""
    conn = get_db()
    r = conn.execute("SELECT * FROM acs_resources WHERE id = ?", (resource_id,)).fetchone()
    if not r:
        conn.close()
        abort(404)
    title = request.form.get("title", "").strip() or r["title"]
    doc_type = request.form.get("doc_type", "").strip() or r["doc_type"]
    meta = tuple(request.form.get(k, "").strip() or None for k in _RESOURCE_META)
    conn.execute("UPDATE acs_resources SET title = ?, doc_type = ?, designation = ?, doc_date = ?, "
                 "revision = ?, effectivity = ?, categories = ? WHERE id = ?",
                 (title, doc_type) + meta + (_form_categories(), resource_id))
    if "source_url" in request.form:
        _save_source(conn, resource_id, request.form.get("source_url"))
    conn.commit()
    conn.close()
    flash(f"Saved '{title}'.", "success")
    return redirect(url_for("groundschool.resources_list"))


def _save_source(conn, resource_id, url):
    """Sets (or with a blank link, removes) the resource's source link. A link that isn't on the
    allow-list is refused with a message and the old one is kept. Returns True when nothing was refused."""
    url = (url or "").strip()
    cur = conn.execute("SELECT * FROM resource_sources WHERE resource_id = ? ORDER BY id DESC LIMIT 1",
                       (resource_id,)).fetchone()
    if not url:
        if cur:
            conn.execute("DELETE FROM resource_sources WHERE resource_id = ?", (resource_id,))
        return True
    try:
        kind = resource_scan.detect_kind(url)
        if kind == "ecfr_multi":
            url = resource_scan.normalize_ecfr_multi(url)
    except resource_scan.BlockedHost as e:
        flash(f"Source link not saved. {e}", "warning")
        return False
    if cur and cur["source_url"] == url:
        return True
    conn.execute("DELETE FROM resource_sources WHERE resource_id = ?", (resource_id,))
    conn.execute("INSERT INTO resource_sources (resource_id, source_url, source_kind) VALUES (?, ?, ?)",
                 (resource_id, url, kind))
    return True


SCAN_ASYNC = True  # tests switch this off so "Check now" finishes before the page reloads


def _scan_running(conn):
    row = conn.execute("SELECT 1 FROM resource_scan_runs WHERE finished_at IS NULL AND started_at > ?",
                       ((datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),)).fetchone()
    return bool(row)


def _scan_job(run_id):
    c = get_db()
    try:
        resource_scan.run_scan(c, "check now", run_id=run_id)
    except Exception:
        c.execute("UPDATE resource_scan_runs SET finished_at = ? WHERE id = ?", (resource_scan.now_iso(), run_id))
        c.commit()
    finally:
        c.close()


@groundschool_bp.route("/resources/scan", methods=["POST"])
@admin_required
def resources_scan():
    conn = get_db()
    if _scan_running(conn):
        flash("A check is already running. Refresh in a minute.", "info")
    elif not conn.execute("SELECT 1 FROM resource_sources").fetchone():
        flash("No source links yet. Add a Source link to a resource first.", "info")
    else:
        run_id = conn.execute("INSERT INTO resource_scan_runs (started_at, trigger) VALUES (?, 'check now')",
                              (resource_scan.now_iso(),)).lastrowid
        conn.commit()
        if SCAN_ASYNC:
            threading.Thread(target=_scan_job, args=(run_id,), daemon=True).start()
            flash("Checking the sources now. Refresh in a minute or two to see the results.", "success")
        else:
            _scan_job(run_id)
            flash("Check finished.", "success")
    conn.close()
    return redirect(url_for("groundschool.resources_list"))


def _update_or_404(conn, update_id):
    u = conn.execute("SELECT * FROM resource_updates WHERE id = ?", (update_id,)).fetchone()
    if not u:
        conn.close()
        abort(404)
    return u


@groundschool_bp.route("/resources/updates/<int:update_id>/file")
@admin_required
def resource_update_file(update_id):
    conn = get_db()
    u = _update_or_404(conn, update_id)
    conn.close()
    path = os.path.join(resource_scan.quarantine_dir(), u["new_filename"] or "")
    if not u["new_filename"] or not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype="application/pdf")


PREVIEW_KINDS = ("faa_alert", "ecfr_multi")
CHANGES_PER_PAGE = 20
PREVIEW_ASYNC = True  # tests switch this off so the preview is ready when the page reloads


def _preview_job(update_id):
    c = get_db()
    try:
        u = c.execute("SELECT * FROM resource_updates WHERE id = ?", (update_id,)).fetchone()
        src = c.execute("SELECT * FROM resource_sources WHERE id = ?", (u["source_id"],)).fetchone()
        res = c.execute("SELECT * FROM acs_resources WHERE id = ?", (u["resource_id"],)).fetchone()
        resource_scan.make_preview(c, src, res, u)
        c.execute("UPDATE resource_updates SET preview_state = 'ready' WHERE id = ?", (update_id,))
    except resource_scan.BlockedHost:
        c.rollback()
        c.execute("UPDATE resource_updates SET preview_state = 'failed: not an allowed site' WHERE id = ?", (update_id,))
    except Exception as e:
        c.rollback()
        c.execute("UPDATE resource_updates SET preview_state = ? WHERE id = ?", (f"failed: {str(e)[:150]}", update_id))
    finally:
        c.commit()
        c.close()


@groundschool_bp.route("/resources/updates/<int:update_id>/preview", methods=["POST"])
@admin_required
def resource_update_preview(update_id):
    """Fetches the new AIM / combined eCFR file into quarantine and opens the change view. Nothing goes live."""
    conn = get_db()
    u = _update_or_404(conn, update_id)
    src = conn.execute("SELECT * FROM resource_sources WHERE id = ?", (u["source_id"],)).fetchone()
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if u["status"] != "pending" or u["new_filename"] or not src or src["source_kind"] not in PREVIEW_KINDS:
        conn.close()
        flash("That update has no preview.", "warning")
        return redirect(url_for("groundschool.resources_list"))
    if u["preview_state"] == "working" and (u["preview_at"] or "") > fresh:
        flash("The preview is still being prepared. Refresh in a minute.", "info")
    else:
        conn.execute("UPDATE resource_updates SET preview_state = 'working', preview_at = ? WHERE id = ?",
                     (now_iso(), update_id))
        conn.commit()
        if PREVIEW_ASYNC:
            threading.Thread(target=_preview_job, args=(update_id,), daemon=True).start()
            flash("Getting the new version. This can take a few minutes; the card updates when it is ready.", "success")
        else:
            _preview_job(update_id)
            return redirect(url_for("groundschool.resource_update_changes", update_id=update_id))
    conn.close()
    return redirect(url_for("groundschool.resources_list"))


@groundschool_bp.route("/resources/updates/<int:update_id>/cancel", methods=["POST"])
@admin_required
def resource_update_cancel(update_id):
    """Throws away the quarantined copy; the alert and the live version stay."""
    conn = get_db()
    u = _update_or_404(conn, update_id)
    src = conn.execute("SELECT source_kind FROM resource_sources WHERE id = ?", (u["source_id"],)).fetchone()
    if u["status"] == "pending" and u["new_filename"] and src and src["source_kind"] in PREVIEW_KINDS:
        conn.execute("UPDATE resource_updates SET new_filename = NULL, preview_state = NULL, new_revision = NULL, "
                     "new_effectivity = NULL, summary = COALESCE(alert_text, summary) WHERE id = ?", (update_id,))
        conn.execute("DELETE FROM resource_update_pages WHERE update_id = ?", (update_id,))
        conn.commit()
        try:
            os.remove(os.path.join(resource_scan.quarantine_dir(), u["new_filename"]))
        except OSError:
            pass
        flash("Cancelled. The current version stays and the alert is still there.", "success")
    conn.close()
    return redirect(url_for("groundschool.resources_list"))


@groundschool_bp.route("/resources/updates/<int:update_id>/changes")
@admin_required
def resource_update_changes(update_id):
    """Before / after view of a pending update, grouped by chapter or part, biggest first when there are many."""
    conn = get_db()
    u = _update_or_404(conn, update_id)
    r = conn.execute("SELECT * FROM acs_resources WHERE id = ?", (u["resource_id"],)).fetchone()
    old = [(x["page_num"], x["text"]) for x in conn.execute(
        "SELECT page_num, text FROM acs_resource_pages WHERE resource_id = ? ORDER BY page_num", (u["resource_id"],))]
    new = [(x["page_num"], x["text"]) for x in conn.execute(
        "SELECT page_num, text FROM resource_update_pages WHERE update_id = ? ORDER BY page_num", (update_id,))]
    src = conn.execute("SELECT source_kind FROM resource_sources WHERE id = ?", (u["source_id"],)).fetchone()
    conn.close()
    if u["status"] != "pending" or not new:
        flash("There is nothing to compare for that update.", "warning")
        return redirect(url_for("groundschool.resources_list"))
    changes = resource_scan.list_changes(old, new)
    counts = {k: sum(1 for c in changes if c["kind"] == k) for k in ("changed", "added", "removed")}
    show = request.args.get("show", "all")
    q = (request.args.get("q") or "").strip().lower()
    shown = [c for c in changes if (show == "all" or c["kind"] == show)
             and (not q or q in c["key"].lower() or q in c["old"].lower() or q in c["new"].lower())]
    big = len(shown) > 60
    if big:
        shown.sort(key=lambda c: -c["size"])
    page = max(1, request.args.get("page", 1, type=int))
    pages_n = max(1, -(-len(shown) // CHANGES_PER_PAGE))
    page = min(page, pages_n)
    items = []
    for c in shown[(page - 1) * CHANGES_PER_PAGE: page * CHANGES_PER_PAGE]:
        before, after = resource_scan.diff_html(c["old"], c["new"])
        items.append(dict(c, before=before, after=after))
    return render_template("flight/groundschool_resource_changes.html", tab="resources", u=u, r=r, counts=counts,
                           items=items, total=len(shown), can_cancel=bool(src and src["source_kind"] in PREVIEW_KINDS), big=big, show=show, q=request.args.get("q", ""),
                           page=page, pages_n=pages_n, summary_head=(u["summary"] or "").split("\n")[0])


def _approve_acs(conn, rating, res, update_id, stored, areas, error):
    """The ACS half of approving an update: the course now points at the new PDF, and its Tasks and
    elements are merged in place. If the new file can't be merged the file is still published and the
    lessons are left exactly as they were. Writes (no commit); returns a short message for the admin."""
    doc_number = _detect_doc_number(os.path.join(UPLOAD_DIR, stored)) or rating["doc_number"]
    conn.execute("UPDATE acs_ratings SET pdf_filename = ?, doc_number = ? WHERE id = ?",
                 (stored, doc_number, rating["id"]))
    conn.execute("UPDATE acs_resources SET designation = COALESCE(?, designation) WHERE id = ?",
                 (doc_number, res["id"]))
    keys = acs_sync.lesson_task_keys(conn, rating["id"])
    report_text, note = None, None
    if error is None:
        conn.execute("SAVEPOINT acs_merge")
        try:
            report = acs_sync.apply_acs(conn, rating["id"], areas, update_id)
            conn.execute("RELEASE acs_merge")
            conn.execute("UPDATE acs_ratings SET status = 'parsed', parsed_at = ?, parse_error = NULL WHERE id = ?",
                         (now_iso(), rating["id"]))
            report_text = acs_sync.format_report(report, keys)
            n = len(report["lessons"])
            note = (f"{n} lesson plan{'s' if n != 1 else ''} need a refresh. They are flagged in the course."
                    if n else "The course is up to date. No built lesson plan was affected.")
        except Exception as e:
            conn.execute("ROLLBACK TO acs_merge")
            conn.execute("RELEASE acs_merge")
            error = str(e)
    if error is not None:
        report_text = ("ACS standards: the new file was published, but its Tasks could not be merged into the "
                       f"course ({error[:150]}). The lessons were left as they were.")
        note = "The new ACS file is published, but it could not be merged into the lessons. They were left as they were."
    conn.execute("UPDATE resource_updates SET acs_report = ? WHERE id = ?", (report_text, update_id))
    return note


@groundschool_bp.route("/resources/updates/<int:update_id>/approve", methods=["POST"])
@admin_required
def resource_update_approve(update_id):
    """The only place a resource's file is ever swapped, and only when an admin taps it."""
    conn = get_db()
    u = _update_or_404(conn, update_id)
    r = conn.execute("SELECT * FROM acs_resources WHERE id = ?", (u["resource_id"],)).fetchone()
    qpath = os.path.join(resource_scan.quarantine_dir(), u["new_filename"] or "")
    if u["status"] != "pending" or not r or not u["new_filename"] or not os.path.isfile(qpath):
        conn.close()
        flash("That update can't be published. Dismiss it instead.", "warning")
        return redirect(url_for("groundschool.resources_list"))
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    stored = f"resource_{''.join(random.choices(string.ascii_lowercase + string.digits, k=12))}.pdf"
    shutil.copyfile(qpath, os.path.join(UPLOAD_DIR, stored))
    # ACS-RESOURCES: read the new ACS first (slow), so no write is held open while it runs.
    acs_areas, acs_error = None, None
    rating = (conn.execute("SELECT * FROM acs_ratings WHERE id = ?", (r["acs_rating_id"],)).fetchone()
              if r["acs_rating_id"] else None)
    if rating:
        try:
            acs_areas = acs_sync.parse_pdf(os.path.join(UPLOAD_DIR, stored))
        except Exception as e:
            acs_error = str(e)
    pages = conn.execute("SELECT page_num, text FROM resource_update_pages WHERE update_id = ?", (update_id,)).fetchall()
    now = now_iso()
    conn.execute("UPDATE resource_updates SET old_doc_date = ?, old_revision = ?, old_effectivity = ? WHERE id = ?",
                 (r["doc_date"], r["revision"], r["effectivity"], update_id))
    conn.execute("UPDATE acs_resources SET prev_filename = filename, filename = ?, designation = COALESCE(?, designation), "
                 "doc_date = COALESCE(?, doc_date), revision = COALESCE(?, revision), "
                 "effectivity = COALESCE(?, effectivity), new_until = ? WHERE id = ?",
                 (stored, u["new_designation"], u["new_doc_date"], u["new_revision"], u["new_effectivity"],
                  (date.today() + timedelta(days=14)).isoformat(), r["id"]))
    conn.execute("DELETE FROM acs_resource_pages WHERE resource_id = ?", (r["id"],))
    conn.executemany("INSERT INTO acs_resource_pages (resource_id, page_num, text) VALUES (?, ?, ?)",
                     [(r["id"], p["page_num"], p["text"]) for p in pages])
    conn.execute("DELETE FROM resource_seen WHERE resource_id = ?", (r["id"],))
    conn.execute("UPDATE resource_updates SET status = 'approved', decided_by = ?, decided_at = ? WHERE id = ?",
                 (session.get("user_id"), now, update_id))
    conn.execute("DELETE FROM resource_update_pages WHERE update_id = ?", (update_id,))
    acs_note = None
    if rating:
        acs_note = _approve_acs(conn, rating, r, update_id, stored, acs_areas, acs_error)
    conn.commit()
    conn.close()
    try:
        os.remove(qpath)
    except OSError:
        pass
    flash(f"Published the new '{r['title']}'. The old file is kept as Previous version.", "success")
    if acs_note:
        flash(acs_note, "info")
    return redirect(url_for("groundschool.resource_update_send", update_id=update_id))


@groundschool_bp.route("/resources/updates/<int:update_id>/reject", methods=["POST"])
@admin_required
def resource_update_reject(update_id):
    conn = get_db()
    u = _update_or_404(conn, update_id)
    if u["status"] == "pending":
        conn.execute("UPDATE resource_updates SET status = 'rejected', decided_by = ?, decided_at = ? WHERE id = ?",
                     (session.get("user_id"), now_iso(), update_id))
        conn.execute("DELETE FROM resource_update_pages WHERE update_id = ?", (update_id,))
        conn.commit()
        if u["new_filename"]:
            try:
                os.remove(os.path.join(resource_scan.quarantine_dir(), u["new_filename"]))
            except OSError:
                pass
        flash("Update rejected. The current version stays, and this version won't be shown again.", "success")
    conn.close()
    return redirect(url_for("groundschool.resources_list"))


@groundschool_bp.route("/resources/<int:resource_id>/previous")
@login_required
def resource_previous(resource_id):
    conn = get_db()
    r = conn.execute("SELECT * FROM acs_resources WHERE id = ?", (resource_id,)).fetchone()
    conn.close()
    if not r or not r["prev_filename"]:
        abort(404)
    return send_file(os.path.join(UPLOAD_DIR, r["prev_filename"]), mimetype="application/pdf")


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
    meta = tuple(request.form.get(k, "").strip() or None for k in _RESOURCE_META)
    stored_name = _save_resource_pdf(f)
    pdf_path = os.path.join(UPLOAD_DIR, stored_name)
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO acs_resources (title, doc_type, filename, uploaded_by, uploaded_at, "
        "designation, doc_date, revision, effectivity, categories) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (title, doc_type, stored_name, session.get("cfi_id"), now_iso()) + meta + (_form_categories(),))
    resource_id = cur.lastrowid
    _save_source(conn, resource_id, request.form.get("source_url"))
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
    if r and r["acs_rating_id"]:
        conn.close()
        flash("That is an ACS course. Delete it from Ground School instead, and it leaves Resources too.", "warning")
        return redirect(url_for("groundschool.resources_list"))
    if r:
        conn.execute("DELETE FROM acs_resource_pages WHERE resource_id = ?", (resource_id,))
        conn.execute("DELETE FROM resource_update_pages WHERE update_id IN "
                     "(SELECT id FROM resource_updates WHERE resource_id = ?)", (resource_id,))
        for t in ("resource_updates", "resource_sources", "resource_seen"):
            conn.execute(f"DELETE FROM {t} WHERE resource_id = ?", (resource_id,))
        conn.execute("DELETE FROM acs_resources WHERE id = ?", (resource_id,))
        conn.commit()
        try:
            os.remove(os.path.join(UPLOAD_DIR, r["filename"]))
        except OSError:
            pass
        flash(f"Deleted '{r['title']}' forever.", "success")
    conn.close()
    return redirect(url_for("groundschool.resources_list"))


@groundschool_bp.route("/resources/<int:resource_id>/file")
@login_required
def resource_file(resource_id):
    conn = get_db()
    r = conn.execute("SELECT * FROM acs_resources WHERE id = ?", (resource_id,)).fetchone()
    allowed = True  # anyone with Flight Academy access gets the Resources library
    if r and session.get("user_id"):
        conn.execute("INSERT OR IGNORE INTO resource_seen (user_id, resource_id) VALUES (?, ?)",
                     (session["user_id"], resource_id))  # opening it clears this viewer's NEW badge
        conn.commit()
    conn.close()
    if not r:
        abort(404)
    if not allowed:
        return _not_granted()
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
    allowed = True  # anyone with Flight Academy access gets the Resources library
    conn.close()
    if not r:
        abort(404)
    if not allowed:
        return _not_granted()
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
        return _gone("element")
    task = conn.execute("SELECT * FROM acs_tasks WHERE id = ?", (el["task_id"],)).fetchone()
    area = conn.execute("SELECT * FROM acs_areas WHERE id = ?", (task["area_id"],)).fetchone()
    if _course_blocked(area["rating_id"]):
        conn.close()
        return _not_granted()
    rating = conn.execute("SELECT * FROM acs_ratings WHERE id = ?", (area["rating_id"],)).fetchone()
    items = conn.execute("SELECT * FROM acs_element_lesson_items WHERE element_id = ? ORDER BY order_index",
                          (element_id,)).fetchall()
    viewing_student_id, students = _resolve_viewing_student(conn)
    # SCHOOL-07: an element with nothing required to click or watch is
    # self-completed the moment the student opens it (the visit is the
    # lesson) - otherwise text-only and empty elements could never finish.
    # GS-LESSONS: when a lesson plan covers this element, the student marks it done
    # with "I've finished this lesson" instead of the visit counting.
    task_codes = [r["code"] for r in conn.execute(
        "SELECT code FROM acs_task_elements WHERE task_id = ?", (task["id"],)).fetchall()]
    task_lesson = ground_lessons.load(ground_lessons.task_code(task_codes))
    el_lesson = ground_lessons.for_element(task_lesson, el["code"])
    if session.get("student_id") and not any(it["required"] for it in items) and not el_lesson:
        _check_self_complete(conn, session["student_id"], element_id)
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
    gs_el_ids, el_done_map, el_self = {}, {}, set()
    if el_lesson:
        task_els = conn.execute("SELECT id, code FROM acs_task_elements WHERE task_id = ?", (task["id"],)).fetchall()
        gs_el_ids = {r["code"]: r["id"] for r in task_els}
        if viewing_student_id:
            ph = ",".join("?" * len(task_els))
            el_done_map = {r["element_id"]: bool(r["cfi_verified_at"]) for r in conn.execute(
                f"SELECT element_id, cfi_verified_at FROM acs_element_completion WHERE student_id = ? "
                f"AND element_id IN ({ph})", [viewing_student_id] + [r["id"] for r in task_els]).fetchall()}
            el_self = _element_self_done(conn, viewing_student_id, gs_el_ids.values())
    conn.close()
    return render_template("flight/groundschool_element.html", el=el, task=task, area=area, rating=rating,
                            items=items, done_item_ids=done_item_ids, completion=completion,
                            gs_students=students, viewing_student_id=viewing_student_id,
                            all_required_done=all_required_done, related_pages=related_pages,
                            tab="groundschool", el_lesson=el_lesson, gs_lesson=task_lesson,
                            gs_el_ids=gs_el_ids, element_done=el_done_map, element_self=el_self)


@groundschool_bp.route("/element/<int:element_id>/lesson-done", methods=["POST"])
@login_required
def element_lesson_done(element_id):
    """GS-LESSONS: the student marks an element's lesson finished (green check); a CFI
    still verifies it. Posting again with undo=1 takes it back."""
    student_id = session.get("student_id")
    if not student_id:
        abort(403)
    conn = get_db()
    rid = _rating_id_of_element(conn, element_id)
    if rid is None or _course_blocked(rid):
        conn.close()
        abort(404)
    existing = conn.execute("SELECT * FROM acs_element_completion WHERE student_id = ? AND element_id = ?",
                             (student_id, element_id)).fetchone()
    undo = request.form.get("undo") == "1"
    if undo:
        if existing and not existing["cfi_verified_at"]:
            conn.execute("UPDATE acs_element_completion SET self_completed_at = NULL "
                         "WHERE student_id = ? AND element_id = ?", (student_id, element_id))
    elif existing:
        if not existing["self_completed_at"]:
            conn.execute("UPDATE acs_element_completion SET self_completed_at = ? "
                         "WHERE student_id = ? AND element_id = ?", (now_iso(), student_id, element_id))
    else:
        conn.execute("INSERT INTO acs_element_completion (student_id, element_id, self_completed_at) "
                     "VALUES (?, ?, ?)", (student_id, element_id, now_iso()))
    conn.commit()
    task_id = conn.execute("SELECT task_id FROM acs_task_elements WHERE id = ?", (element_id,)).fetchone()["task_id"]
    conn.close()
    if undo:
        return redirect(url_for("groundschool.element_detail", element_id=element_id))
    flash("Marked complete. Your instructor will verify it.", "success")
    return redirect(url_for("groundschool.task_detail", task_id=task_id))


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
    rid = _rating_id_of_task(conn, task_id)
    if rid is None or _course_blocked(rid):
        conn.close()
        return {"ok": False, "error": "not found"}, 404
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
    rid = _rating_id_of_task(conn, task_id)
    if rid is not None and _course_blocked(rid):
        conn.close()
        return _not_granted()
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
    return redirect(url_for("groundschool.task_detail", task_id=task_id, student_id=student_id))


@groundschool_bp.route("/element/<int:element_id>/item/<int:item_id>/interact", methods=["POST"])
@login_required
def element_item_interact(element_id, item_id):
    student_id = session.get("student_id")
    if not student_id:
        return {"ok": False, "error": "Only a student's own lesson progress is tracked."}, 403
    conn = get_db()
    rid = _rating_id_of_element(conn, element_id)
    if rid is None or _course_blocked(rid):
        conn.close()
        return {"ok": False, "error": "not found"}, 404
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


def _verify_element(conn, student_id, element_id):
    """Marks one element CFI-verified for a student. Returns True when it
    was newly verified (False if it already was). SCHOOL-07: no gate on the
    student's own self-completion - the CFI's word is what counts."""
    existing = conn.execute("SELECT * FROM acs_element_completion WHERE student_id = ? AND element_id = ?",
                             (student_id, element_id)).fetchone()
    if existing and existing["cfi_verified_at"]:
        return False
    if existing:
        conn.execute("UPDATE acs_element_completion SET cfi_id = ?, cfi_verified_at = ? "
                     "WHERE student_id = ? AND element_id = ?",
                     (session.get("cfi_id"), now_iso(), student_id, element_id))
    else:
        conn.execute("INSERT INTO acs_element_completion (student_id, element_id, cfi_id, cfi_verified_at) "
                     "VALUES (?, ?, ?, ?)", (student_id, element_id, session.get("cfi_id"), now_iso()))
    return True


@groundschool_bp.route("/element/<int:element_id>/verify", methods=["POST"])
@cfi_required
def element_verify(element_id):
    student_id = request.form.get("student_id", type=int)
    conn = get_db()
    student = conn.execute("SELECT id, name FROM students WHERE id = ?", (student_id,)).fetchone() \
        if student_id else None
    if not student:
        conn.close()
        flash("Choose a student at the top of the page first.", "danger")
        return redirect(url_for("groundschool.element_detail", element_id=element_id))
    _verify_element(conn, student_id, element_id)
    conn.commit()
    conn.close()
    flash(f"Verified {student['name']} on this element.", "success")
    return redirect(url_for("groundschool.element_detail", element_id=element_id, student_id=student_id))


# ---------------------------------------------------------------------------
# "Send this update to..." - the notice after an admin approves a Resource update.
# Reuses what already exists: push_pending/push_subscriptions (push.queue_and_push) and the
# SMTP email in notify.py. The log is resource_update_notices; who got it, and whether they
# have read it, is resource_notice_recipients (that is also what the "What's new" list reads).
# ---------------------------------------------------------------------------

@groundschool_bp.app_template_global()
def resource_unread_count():
    """Unread 'What's new in Resources' notices for the signed-in user (dot on the Resources tab)."""
    uid = session.get("user_id")
    if not uid:
        return 0
    conn = get_db()
    try:
        return conn.execute("SELECT COUNT(*) c FROM resource_notice_recipients WHERE user_id = ? AND read_at IS NULL",
                            (uid,)).fetchone()["c"]
    except Exception:
        return 0
    finally:
        conn.close()


_CATEGORY_RATING = {"Instrument": "instrument", "Complex": "complex", "High Performance": "high_performance",
                    "CFI": "cfi", "CFII": "cfii"}
_CATEGORY_CERT = {"Sport": "sport", "Private": "private", "Commercial": "commercial"}
_CATEGORY_CRED = {"CFI": "cred_cfi", "CFII": "cred_cfii", "Complex": "cred_complex",
                  "High Performance": "cred_high_performance"}


def notice_people(conn):
    """Everyone with Flight Academy / Ground School access: [{id, name, roles, cats}], where cats is the
    set of Resource categories that person is enrolled in or certified for (student certificate and
    ratings, granted courses, CFI credentials). Uses only what the app already stores."""
    from db import user_flight_roles
    grants = {}
    for g in conn.execute("SELECT gr.student_id, r.name FROM academy_course_grants gr "
                          "JOIN acs_ratings r ON r.id = gr.rating_id").fetchall():
        grants.setdefault(g["student_id"], []).append((g["name"] or "").lower())
    students = {r["user_id"]: r for r in conn.execute("SELECT * FROM students WHERE user_id IS NOT NULL").fetchall()}
    cfis = {r["user_id"]: r for r in conn.execute("SELECT * FROM cfis WHERE user_id IS NOT NULL").fetchall()}
    out = []
    for u in conn.execute("SELECT * FROM users WHERE active = 1 ORDER BY name COLLATE NOCASE").fetchall():
        roles = set(user_flight_roles(u))
        st = students.get(u["id"]) if "student" in roles else None
        cf = cfis.get(u["id"]) if "cfi" in roles else None
        has_access = (u["is_master_admin"] or u["academy_access"] or u["groundschool_access"] or "admin" in roles
                      or (st is not None and st["id"] in grants))
        if not has_access:
            continue
        cats = set()
        if st is not None:
            have = {x for x in (st["pilot_ratings"] or "").split(",") if x}
            courses = " ".join(grants.get(st["id"], []))
            for c in RESOURCE_CATEGORIES:
                if (_CATEGORY_CERT.get(c) and st["pilot_certificate"] == _CATEGORY_CERT[c]) \
                        or (_CATEGORY_RATING.get(c) in have) or c.lower() in courses:
                    cats.add(c)
        if cf is not None:
            for c, col in _CATEGORY_CRED.items():
                if cf[col]:
                    cats.add(c)
        out.append({"id": u["id"], "name": u["name"], "email": u["email"], "notify_email": bool(u["notify_email"]),
                    "is_cfi": "cfi" in roles, "is_student": "student" in roles, "cats": cats})
    return out


def pick_audience(people, mode, cats=(), person_ids=()):
    """(audience key, label, [user ids]) for a choice on the send form. mode: all | cfis | students |
    cats | people."""
    if mode == "cfis":
        return "cfis", "All CFIs / instructors", [p["id"] for p in people if p["is_cfi"]]
    if mode == "students":
        return "students", "All students", [p["id"] for p in people if p["is_student"]]
    if mode == "cats":
        cats = [c for c in RESOURCE_CATEGORIES if c in set(cats)]
        return ("cats:" + ",".join(cats), "Category: " + ", ".join(cats),
                [p["id"] for p in people if p["cats"] & set(cats)])
    if mode == "people":
        want = sorted({int(i) for i in person_ids if str(i).isdigit()})
        ids = [p["id"] for p in people if p["id"] in want]
        return "people:" + ",".join(str(i) for i in sorted(ids)), f"{len(ids)} chosen {'person' if len(ids) == 1 else 'people'}", ids
    return "all", "Everyone with Flight Academy access", [p["id"] for p in people]


def notice_text(conn, u):
    """The editable 'What changed' notice for an approved update row."""
    r = conn.execute("SELECT * FROM acs_resources WHERE id = ?", (u["resource_id"],)).fetchone()

    def ver(d, rev, eff):
        bits = [d or "", rev or "", ("eff. " + eff) if eff else ""]
        return " | ".join(b for b in bits if b) or "-"
    link = url_for("groundschool.resource_file", resource_id=u["resource_id"], _external=True)
    return (f"Resources update: {r['title']}\n"
            f"FAA number: {r['designation'] or '-'}\n"
            f"Old: {ver(u['old_doc_date'], u['old_revision'], u['old_effectivity'])}\n"
            f"New: {ver(r['doc_date'], r['revision'], r['effectivity'])}\n\n"
            f"What changed:\n{(u['summary'] or 'See the new PDF.').strip()}\n\n"
            f"New PDF: {link}")


def deliver_notice(conn, notice_id, user_ids, text, people):
    """Push (and email where the person opted in and email is set up) - the existing paths. Never raises."""
    import push
    import notify
    title = text.split("\n", 1)[0][:120]
    body = "Open Resources to read what changed."
    settings = notify.get_settings(conn)
    by_id = {p["id"]: p for p in people}
    for uid in user_ids:
        try:
            push.queue_and_push(conn, uid, title, body, tag=f"resource-notice-{notice_id}",
                                url=url_for("groundschool.resources_list"))
        except Exception:
            pass
        p = by_id.get(uid)
        if p and p["notify_email"] and p["email"] and notify.email_configured(settings):
            try:
                notify.send_email(settings, p["email"], title, text)
            except Exception:
                pass


def _approved_update_or_404(conn, update_id):
    u = _update_or_404(conn, update_id)
    if u["status"] != "approved":
        conn.close()
        abort(404)
    return u


@groundschool_bp.route("/resources/updates/<int:update_id>/send", methods=["GET", "POST"])
@admin_required
def resource_update_send(update_id):
    conn = get_db()
    u = _approved_update_or_404(conn, update_id)
    people = notice_people(conn)
    again = None
    if request.method == "POST":
        text = (request.form.get("text") or "").strip()
        mode = request.form.get("audience", "all")
        key, label, ids = pick_audience(people, mode, request.form.getlist("cats"), request.form.getlist("people"))
        if mode == "cats" and not request.form.getlist("cats"):
            flash("Pick at least one category.", "warning")
        elif not text:
            flash("The notice is empty.", "warning")
        elif not ids:
            flash("Nobody matches that choice, so nothing was sent.", "warning")
        else:
            prior = conn.execute("SELECT sent_at FROM resource_update_notices WHERE update_id = ? AND audience = ? "
                                 "ORDER BY id DESC LIMIT 1", (update_id, key)).fetchone()
            if prior and not request.form.get("again"):
                again = f"This update was already sent to {label} on {prior['sent_at'][:16].replace('T', ' ')}."
            else:
                nid = conn.execute(
                    "INSERT INTO resource_update_notices (update_id, resource_id, audience, audience_label, sent_by, "
                    "sent_at, text, recipient_count) VALUES (?,?,?,?,?,?,?,?)",
                    (update_id, u["resource_id"], key, label, session.get("user_id"), now_iso(), text, len(ids))).lastrowid
                conn.executemany("INSERT INTO resource_notice_recipients (notice_id, user_id) VALUES (?, ?)",
                                 [(nid, i) for i in ids])
                conn.commit()
                deliver_notice(conn, nid, ids, text, people)
                conn.close()
                flash(f"Sent to {len(ids)} {'person' if len(ids) == 1 else 'people'} ({label}).", "success")
                return redirect(url_for("groundschool.resources_list"))
    form = request.form if request.method == "POST" else {}
    text = form.get("text") or notice_text(conn, u)
    sent = conn.execute("SELECT * FROM resource_update_notices WHERE update_id = ? ORDER BY id DESC", (update_id,)).fetchall()
    conn.close()
    return render_template("flight/groundschool_resource_send.html", tab="resources", u=u, text=text, people=people,
                           categories=RESOURCE_CATEGORIES, sent=sent, again=again, form=form,
                           picked_cats=form.getlist("cats") if request.method == "POST" else [],
                           picked_people={str(i) for i in (form.getlist("people") if request.method == "POST" else [])})


@groundschool_bp.route("/resources/notices")
@admin_required
def resource_notices_log():
    """What was sent, to whom, by whom."""
    conn = get_db()
    notices = conn.execute(
        "SELECT n.*, r.title AS r_title, s.name AS sender FROM resource_update_notices n "
        "LEFT JOIN acs_resources r ON r.id = n.resource_id LEFT JOIN users s ON s.id = n.sent_by "
        "ORDER BY n.id DESC LIMIT 100").fetchall()
    names = {}
    for n in notices:
        names[n["id"]] = conn.execute(
            "SELECT u.name, rc.read_at FROM resource_notice_recipients rc JOIN users u ON u.id = rc.user_id "
            "WHERE rc.notice_id = ? ORDER BY u.name COLLATE NOCASE", (n["id"],)).fetchall()
    conn.close()
    return render_template("flight/groundschool_resource_notices.html", tab="resources", notices=notices, names=names)
