"""Parser for FAA Airman Certification Standards (ACS) PDFs.

Extracts a nested structure of:
  areas: [{code, title, tasks: [...]}]
  task: {code (area letter, e.g. "A"), title, aircraft_note, references,
         objective, notes: [...], knowledge: [...], risk_management: [...],
         skills: [...]}
  element: {code (e.g. "PA.I.A.K1"), text}

Built against FAA-S-ACS-6C (Private Pilot - Airplane), but the pattern
(References/Objective/Notes/Knowledge/Risk Management/Skills, each
element line prefixed with a "<PREFIX>.<AREA_ROMAN>.<TASK_LETTER>.<K|R|S><n><subletter?>"
code) is the same across the FAA's ACS document family, so this should
work with only the FOOTER_PREFIX changed for other ratings' ACS PDFs.
"""
import re
import sys

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

AREA_RE = re.compile(r"^Area of Operation ([IVXLC]+)\.\s*(.+)$")
TASK_RE = re.compile(r"^Task ([A-Z])\.\s*(.+)$")
REFERENCES_RE = re.compile(r"^References:\s*(.*)$")
OBJECTIVE_RE = re.compile(r"^Objective:\s*(.*)$")
NOTE_RE = re.compile(r"^Note:\s*(.*)$")
KNOWLEDGE_HDR_RE = re.compile(r"^Knowledge:\s*(.*)$")
RISK_HDR_RE = re.compile(r"^Risk\s*$")
RISK_HDR_INLINE_RE = re.compile(r"^Risk\s+Management:\s*(.*)$")
MANAGEMENT_CONT_RE = re.compile(r"^Management:\s*(.*)$")
SKILLS_HDR_RE = re.compile(r"^Skills:\s*(.*)$")
# A code like PA.I.A.K1 or PA.I.A.K1a - the prefix (PA/CA/IR/etc.) varies by cert.
CODE_LINE_RE = re.compile(r"^([A-Z]{2}\.[IVXLC]+\.[A-Z]\.[KRS]\d+[a-z]?)\b\s*(.*)$")


_ANY_CODE_RE = re.compile(r"\b[A-Z]{2}\.[IVXLC]+\.[A-Z]\.[KRS]\d+[a-z]?\b")


def _clean_pages(pdf_path, footer_prefix, end_marker="Appendix 1"):
    """Return the list of body lines (footers stripped, page-header dedup'd)
    starting at the first page that contains an actual element code (this
    skips the front matter and Table of Contents, which lists Area/Task
    names but has no References/Objective/K/R/S codes) through the first
    page whose text begins with end_marker."""
    if pdfplumber is None:
        raise RuntimeError("pdfplumber is required to parse ACS PDFs")
    lines = []
    started = False
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if not started:
                # The changelog page lists bare codes too, and the Table of
                # Contents lists Area/Task names - neither has a References:
                # line, which only appears in actual task content.
                if _ANY_CODE_RE.search(text) and re.search(r"(?m)^References:", text):
                    started = True
                else:
                    continue
            if text.strip().startswith(end_marker):
                break
            for raw in text.split("\n"):
                line = raw.strip()
                if not line:
                    continue
                if line.startswith(footer_prefix):
                    continue
                lines.append(line)
    # Join the split "Risk" / "Management:" header across a page-wrap.
    joined = []
    i = 0
    while i < len(lines):
        if RISK_HDR_RE.match(lines[i]) and i + 1 < len(lines) and MANAGEMENT_CONT_RE.match(lines[i + 1]):
            m = MANAGEMENT_CONT_RE.match(lines[i + 1])
            joined.append("Risk Management: " + (m.group(1) or ""))
            i += 2
            continue
        joined.append(lines[i])
        i += 1
    return joined


def parse_acs(pdf_path, footer_prefix="Private Pilot for Airplane Category ACS"):
    lines = _clean_pages(pdf_path, footer_prefix)

    areas = []
    area = None
    task = None
    section = None  # None | 'knowledge' | 'risk_management' | 'skills'
    current_code = None  # last element code, for continuation-line merging

    def flush_task():
        nonlocal task
        if task is not None:
            task["references"] = " ".join(task["_ref_lines"]).strip()
            task["objective"] = " ".join(task["_obj_lines"]).strip()
            del task["_ref_lines"], task["_obj_lines"]
            area["tasks"].append(task)
        task = None

    prev_area_code = None
    for line in lines:
        m = AREA_RE.match(line)
        if m:
            code, title = m.group(1), m.group(2).strip()
            if code != prev_area_code:
                flush_task()
                area = {"code": code, "title": title, "tasks": []}
                areas.append(area)
                prev_area_code = code
                section = None
                current_code = None
            continue  # running header on continuation pages - nothing else to do

        m = TASK_RE.match(line)
        if m:
            flush_task()
            letter, title = m.group(1), m.group(2).strip()
            task = {"code": letter, "title": title, "_ref_lines": [], "_obj_lines": [],
                    "notes": [], "knowledge": [], "risk_management": [], "skills": []}
            section = "title_cont"  # a long title can wrap onto the next line
            current_code = None
            continue

        if task is None:
            continue  # front matter / stray line before first Task

        m = REFERENCES_RE.match(line)
        if m:
            section = "references"
            if m.group(1):
                task["_ref_lines"].append(m.group(1))
            continue

        m = OBJECTIVE_RE.match(line)
        if m:
            section = "objective"
            if m.group(1):
                task["_obj_lines"].append(m.group(1))
            continue

        m = NOTE_RE.match(line)
        if m:
            section = "note"
            task["notes"].append(m.group(1))
            current_code = None
            continue

        m = KNOWLEDGE_HDR_RE.match(line)
        if m:
            section = "knowledge"
            current_code = None
            continue

        m = RISK_HDR_INLINE_RE.match(line)
        if m:
            section = "risk_management"
            current_code = None
            continue

        m = SKILLS_HDR_RE.match(line)
        if m:
            section = "skills"
            current_code = None
            continue

        m = CODE_LINE_RE.match(line)
        if m:
            code, text = m.group(1), m.group(2).strip()
            if text == "[Archived]":
                current_code = None
                continue
            bucket = {"knowledge": task["knowledge"], "risk_management": task["risk_management"],
                      "skills": task["skills"]}.get(section)
            if bucket is not None:
                bucket.append({"code": code, "text": text})
                current_code = code
            continue

        # Continuation line (wrapped text with no code prefix).
        if section == "title_cont":
            task["title"] += " " + line
        elif section == "references":
            task["_ref_lines"].append(line)
        elif section == "objective":
            task["_obj_lines"].append(line)
        elif section == "note" and task["notes"]:
            task["notes"][-1] += " " + line
        elif section in ("knowledge", "risk_management", "skills") and current_code:
            bucket = {"knowledge": task["knowledge"], "risk_management": task["risk_management"],
                      "skills": task["skills"]}[section]
            bucket[-1]["text"] += " " + line
        # else: drop silently (e.g. stray running header fragments)

    flush_task()
    return areas


def summarize(areas):
    n_tasks = sum(len(a["tasks"]) for a in areas)
    n_elements = sum(len(t["knowledge"]) + len(t["risk_management"]) + len(t["skills"])
                      for a in areas for t in a["tasks"])
    return {"areas": len(areas), "tasks": n_tasks, "elements": n_elements}


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "private_acs.pdf"
    areas = parse_acs(path)
    print(summarize(areas))
    for a in areas:
        print(f"\nArea {a['code']}. {a['title']}  ({len(a['tasks'])} tasks)")
        for t in a["tasks"]:
            print(f"  Task {t['code']}. {t['title']}  "
                  f"[K={len(t['knowledge'])} R={len(t['risk_management'])} S={len(t['skills'])}]")
