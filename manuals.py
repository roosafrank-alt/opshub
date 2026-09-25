"""Manual / Illustrated-Parts-Catalog library.

PDFs (maintenance manuals, IPCs) get uploaded and tagged at upload time to
a make/model/year-range/serial-range, then auto-indexed page-by-page - the
PDF's own text is pulled out with pypdf and scanned for figure numbers and
part numbers (see _extract_figures/_extract_parts) so a plane's detail page
can show just the manuals that actually cover it (manual_matches_asset),
and a manual can later be searched by part/figure number to jump straight
to the relevant pages.

This is a heuristic text scan, not a real PDF parser - manuals vary too
much in layout for anything stronger without per-manufacturer tuning. It
will miss some real part numbers and pick up some junk; that's fine, since
matches only ever narrow down where to look for a human, nothing here
auto-applies parts to a project.
"""
import os
import re
import string
import random

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, session
from db import get_db, now_iso, UPLOAD_DIR
from auth import shop_role_required

manuals_bp = Blueprint("manuals", __name__)

ALLOWED_MANUAL_EXT = {"pdf"}

# Aviation part numbers are all over the map (AN960-10, MS20470AD4-6,
# 65-41003-1, ST-1234...) so this is deliberately loose: 2+ dash-joined
# alphanumeric groups, at least one digit somewhere, 5-20 chars - catches
# real part numbers without matching page numbers, dates, or plain words.
_PART_RE = re.compile(r'\b(?=[A-Z0-9-]{5,20}\b)[A-Z0-9]+(?:-[A-Z0-9]+){1,4}\b')
_FIGURE_RE = re.compile(r'\bFIG(?:URE)?\.?\s*([0-9]{1,3}-[0-9]{1,3}(?:-[0-9]{1,3})?)\b', re.IGNORECASE)


def _extract_parts(text):
    if not text:
        return []
    found = set()
    for m in _PART_RE.finditer(text.upper()):
        tok = m.group(0)
        if any(c.isdigit() for c in tok):
            found.add(tok)
    return sorted(found)


def _extract_figures(text):
    if not text:
        return []
    return sorted(set(m.group(1) for m in _FIGURE_RE.finditer(text)))


def allowed_manual(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_MANUAL_EXT


def save_manual_upload(file_storage):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    ext = file_storage.filename.rsplit(".", 1)[1].lower()
    alphabet = string.ascii_lowercase + string.digits
    unique = "".join(random.choices(alphabet, k=12))
    stored_name = f"manual_{unique}.{ext}"
    file_storage.save(os.path.join(UPLOAD_DIR, stored_name))
    return stored_name


def _index_manual_pdf(conn, manual_id, file_path):
    """Populates manual_pages + manual_page_parts from the PDF's text
    layer. If pypdf isn't installed, or the PDF is scanned images with no
    text layer, this quietly indexes 0 pages rather than failing the
    upload - the manual is still saved and viewable by page number, just
    without the figure/part search."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return 0
    try:
        reader = PdfReader(file_path)
    except Exception:
        return 0
    page_count = 0
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        figures = _extract_figures(text)
        # A figure number ("32-10") also matches the part-number pattern -
        # drop it from parts so it isn't listed as both.
        parts = [p for p in _extract_parts(text) if p not in figures]
        cur = conn.execute(
            "INSERT INTO manual_pages (manual_id, page_num, text_content, figure_refs, part_numbers) "
            "VALUES (?, ?, ?, ?, ?)",
            (manual_id, i, text, ",".join(figures), ",".join(parts)))
        page_id = cur.lastrowid
        for p in parts:
            conn.execute("INSERT INTO manual_page_parts (manual_page_id, part_number) VALUES (?, ?)",
                         (page_id, p))
        page_count = i
    conn.commit()
    return page_count


def _serial_num(s):
    """Pulls the numeric portion out of a serial string ('172-71234' ->
    71234) so ranges compare even when serials carry a model prefix;
    returns None if there's nothing numeric, in which case range checks
    are skipped rather than guessed at."""
    if not s:
        return None
    digits = re.sub(r'[^0-9]', '', str(s))
    return int(digits) if digits else None


def manual_matches_asset(manual, asset):
    """True if this manual's tagged make/model/year/serial range covers
    this plane. Make/model must line up (case-insensitive, either side a
    prefix of the other, so a manual tagged "172" also matches an asset
    model of "172N" or "172SP"); year and serial range only apply if the
    manual specified them - an unset bound doesn't exclude anything."""
    if manual["make"] and asset["make"]:
        if manual["make"].strip().lower() != asset["make"].strip().lower():
            return False
    m_model = (manual["model"] or "").strip().lower()
    a_model = (asset["model"] or "").strip().lower()
    if m_model and a_model:
        if not (a_model.startswith(m_model) or m_model.startswith(a_model)):
            return False
    if manual["year_start"] or manual["year_end"]:
        try:
            a_year = int(re.sub(r'[^0-9]', '', str(asset["year"] or "")))
        except ValueError:
            a_year = None
        if a_year:
            if manual["year_start"] and a_year < manual["year_start"]:
                return False
            if manual["year_end"] and a_year > manual["year_end"]:
                return False
    if manual["serial_start"] or manual["serial_end"]:
        a_sn = _serial_num(asset["serial_number"])
        if a_sn is not None:
            sn_start = _serial_num(manual["serial_start"])
            sn_end = _serial_num(manual["serial_end"])
            if sn_start is not None and a_sn < sn_start:
                return False
            if sn_end is not None and a_sn > sn_end:
                return False
    return True


def manuals_for_asset(conn, asset):
    """Every manual in the library whose tagged make/model/year/serial
    range covers this plane - used on the asset detail page."""
    all_manuals = conn.execute("SELECT * FROM manuals ORDER BY manual_type, title").fetchall()
    return [m for m in all_manuals if manual_matches_asset(m, asset)]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@manuals_bp.route("/manuals")
@shop_role_required('admin')
def manuals_list():
    conn = get_db()
    manuals = conn.execute("SELECT * FROM manuals ORDER BY created_at DESC").fetchall()
    conn.close()
    return render_template("manuals_list.html", manuals=manuals)


@manuals_bp.route("/manuals/upload", methods=["POST"])
@shop_role_required('admin')
def manuals_upload():
    f = request.files.get("manual_file")
    title = request.form.get("title", "").strip()
    if not f or not f.filename or not allowed_manual(f.filename):
        flash("Choose a PDF to upload.", "danger")
        return redirect(url_for("manuals.manuals_list"))
    if not title:
        title = f.filename.rsplit(".", 1)[0]

    def _int_or_none(key):
        v = request.form.get(key, "").strip()
        return int(v) if v.isdigit() else None

    stored_name = save_manual_upload(f)
    conn = get_db()
    cur = conn.execute("""
        INSERT INTO manuals (title, manual_type, filename, make, model, year_start, year_end,
                              serial_start, serial_end, page_count, uploaded_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
    """, (title, request.form.get("manual_type", "maintenance"), stored_name,
          request.form.get("make", "").strip() or None, request.form.get("model", "").strip() or None,
          _int_or_none("year_start"), _int_or_none("year_end"),
          request.form.get("serial_start", "").strip() or None,
          request.form.get("serial_end", "").strip() or None,
          session.get("user_id"), now_iso()))
    manual_id = cur.lastrowid
    conn.commit()
    page_count = _index_manual_pdf(conn, manual_id, os.path.join(UPLOAD_DIR, stored_name))
    conn.execute("UPDATE manuals SET page_count = ? WHERE id = ?", (page_count, manual_id))
    conn.commit()
    conn.close()
    if page_count:
        flash(f"Uploaded '{title}' - indexed {page_count} page(s).", "success")
    else:
        flash(f"Uploaded '{title}' - no searchable text found (scanned/image-only PDF?), "
              "so it's saved but not indexed by part/figure number.", "warning")
    return redirect(url_for("manuals.manual_detail", manual_id=manual_id))


@manuals_bp.route("/manuals/<int:manual_id>")
@shop_role_required('admin')
def manual_detail(manual_id):
    conn = get_db()
    manual = conn.execute("SELECT * FROM manuals WHERE id = ?", (manual_id,)).fetchone()
    if not manual:
        conn.close()
        abort(404)
    q = request.args.get("q", "").strip()
    pages = []
    if q:
        like = f"%{q.upper()}%"
        pages = conn.execute("""
            SELECT * FROM manual_pages
            WHERE manual_id = ? AND (UPPER(part_numbers) LIKE ? OR UPPER(figure_refs) LIKE ? OR UPPER(text_content) LIKE ?)
            ORDER BY page_num
        """, (manual_id, like, like, like)).fetchall()
    assets = conn.execute("SELECT * FROM assets WHERE deleted_at IS NULL ORDER BY tag").fetchall()
    matching_assets = [a for a in assets if manual_matches_asset(manual, a)]
    conn.close()
    return render_template("manual_detail.html", manual=manual, pages=pages, q=q,
                           matching_assets=matching_assets)


@manuals_bp.route("/manuals/<int:manual_id>/delete", methods=["POST"])
@shop_role_required('admin')
def manual_delete(manual_id):
    conn = get_db()
    manual = conn.execute("SELECT * FROM manuals WHERE id = ?", (manual_id,)).fetchone()
    if manual:
        conn.execute("""DELETE FROM manual_page_parts WHERE manual_page_id IN
                         (SELECT id FROM manual_pages WHERE manual_id = ?)""", (manual_id,))
        conn.execute("DELETE FROM manual_pages WHERE manual_id = ?", (manual_id,))
        conn.execute("DELETE FROM manuals WHERE id = ?", (manual_id,))
        conn.commit()
        try:
            os.remove(os.path.join(UPLOAD_DIR, manual["filename"]))
        except OSError:
            pass
        flash(f"Deleted '{manual['title']}'.", "success")
    conn.close()
    return redirect(url_for("manuals.manuals_list"))
