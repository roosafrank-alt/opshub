"""Ground School lesson plans (GS-LESSONS).

One JSON file per ACS Task in ground_lessons/<TASKCODE>.json (e.g. PA.I.A.json),
written by Claude from the ACS and the uploaded Resources, fact-checked against
the source pages. Figures live in static/lessons/. The Task page shows the
lesson above the editable Lesson Content box when a file exists for that Task.

Inline citations in lesson text look like [[35:312|14 CFR 61.57]] - resource id,
PDF page, label - and render as links to that page of the uploaded resource.
"""
import json
import os
import re

from flask import url_for
from markupsafe import Markup, escape

LESSON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ground_lessons")
_CACHE = {}
_CODE_RE = re.compile(r"^([A-Z]{2,3}\.[IVXL]+\.[A-Z]+)\.")
_CITE_RE = re.compile(r"\[\[(\d+):(\d+)\|([^\]]+)\]\]")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")


def task_code(element_codes):
    """'PA.I.A' from the Task's element codes ('PA.I.A.K1', ...)."""
    for c in element_codes:
        m = _CODE_RE.match(c or "")
        if m:
            return m.group(1)
    return None


def load(code):
    """The lesson dict for an ACS Task code, or None. Re-read when the file changes."""
    if not code or not re.fullmatch(r"[A-Z]{2,3}\.[IVXL]+\.[A-Z]+", code):
        return None
    path = os.path.join(LESSON_DIR, code + ".json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    hit = _CACHE.get(code)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(path, encoding="utf-8") as f:
            lesson = json.load(f)
    except (OSError, ValueError):
        return None
    _CACHE[code] = (mtime, lesson)
    return lesson


def cite_url(res_id, page):
    return url_for("groundschool.resource_excerpt", resource_id=int(res_id), pages=int(page))


def rich(text):
    """Escape lesson text, then turn [[res:page|label]] into links and **x** into bold."""
    s = str(escape(text or ""))

    def link(m):
        return ('<a href="%s" target="_blank" rel="noopener" class="gsl-cite">%s</a>'
                % (cite_url(m.group(1), m.group(2)), m.group(3)))
    s = _CITE_RE.sub(link, s)
    s = _BOLD_RE.sub(r"<b>\1</b>", s)
    return Markup(s)


def references(lesson):
    """Every distinct citation in the lesson, sorted by label: [(label, url)]."""
    found = {}
    for r, p, label in _CITE_RE.findall(json.dumps(lesson, ensure_ascii=False)):
        found.setdefault(label + "|" + r + "|" + p, (label, cite_url(r, p)))
    return sorted(found.values(), key=lambda x: [int(t) if t.isdigit() else t.lower()
                                                 for t in re.split(r"(\d+)", x[0])])


def element_kind(code):
    m = re.search(r"\.(K|R|S)\d", code or "")
    return m.group(1) if m else "K"


def _code_matches(c, el_code):
    """Same element, or one is the other's sub-element (K1 <-> K1a)."""
    if not c or not el_code:
        return False
    return c == el_code or (c.startswith(el_code) and c[len(el_code):].isalpha()) or \
        (el_code.startswith(c) and el_code[len(c):].isalpha())


def for_element(lesson, el_code):
    """The parts of a Task lesson that teach one ACS element: its chunks (numbered as in the
    full lesson), the scenario and checkride skills that list it. None if nothing matches."""
    if not lesson or not el_code:
        return None
    chunks = [(i + 1, c) for i, c in enumerate(lesson.get("chunks") or [])
              if any(_code_matches(cd, el_code) for cd in c.get("codes") or [])]
    sc = lesson.get("scenario")
    scenario = sc if sc and any(_code_matches(cd, el_code) for cd in sc.get("codes") or []) else None
    skills = [s for s in lesson.get("skills") or [] if _code_matches(s.get("code"), el_code)]
    if not (chunks or scenario or skills):
        return None
    return {"chunks": chunks, "scenario": scenario, "skills": skills, "minutes": sum(c.get("minutes") or 0 for _, c in chunks)}


ACTIVITY_TYPES = ("match", "sort", "order", "tf", "flash", "label")


def activities(lesson):
    """Games for a lesson: its own `activities` list (JSON), then a flashcard deck and a
    term-matching game built from `key_terms`, so every lesson has something to play."""
    out = list(lesson.get("activities") or [])
    terms = [t for t in lesson.get("key_terms") or [] if t.get("term") and t.get("def")]
    if len(terms) >= 4:
        out.append({"type": "flash", "title": "Flashcards: key terms",
                    "cards": [{"front": t["term"], "back": t["def"]} for t in terms]})
        out.append({"type": "match", "title": "Match the term",
                    "intro": "Tap a term on the left, then its meaning on the right.",
                    "pairs": [{"l": t["term"], "r": t["def"]} for t in terms[:6]]})
    return out
