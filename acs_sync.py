"""ACS in Flight Academy Resources: keep the Ground School course in step with the ACS PDF.

Every ACS course (acs_ratings) also shows up in Resources (acs_resources.acs_rating_id), so the
normal update check watches it and files a PENDING update for an admin to approve. This module
does the two ACS-specific parts of that:

  * diff_acs()  - before approval: what the new ACS changes in the course (read-only), written
                  into the pending update's summary so the admin sees it up front.
  * apply_acs() - on approval: merge the new ACS into the course IN PLACE. Task and element
                  ids stay the same, so lesson plans, sign-offs and reading progress survive.
                  Every Task whose ACS text changed gets a change note (acs_task_changes) that
                  shows on its lesson until an admin marks the lesson refreshed.

Items are matched by their ACS codes (Area "I", Task "A", element "PA.I.A.K1"), the same codes
the FAA keeps across editions. An item the new ACS drops is deleted only when nobody has worked
on it; otherwise it stays, marked retired_at, so student records are never lost.

No network, no PDF reading here: the caller hands in the parsed areas (acs_parser.parse_acs).
"""
import re
from datetime import datetime, timezone

# FAA publishes each ACS at one address that keeps its name from edition to edition, so the
# course is matched by its document number (FAA-S-ACS-6C -> 6) and falls back to its name.
FAA_ACS_BASE = "https://www.faa.gov/training_testing/testing/acs/"
_BY_NUMBER = {
    "6": "private_airplane_acs_6.pdf",
    "7": "commercial_airplane_acs_7.pdf",
    "8": "instrument_rating_airplane_acs_8.pdf",
    "25": "cfi_airplane_acs_25.pdf",
    "11": "atp_airplane_acs_11.pdf",
    "15": "private_helicopter_acs_15.pdf",
    "16": "commercial_helicopter_acs_16.pdf",
    "14": "instrument_helicopter_acs_14.pdf",
    "29": "cfi_helicopter_acs_29.pdf",
}
_BY_NAME = [
    (r"private.*airplane|private pilot", "6"),
    (r"commercial.*airplane|commercial pilot", "7"),
    (r"instrument", "8"),
    (r"flight instructor|\bcfi\b", "25"),
]
_NUMBER_RE = re.compile(r"ACS-(\d+)", re.I)

# Which Resources section(s) an ACS is filed under.
_CATEGORY_BY_NAME = [
    (r"private", "Private"), (r"instrument", "Instrument"), (r"commercial", "Commercial"),
    (r"sport", "Sport"), (r"instructor|\bcfi\b", "CFI"),
]

MAX_DETAIL = 160
SANITY_RATIO = 0.5  # a new ACS that finds fewer than this share of the old Tasks is treated as unreadable


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_pdf(path):
    """Parsed Areas/Tasks/elements of an ACS PDF (acs_parser.parse_acs). One place so tests can swap it."""
    from acs_parser import parse_acs
    return parse_acs(path)


def default_source_url(name, doc_number):
    """The FAA's own address for this ACS, or None when we aren't sure which one it is."""
    num = None
    m = _NUMBER_RE.search(doc_number or "")
    if m and m.group(1) in _BY_NUMBER:
        num = m.group(1)
    else:
        low = (name or "").lower()
        for pat, n in _BY_NAME:
            if re.search(pat, low):
                num = n
                break
    return FAA_ACS_BASE + _BY_NUMBER[num] if num else None


def categories_for(name):
    tags = [cat for pat, cat in _CATEGORY_BY_NAME if re.search(pat, (name or "").lower())]
    return ",".join(tags) or None


def _snip(text, n=MAX_DETAIL):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def _norm(text):
    return re.sub(r"\s+", " ", text or "").strip()


# ---------------------------------------------------------------------------
# Reading the course as it is now
# ---------------------------------------------------------------------------

def _current(conn, rating_id):
    """{(area code, task code): {'row', 'area_row', 'notes', 'elements': {code: row}}} for live
    (not retired) tasks, plus {area code: area row}."""
    areas = {a["code"]: a for a in conn.execute(
        "SELECT * FROM acs_areas WHERE rating_id = ? ORDER BY order_index", (rating_id,)).fetchall()}
    tasks = {}
    for a in areas.values():
        for t in conn.execute("SELECT * FROM acs_tasks WHERE area_id = ? AND retired_at IS NULL "
                              "ORDER BY order_index", (a["id"],)).fetchall():
            els = {e["code"]: e for e in conn.execute(
                "SELECT * FROM acs_task_elements WHERE task_id = ? AND retired_at IS NULL "
                "ORDER BY order_index", (t["id"],)).fetchall()}
            notes = [n["note"] for n in conn.execute(
                "SELECT note FROM acs_task_notes WHERE task_id = ? ORDER BY order_index", (t["id"],))]
            tasks[(a["code"], t["code"])] = {"row": t, "area_row": a, "notes": notes, "elements": els}
    return areas, tasks


def _new_elements(task):
    out = []
    for kind in ("knowledge", "risk_management", "skills"):
        for el in task[kind]:
            out.append((kind, el["code"], el["text"]))
    return out


def _task_label(area_code, task_code, title):
    return f"Area {area_code} Task {task_code}" + (f" ({title})" if title else "")


# ---------------------------------------------------------------------------
# The difference (no writes)
# ---------------------------------------------------------------------------

def diff_acs(conn, rating_id, new_areas):
    """{'tasks': [{'key', 'label', 'changes': [(kind, code, detail)]}], 'added_tasks', 'removed_tasks',
    'old_task_count', 'new_task_count'} comparing the course now with the parsed new ACS."""
    _areas, old = _current(conn, rating_id)
    seen = set()
    report = {"tasks": [], "added_tasks": [], "removed_tasks": [],
              "old_task_count": len(old), "new_task_count": 0}
    for area in new_areas:
        for task in area["tasks"]:
            key = (area["code"], task["code"])
            seen.add(key)
            report["new_task_count"] += 1
            label = _task_label(area["code"], task["code"], task["title"])
            cur = old.get(key)
            if not cur:
                report["added_tasks"].append({"key": key, "label": label})
                continue
            changes = []
            row = cur["row"]
            if _norm(row["title"]) != _norm(task["title"]):
                changes.append(("changed", "Title", f"Title was \"{_snip(row['title'])}\". Now \"{_snip(task['title'])}\"."))
            if _norm(row["objective"]) != _norm(task["objective"]):
                changes.append(("changed", "Objective", f"Objective now: \"{_snip(task['objective'])}\"."))
            if _norm(row["acs_references"]) != _norm(task["references"]):
                changes.append(("changed", "References", f"References now: \"{_snip(task['references'])}\"."))
            if [_norm(n) for n in cur["notes"]] != [_norm(n) for n in task["notes"]]:
                changes.append(("changed", "Notes", "The ACS Notes for this Task changed."))
            new_els = {code: (kind, text) for kind, code, text in _new_elements(task)}
            for code, (kind, text) in new_els.items():
                old_el = cur["elements"].get(code)
                if not old_el:
                    changes.append(("added", code, f"{code} is new: \"{_snip(text)}\"."))
                elif _norm(old_el["text"]) != _norm(text):
                    changes.append(("changed", code, f"{code} was \"{_snip(old_el['text'], 110)}\". "
                                                    f"Now \"{_snip(text, 110)}\"."))
            for code in cur["elements"]:
                if code not in new_els:
                    changes.append(("removed", code, f"{code} is gone: \"{_snip(cur['elements'][code]['text'], 110)}\"."))
            if changes:
                report["tasks"].append({"key": key, "label": label, "changes": changes})
    for key, cur in old.items():
        if key not in seen:
            report["removed_tasks"].append({"key": key, "label": _task_label(key[0], key[1], cur["row"]["title"])})
    return report


def looks_unreadable(report):
    """True when the new ACS found too few of the course's Tasks to trust the merge."""
    old_n = report["old_task_count"]
    return report["new_task_count"] == 0 or (old_n >= 4 and report["new_task_count"] < old_n * SANITY_RATIO)


def format_report(report, lesson_task_keys=None, limit=30):
    """Plain-words text for the pending update / the approved update page."""
    n_changed = len(report["tasks"])
    if not (n_changed or report["added_tasks"] or report["removed_tasks"]):
        return "ACS standards: no Task or element wording changed, so the lessons stay as they are."
    lines = [f"ACS standards: {n_changed} Task{'s' if n_changed != 1 else ''} changed, "
             f"{len(report['added_tasks'])} added, {len(report['removed_tasks'])} removed."]
    items = []
    for t in report["tasks"]:
        flag = ""
        if lesson_task_keys is not None and t["key"] in lesson_task_keys:
            flag = " [lesson plan to refresh]"
        items.append(f"{t['label']}{flag}: " + " ".join(d for _k, _c, d in t["changes"]))
    items += [f"New Task: {t['label']}." for t in report["added_tasks"]]
    items += [f"Removed Task: {t['label']}." for t in report["removed_tasks"]]
    if len(items) > limit:
        lines.append(f"Showing {limit} of {len(items)}.")
        items = items[:limit]
    return "\n".join(lines + items)


def _file_lesson_exists(conn, task_id):
    """True when a built lesson plan file (ground_lessons/<TASKCODE>.json) exists for the Task."""
    try:
        import ground_lessons
    except Exception:
        return False
    codes = [r["code"] for r in conn.execute("SELECT code FROM acs_task_elements WHERE task_id = ?", (task_id,))]
    return bool(ground_lessons.load(ground_lessons.task_code(codes)))


def _task_has_lesson(conn, task_id):
    """A lesson plan is built for this Task: a lesson file, saved lesson text, or element lesson items."""
    if _file_lesson_exists(conn, task_id):
        return True
    lesson = conn.execute("SELECT content FROM acs_lesson_content WHERE task_id = ?", (task_id,)).fetchone()
    if lesson and (lesson["content"] or "").strip():
        return True
    return bool(conn.execute(
        "SELECT 1 FROM acs_element_lesson_items i JOIN acs_task_elements e ON e.id = i.element_id "
        "WHERE e.task_id = ? LIMIT 1", (task_id,)).fetchone())


def lesson_task_keys(conn, rating_id):
    """Keys of Tasks that already have a lesson plan."""
    keys = set()
    for r in conn.execute(
            "SELECT t.id, a.code AS ac, t.code AS tc FROM acs_tasks t JOIN acs_areas a ON a.id = t.area_id "
            "WHERE a.rating_id = ? AND t.retired_at IS NULL", (rating_id,)).fetchall():
        if _task_has_lesson(conn, r["id"]):
            keys.add((r["ac"], r["tc"]))
    return keys


# ---------------------------------------------------------------------------
# The merge (writes; the caller wraps it in a savepoint)
# ---------------------------------------------------------------------------

def _element_has_work(conn, element_id):
    return bool(conn.execute(
        "SELECT 1 FROM acs_element_completion WHERE element_id = ? "
        "UNION SELECT 1 FROM acs_element_lesson_items WHERE element_id = ? LIMIT 1",
        (element_id, element_id)).fetchone())


def _task_has_work(conn, task_id):
    if conn.execute("SELECT 1 FROM acs_task_signoff WHERE task_id = ? LIMIT 1", (task_id,)).fetchone():
        return True
    lesson = conn.execute("SELECT content FROM acs_lesson_content WHERE task_id = ?", (task_id,)).fetchone()
    if lesson and (lesson["content"] or "").strip():
        return True
    return any(_element_has_work(conn, e["id"]) for e in conn.execute(
        "SELECT id FROM acs_task_elements WHERE task_id = ?", (task_id,)))


def purge_tasks(conn, task_ids):
    """Deletes these Tasks and everything hanging off them (elements, lesson items and progress, completions,
    sign-offs, lesson text, notes, change notes). Does not commit."""
    for tid in task_ids:
        for eid in [r["id"] for r in conn.execute("SELECT id FROM acs_task_elements WHERE task_id = ?", (tid,))]:
            conn.execute("DELETE FROM acs_element_item_progress WHERE item_id IN "
                         "(SELECT id FROM acs_element_lesson_items WHERE element_id = ?)", (eid,))
            conn.execute("DELETE FROM acs_element_lesson_items WHERE element_id = ?", (eid,))
            conn.execute("DELETE FROM acs_element_completion WHERE element_id = ?", (eid,))
        conn.execute("DELETE FROM acs_task_reading_progress WHERE task_id = ?", (tid,))
        conn.execute("DELETE FROM acs_task_signoff WHERE task_id = ?", (tid,))
        conn.execute("DELETE FROM acs_lesson_content WHERE task_id = ?", (tid,))
        conn.execute("DELETE FROM acs_task_elements WHERE task_id = ?", (tid,))
        conn.execute("DELETE FROM acs_task_notes WHERE task_id = ?", (tid,))
        conn.execute("DELETE FROM acs_task_changes WHERE task_id = ?", (tid,))
        conn.execute("DELETE FROM acs_tasks WHERE id = ?", (tid,))


def _delete_task(conn, task_id):
    purge_tasks(conn, [task_id])


def _has_table(conn, name):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone())


def apply_acs(conn, rating_id, new_areas, update_id=None):
    """Merges the parsed new ACS into the course in place and logs a change note on every Task whose
    ACS text changed. Returns the same report diff_acs() gives, plus 'lessons': [(task_id, label)]
    for Tasks that already have a lesson plan. Does not commit."""
    report = diff_acs(conn, rating_id, new_areas)
    if looks_unreadable(report):
        raise ValueError(f"the new ACS only showed {report['new_task_count']} of the course's "
                         f"{report['old_task_count']} Tasks, so it was not merged")
    now = now_iso()
    areas_by_code, _old = _current(conn, rating_id)
    changed_keys = {t["key"]: t for t in report["tasks"]}
    lessons = []
    for a_idx, area in enumerate(new_areas):
        arow = areas_by_code.get(area["code"])
        if arow:
            conn.execute("UPDATE acs_areas SET title = ?, order_index = ? WHERE id = ?",
                         (area["title"], a_idx, arow["id"]))
            area_id = arow["id"]
        else:
            area_id = conn.execute("INSERT INTO acs_areas (rating_id, code, title, order_index) VALUES (?,?,?,?)",
                                   (rating_id, area["code"], area["title"], a_idx)).lastrowid
        existing = {t["code"]: t for t in conn.execute(
            "SELECT * FROM acs_tasks WHERE area_id = ?", (area_id,)).fetchall()}
        for t_idx, task in enumerate(area["tasks"]):
            trow = existing.get(task["code"])
            if trow:
                task_id = trow["id"]
                conn.execute("UPDATE acs_tasks SET title = ?, acs_references = ?, objective = ?, order_index = ?, "
                             "retired_at = NULL WHERE id = ?",
                             (task["title"], task["references"], task["objective"], t_idx, task_id))
            else:
                task_id = conn.execute(
                    "INSERT INTO acs_tasks (area_id, code, title, acs_references, objective, order_index) "
                    "VALUES (?,?,?,?,?,?)",
                    (area_id, task["code"], task["title"], task["references"], task["objective"], t_idx)).lastrowid
            conn.execute("DELETE FROM acs_task_notes WHERE task_id = ?", (task_id,))
            for n_idx, note in enumerate(task["notes"]):
                conn.execute("INSERT INTO acs_task_notes (task_id, note, order_index) VALUES (?,?,?)",
                             (task_id, note, n_idx))
            _merge_elements(conn, task_id, _new_elements(task))
            key = (area["code"], task["code"])
            if key in changed_keys:
                for kind, code, detail in changed_keys[key]["changes"]:
                    conn.execute(
                        "INSERT INTO acs_task_changes (update_id, rating_id, task_id, kind, code, detail, created_at) "
                        "VALUES (?,?,?,?,?,?,?)", (update_id, rating_id, task_id, kind, code, detail, now))
                if _task_has_work_for_lesson(conn, task_id):
                    lessons.append((task_id, changed_keys[key]["label"]))
            elif not trow and report["old_task_count"]:
                conn.execute(
                    "INSERT INTO acs_task_changes (update_id, rating_id, task_id, kind, code, detail, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (update_id, rating_id, task_id, "added", "Task",
                     "This Task is new in the ACS. It needs a lesson plan.", now))
    # Tasks the new ACS no longer has
    for item in report["removed_tasks"]:
        area_code, task_code = item["key"]
        arow = areas_by_code.get(area_code)
        if not arow:
            continue
        trow = conn.execute("SELECT id FROM acs_tasks WHERE area_id = ? AND code = ?",
                            (arow["id"], task_code)).fetchone()
        if not trow:
            continue
        if _task_has_work(conn, trow["id"]):
            conn.execute("UPDATE acs_tasks SET retired_at = ? WHERE id = ?", (now, trow["id"]))
            conn.execute("INSERT INTO acs_task_changes (update_id, rating_id, task_id, kind, code, detail, created_at) "
                         "VALUES (?,?,?,?,?,?,?)",
                         (update_id, rating_id, trow["id"], "removed", "Task",
                          "This Task is no longer in the ACS. It is kept because it has lesson content or student "
                          "records.", now))
        else:
            _delete_task(conn, trow["id"])
    # Areas left with no Tasks at all
    for arow in areas_by_code.values():
        if not conn.execute("SELECT 1 FROM acs_tasks WHERE area_id = ? LIMIT 1", (arow["id"],)).fetchone():
            conn.execute("DELETE FROM acs_areas WHERE id = ?", (arow["id"],))
    report["lessons"] = lessons
    return report


def _task_has_work_for_lesson(conn, task_id):
    return _task_has_lesson(conn, task_id)


def _merge_elements(conn, task_id, new_els):
    """Same element code keeps its row (and so its lesson items and progress); new codes are added; codes
    the ACS dropped are deleted when untouched, retired when a student or lesson used them."""
    existing = {e["code"]: e for e in conn.execute(
        "SELECT * FROM acs_task_elements WHERE task_id = ?", (task_id,)).fetchall()}
    keep = set()
    for idx, (kind, code, text) in enumerate(new_els):
        keep.add(code)
        row = existing.get(code)
        if row:
            conn.execute("UPDATE acs_task_elements SET kind = ?, text = ?, order_index = ?, retired_at = NULL "
                         "WHERE id = ?", (kind, text, idx, row["id"]))
        else:
            conn.execute("INSERT INTO acs_task_elements (task_id, kind, code, text, order_index) VALUES (?,?,?,?,?)",
                         (task_id, kind, code, text, idx))
    for code, row in existing.items():
        if code in keep:
            continue
        if _element_has_work(conn, row["id"]):
            conn.execute("UPDATE acs_task_elements SET retired_at = COALESCE(retired_at, ?) WHERE id = ?",
                         (now_iso(), row["id"]))
        else:
            conn.execute("DELETE FROM acs_task_elements WHERE id = ?", (row["id"],))
