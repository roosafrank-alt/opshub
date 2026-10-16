"""Parser for FAA Practical Test Standards (PTS) PDFs, e.g. FAA-S-8081-29
(Sport Pilot and Flight Instructors with a Sport Pilot Rating).

Returns the same structure as acs_parser.parse_acs so the rest of the
ground-school code can store it unchanged:
  areas: [{code, title, tasks: [...]}]
  task: {code, title, references, objective, notes, knowledge,
         risk_management, skills}
  element: {code (e.g. "SP.I.A.K1"), text}

PTS layout (differs from the ACS):
  "I. AREA OF OPERATION: PREFLIGHT PREPARATION"
  "A. TASK: CERTIFICATES AND DOCUMENTS (ASEL and ASES)"
  "REFERENCES: ..."  /  "NOTE: ..."  /  "Objective. To determine that ..."
  numbered objective items "1. ...", each optionally with "a. ..." sub-items.
There are no Knowledge/Risk/Skills codes, so each numbered objective item
becomes one element: knowledge for oral-knowledge items, skills otherwise.
A PTS PDF holds several sections (Airplane, Gyroplane, Glider, Flight
Instructor); `section` picks which one to read (default 1 = Airplane).
"""
import re
import sys

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

AREA_RE = re.compile(r"^([IVXLC]+)\.\s+AREA OF OPERATION:\s*(.+)$")
TASK_RE = re.compile(r"^([A-Z])\.\s+TASK:\s*(.+)$")
SECTION_RE = re.compile(r"^Section\s+(\d)\b", re.I)
REFERENCES_RE = re.compile(r"^REFERENCES?:\s*(.*)$")
OBJECTIVE_RE = re.compile(r"^Objective\.?\s*(.*)$")
NOTE_RE = re.compile(r"^NOTE:\s*(.*)$")
ITEM_RE = re.compile(r"^(\d{1,2})\.\s+(.*)$")
SUBITEM_RE = re.compile(r"^([a-z])\.\s+(.*)$")
PAGE_NUM_RE = re.compile(r"^\d+(\s+\d+)?$")

DEFAULT_PREFIX = "SP"


def _lines(pdf_path):
    if pdfplumber is None:
        raise RuntimeError("pdfplumber is required to parse PTS PDFs")
    out = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for raw in (page.extract_text() or "").split("\n"):
                line = raw.strip()
                if not line or PAGE_NUM_RE.match(line) or line == "SO":
                    continue  # blank, page number, or the V-sub-SO subscript fragment
                out.append(line)
    return out


def _select_section(lines, section):
    """Keep only the lines of one section. A section starts at its
    'Section N' divider; a PDF with no dividers is treated as section 1.
    The divider text also appears in the table of contents and the change
    log, so a section only counts once an AREA OF OPERATION line follows."""
    current = None
    chosen = []
    seen_divider = False
    for line in lines:
        m = SECTION_RE.match(line)
        if m and len(line) < 80 and not AREA_RE.match(line):
            current = int(m.group(1))
            seen_divider = True
            continue
        if not seen_divider or current == section:
            chosen.append(line)
    return chosen


def _roman_to_int(s):
    vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
    total = 0
    for i, ch in enumerate(s):
        v = vals[ch]
        total += -v if i + 1 < len(s) and vals[s[i + 1]] > v else v
    return total


def parse_pts(pdf_path, section=1, prefix=DEFAULT_PREFIX):
    lines = _select_section(_lines(pdf_path), section)

    areas = []
    area = None
    task = None
    mode = None  # title_cont | note | references | objective
    pending_area_notes = []
    obj_intro = []
    items = []  # [{"num", "text", "subs": []}]

    def flush_task():
        nonlocal task, obj_intro, items
        if task is None:
            return
        intro = " ".join(obj_intro).strip()
        task["objective"] = intro
        task["references"] = " ".join(task.pop("_ref_lines")).strip()
        oral_by = bool(re.search(r"knowledge of the elements related to .* by:?$", intro, re.I))
        # Preflight Preparation is tested orally, as are tasks marked Oral Only.
        oral_all = oral_by or area["code"] == "I" or "oral only" in task["title"].lower()
        k_n = s_n = 0
        for it in items:
            text = it["text"]
            if it["subs"]:
                text = text.rstrip() + " " + "; ".join(x.rstrip(".") for x in it["subs"]) + "."
            is_knowledge = oral_all or bool(re.search(r"\bknowledge\b", text, re.I))
            if is_knowledge:
                k_n += 1
                kind, code = "knowledge", f"{prefix}.{area['code']}.{task['code']}.K{k_n}"
            else:
                s_n += 1
                kind, code = "skills", f"{prefix}.{area['code']}.{task['code']}.S{s_n}"
            task[kind].append({"code": code, "text": text.strip()})
        area["tasks"].append(task)
        task = None
        obj_intro = []
        items = []

    for line in lines:
        m = AREA_RE.match(line)
        if m:
            flush_task()
            area = {"code": m.group(1), "title": m.group(2).strip(), "tasks": []}
            areas.append(area)
            mode = None
            pending_area_notes = []
            continue
        if area is None:
            continue  # front matter, table of contents, checklists

        m = TASK_RE.match(line)
        if m:
            flush_task()
            task = {"code": m.group(1), "title": m.group(2).strip(), "_ref_lines": [],
                    "objective": "", "references": "", "notes": list(pending_area_notes),
                    "knowledge": [], "risk_management": [], "skills": []}
            pending_area_notes = []
            mode = "title_cont"
            continue

        if task is None:
            m = NOTE_RE.match(line)
            if m:
                pending_area_notes.append(m.group(1))
                mode = "area_note"
            elif mode == "area_note" and pending_area_notes:
                pending_area_notes[-1] += " " + line
            continue

        m = REFERENCES_RE.match(line)
        if m:
            mode = "references"
            if m.group(1):
                task["_ref_lines"].append(m.group(1))
            continue
        m = OBJECTIVE_RE.match(line)
        if m:
            mode = "objective"
            if m.group(1):
                obj_intro.append(m.group(1))
            continue
        m = NOTE_RE.match(line)
        if m:
            mode = "note"
            task["notes"].append(m.group(1))
            continue

        if mode == "objective" or mode == "item":
            sm = SUBITEM_RE.match(line)
            im = ITEM_RE.match(line)
            if sm and items:
                items[-1]["subs"].append(sm.group(2))
                mode = "sub"
                continue
            if im and (not items or int(im.group(1)) == items[-1]["num"] + 1):
                items.append({"num": int(im.group(1)), "text": im.group(2), "subs": []})
                mode = "item"
                continue
            if mode == "objective":
                obj_intro.append(line)
                continue
        if mode == "sub" and items:
            sm = SUBITEM_RE.match(line)
            im = ITEM_RE.match(line)
            if sm:
                items[-1]["subs"].append(sm.group(2))
            elif im and int(im.group(1)) == items[-1]["num"] + 1:
                items.append({"num": int(im.group(1)), "text": im.group(2), "subs": []})
                mode = "item"
            else:
                items[-1]["subs"][-1] += " " + line
            continue
        if mode == "item" and items:
            items[-1]["text"] += " " + line
        elif mode == "title_cont":
            task["title"] += " " + line
        elif mode == "references":
            task["_ref_lines"].append(line)
        elif mode == "note" and task["notes"]:
            task["notes"][-1] += " " + line

    flush_task()
    # An area with no tasks is just a table-of-contents/checklist echo.
    return [a for a in areas if a["tasks"]]


def summarize(areas):
    n_tasks = sum(len(a["tasks"]) for a in areas)
    n_elements = sum(len(t["knowledge"]) + len(t["risk_management"]) + len(t["skills"])
                     for a in areas for t in a["tasks"])
    return {"areas": len(areas), "tasks": n_tasks, "elements": n_elements}


if __name__ == "__main__":
    path = sys.argv[1]
    sec = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    areas = parse_pts(path, sec)
    print(summarize(areas))
    for a in areas:
        print(f"\nArea {a['code']}. {a['title']}  ({len(a['tasks'])} tasks)")
        for t in a["tasks"]:
            print(f"  Task {t['code']}. {t['title']}  "
                  f"[K={len(t['knowledge'])} S={len(t['skills'])}]")
