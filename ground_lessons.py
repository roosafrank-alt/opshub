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
        return ('<a href="%s" class="gsl-cite">%s</a>'
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


ACTIVITY_TYPES = ("match", "sort", "order", "tf", "flash", "label", "calc", "decide")  # GS-MORE-PLAY: calc, decide


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
    # GS-PRACTICE: a hand-written game may carry its own fixed key (h1, h2, ...). Those don't take
    # an "a" number, so adding hand-written games never shifts the keys (a0, a1, ...) of the games
    # numbered here, and a student's saved progress on them stays attached to the same game.
    n = 0
    for i, a in enumerate(out):
        if isinstance(a.get("key"), str) and _OWN_KEY_RE.fullmatch(a["key"]):
            out[i] = dict(a)
        else:
            out[i] = dict(a, key="a%d" % n)
            n += 1
    return out


_OWN_KEY_RE = re.compile(r"h\d{1,2}")


def play_plan(lesson, group=3):
    """Spread the games and quiz questions through the lesson instead of piling them at the end.
    Returns {"flash": [...], "by_chunk": {chunk_number: [item, ...]}}. An item is
    {"kind": "act", "a": activity} or {"kind": "quiz", "label": "Quick check 2 of 4", "qs": [(n, q), ...]}.
    An activity with "after": N (1-based chunk) is pinned there; the rest are spread evenly,
    alternating game, quiz, game, quiz."""
    n = len(lesson.get("chunks") or [])
    acts = activities(lesson)
    flash = [a for a in acts if a["type"] == "flash"]
    acts = [a for a in acts if a["type"] != "flash"]
    by_chunk = {}
    free = []
    for a in acts:
        after = a.get("after")
        if isinstance(after, int) and 1 <= after <= max(n, 1):
            by_chunk.setdefault(after, []).append({"kind": "act", "a": a})
        else:
            free.append({"kind": "act", "a": a})
    qs = list(enumerate(lesson.get("quiz") or [], 1))
    groups = [qs[i:i + group] for i in range(0, len(qs), group)]
    quizzes = [{"kind": "quiz", "label": "Quick check %d of %d" % (i + 1, len(groups)), "qs": g}
               for i, g in enumerate(groups)]
    items = []
    while free or quizzes:
        if free:
            items.append(free.pop(0))
        if quizzes:
            items.append(quizzes.pop(0))
    m = len(items)
    for k, it in enumerate(items):
        pos = min(max(1, round((k + 1) * n / (m + 1))), max(n, 1))
        by_chunk.setdefault(pos, []).append(it)
    return {"flash": flash, "by_chunk": by_chunk}


def final_questions(lesson):
    """The Final check's multiple-choice questions: the lesson's own `final_quiz`, or its quick checks."""
    return (lesson or {}).get("final_quiz") or (lesson or {}).get("quiz") or []


def final_prefix(lesson):
    """Saved-answer key prefix for the Final check. A lesson with its own `final_quiz` saves under
    'fz' (fz1, fz2, ...), so answers given earlier to the old Final check (its quick-check questions
    asked again, saved as fq1, fq2, ...) are never counted against different questions."""
    return "fz" if (lesson or {}).get("final_quiz") else "fq"


def with_figures(qs, figures_of):
    """[(n, q)] with q["fig"] set to the figure a question shows (its "figure" key, looked up with
    figures_of(key) -> figure dict or None)."""
    out = []
    for n, q in qs:
        f = figures_of(q.get("figure")) if q.get("figure") else None
        out.append((n, dict(q, fig=f) if f else q))
    return out


def final_plan(lesson):
    """The end-of-Task final check: every multiple-choice question (the lesson's own `final_quiz`
    when it has one, otherwise its quick-check questions) plus its match, sort, order and
    true/false games (and GS-MORE-PLAY calc and decide games) played again, in one graded round."""
    figs = lesson.get("figures") or {}
    qs = with_figures(list(enumerate(final_questions(lesson), 1)), figs.get)
    acts = [dict(a, key="f" + a["key"]) for a in activities(lesson) if a["type"] in ("match", "sort", "order", "tf", "calc", "decide")]
    return {"qs": qs, "acts": acts, "prefix": final_prefix(lesson)}


# ---------------------------------------------------------------- GS-AREA-REVIEW
AREA_DIR = os.path.join(LESSON_DIR, "areas")
_AREA_CACHE = {}


def area_code(task_codes):
    """'PA.I' from Task codes like 'PA.I.A' (the first one that parses)."""
    for c in task_codes:
        m = re.fullmatch(r"([A-Z]{2,3}\.[IVXL]+)\.[A-Z]+", c or "")
        if m:
            return m.group(1)
    return None


def load_area(code):
    """The Area review dict (ground_lessons/areas/<code>.json, e.g. PA.I) or None."""
    if not code or not re.fullmatch(r"[A-Z]{2,3}\.[IVXL]+", code):
        return None
    path = os.path.join(AREA_DIR, code + ".json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    hit = _AREA_CACHE.get(code)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(path, encoding="utf-8") as f:
            review = json.load(f)
    except (OSError, ValueError):
        return None
    _AREA_CACHE[code] = (mtime, review)
    return review


def area_figure(ref):
    """A figure named 'PA.I.B:f2' in an Area review question: that Task lesson's figure f2."""
    m = re.fullmatch(r"([A-Z]{2,3}\.[IVXL]+\.[A-Z]+):(\w+)", ref or "")
    if not m:
        return None
    lesson = load(m.group(1))
    return ((lesson or {}).get("figures") or {}).get(m.group(2))


def area_plan(review, group=5):
    """The Area review's questions in parts of about `group`, numbered through: [{"label", "qs"}]."""
    qs = with_figures(list(enumerate((review or {}).get("questions") or [], 1)), area_figure)
    parts = [qs[i:i + group] for i in range(0, len(qs), group)]
    return [{"label": "Part %d of %d" % (i + 1, len(parts)), "qs": p} for i, p in enumerate(parts)]


# ---------------------------------------------------------------- GS-BUILTIN-COURSE
# A course that isn't an FAA ACS (e.g. the Part 91 Refresher) is written as an outline file,
# ground_lessons/courses/<CODE>.json, in the same shape acs_parser.parse_acs() returns:
# {"code": "RF", "name": ..., "doc_number": "OPSHUB-RF", "areas": [{"code", "title", "tasks": [{"code", "title",
#  "objective", "references", "notes", "knowledge": [{"code", "text"}], "risk_management": [...], "skills": [...]}]}]}
# An admin installs it from the Ground School page; its lessons are ordinary <TASKCODE>.json files.
COURSE_DIR = os.path.join(LESSON_DIR, "courses")


def builtin_courses():
    """Every built-in course outline, sorted by name: [{"code", "name", "doc_number", "about", "areas"}]."""
    out = []
    try:
        names = sorted(os.listdir(COURSE_DIR))
    except OSError:
        return out
    for fn in names:
        if not re.fullmatch(r"[A-Z]{2,3}\.json", fn):
            continue
        try:
            with open(os.path.join(COURSE_DIR, fn), encoding="utf-8") as f:
                c = json.load(f)
        except (OSError, ValueError):
            continue
        if c.get("code") == fn[:-5] and c.get("name") and c.get("doc_number") and c.get("areas"):
            out.append(c)
    return sorted(out, key=lambda c: c["name"])


def builtin_course(code):
    return next((c for c in builtin_courses() if c["code"] == code), None)


def builtin_by_doc(doc_number):
    return next((c for c in builtin_courses() if doc_number and c["doc_number"] == doc_number), None)
