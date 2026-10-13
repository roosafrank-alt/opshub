"""Flight Academy Resources: the automatic update scanner.

Each resource can have a "source": the official place its current version
lives (an eCFR part, a faa.gov publication page, or a direct PDF link, see
resource_sources). Every two weeks (pi-scripts/resource-scan.sh) and whenever
an admin taps "Check now", run_scan() looks at each source, one at a time and
politely. When the source shows a new version it downloads the PDF into a
quarantine folder (never into the live uploads), indexes its pages, writes a
plain-words summary of what changed and files a PENDING update. Nothing is
replaced until an admin approves it on the Resources page.

The scanner may only contact the hosts in ALLOWED_HOSTS (one constant, change
it here). Network access goes through one `fetch` function so tests can feed
it saved sample pages and never touch the internet.

    python3 resource_scan.py        # what the cron job runs
"""
import difflib
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# The ONLY hosts the scanner will contact. Anything else is refused.
ALLOWED_HOSTS = ("faa.gov", "www.faa.gov", "ecfr.gov", "www.ecfr.gov", "govinfo.gov")

USER_AGENT = "OpsHub-FlightAcademy-ResourceCheck/1.0 (private flight school reference library; contact via school)"
TIMEOUT = 30                     # seconds per request
PAUSE = 3.0                      # seconds between requests
MAX_PAGE_BYTES = 2 * 1024 * 1024         # a web page or API answer
MAX_PDF_BYTES = 200 * 1024 * 1024        # a PDF (the PHAK and Instrument Handbook are 60-80 MB)
MAX_SUMMARY_ITEMS = 25
FORBIDDEN_PDF_TOKENS = (b"/JavaScript", b"/JS", b"/Launch", b"/OpenAction", b"/EmbeddedFile")

KIND_LABELS = {"ecfr": "eCFR", "faa_page": "FAA page", "pdf_link": "PDF link",
               "ecfr_multi": "eCFR, several parts (alert only)", "faa_alert": "FAA page (alert only)"}

# A source for a file made of several eCFR parts is typed as "14: 1, 23, 91; 49: 830" (title: parts; ...).
_MULTI_RE = re.compile(r"^\s*\d{1,2}\s*:\s*\d+(?:\s*,\s*\d+)*(?:\s*;\s*\d{1,2}\s*:\s*\d+(?:\s*,\s*\d+)*)*\s*;?\s*$")


class BlockedHost(Exception):
    pass


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def quarantine_dir():
    import db
    return os.path.join(os.path.dirname(db.DB_PATH), "quarantine")


# ---------------------------------------------------------------------------
# Host allow-list and the one place that touches the network
# ---------------------------------------------------------------------------

def check_host(url):
    """Raises BlockedHost unless url is https on an allow-listed host."""
    p = urllib.parse.urlparse((url or "").strip())
    if p.scheme != "https" or (p.hostname or "").lower() not in ALLOWED_HOSTS or p.username or p.password:
        raise BlockedHost(f"Only https links on {', '.join(ALLOWED_HOSTS)} are allowed.")
    return url.strip()


def parse_ecfr_multi(text):
    """[(title, part), ...] from "14: 1, 23, 91; 49: 830". Raises ValueError when it isn't that form."""
    if not _MULTI_RE.match(text or ""):
        raise ValueError("expected eCFR parts like 14: 1, 23, 91; 49: 830")
    out = []
    for chunk in text.split(";"):
        if chunk.strip():
            title, parts = chunk.split(":")
            out += [(int(title), int(x)) for x in parts.split(",")]
    return out


def normalize_ecfr_multi(text):
    by_title = {}
    for t, p in parse_ecfr_multi(text):
        by_title.setdefault(t, [])
        if p not in by_title[t]:
            by_title[t].append(p)
    return "; ".join(f"{t}: {', '.join(str(p) for p in ps)}" for t, ps in by_title.items())


def source_href(src):
    """Where the Source link of a resource should point (a typed list of parts links to eCFR itself)."""
    url = src["source_url"]
    return url if url.startswith("https://") else "https://www.ecfr.gov/"


def detect_kind(url):
    """'ecfr', 'ecfr_multi', 'faa_alert', 'pdf_link' or 'faa_page' for an allowed link; raises BlockedHost otherwise."""
    if _MULTI_RE.match(url or ""):
        return "ecfr_multi"
    check_host(url)
    p = urllib.parse.urlparse(url.strip())
    if p.hostname.lower() in ("ecfr.gov", "www.ecfr.gov"):
        return "ecfr"
    if p.path.lower().endswith(".pdf"):
        return "pdf_link"
    if "/aim_html" in p.path.lower():
        return "faa_alert"
    return "faa_page" if p.hostname.lower().endswith("faa.gov") else "pdf_link"


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_host(newurl)  # a redirect off the allow-list is refused too
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def default_fetch(url, max_bytes=MAX_PAGE_BYTES, method="GET"):
    """(status, lower-cased headers, body bytes). Refuses other hosts and oversize answers.
    method "HEAD" reads only the headers (no body), so a big PDF can be checked without downloading it."""
    check_host(url)
    opener = urllib.request.build_opener(_CheckedRedirects)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}, method=method)
    with opener.open(req, timeout=TIMEOUT) as resp:
        body = resp.read(max_bytes + 1) if method != "HEAD" else b""
        if len(body) > max_bytes:
            raise ValueError("answer is larger than the size limit")
        return resp.status, {k.lower(): v for k, v in resp.headers.items()}, body


# ---------------------------------------------------------------------------
# Reading each kind of source
# ---------------------------------------------------------------------------

def ecfr_api_url(url):
    """The versioner API address for an eCFR part link (a /current/title-14/part-91 page or the API itself)."""
    t = re.search(r"title[-=](\d+)", url)
    p = re.search(r"part[-=](\d+)", url)
    if not (t and p):
        raise ValueError("couldn't find the title and part in this eCFR link")
    return f"https://www.ecfr.gov/api/versioner/v1/versions/title-{t.group(1)}.json?part={p.group(1)}"


def parse_ecfr_versions(body):
    """Latest amendment date (YYYY-MM-DD) from the eCFR versioner answer."""
    data = json.loads(body.decode("utf-8", "replace"))
    dates = [v.get("amendment_date") or v.get("date") or "" for v in data.get("content_versions", [])]
    dates = [d for d in dates if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d)]
    if not dates:
        raise ValueError("no amendment dates in the eCFR answer")
    return max(dates)


_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_DATE_RE = re.compile(rf"\b(?:{_MONTHS})\s+\d{{1,2}},\s+\d{{4}}\b|\b\d{{4}}-\d{{2}}-\d{{2}}\b")
_REV_RE = re.compile(r"\b(?:Change|Chg\.?)\s*\d+\b|\b(?:Edition|Revision|Rev\.?)\s*[A-Z0-9]{1,4}\b|\bAC\s*\d+-\d+[A-Z]?\b",
                     re.IGNORECASE)
_PDF_HREF_RE = re.compile(r"""href\s*=\s*["']([^"'#]+?\.pdf)(?:\?[^"']*)?["']""", re.IGNORECASE)


def parse_faa_page(html, base_url):
    """{'pdf_url', 'revision', 'date'} (each may be None) from a faa.gov publication page."""
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    pdf_url = None
    for m in _PDF_HREF_RE.finditer(text):
        cand = urllib.parse.urljoin(base_url, m.group(1))
        try:
            check_host(cand)
        except BlockedHost:
            continue
        pdf_url = cand
        break
    plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))
    rev = re.search(r"\b(?:Change|Chg\.?)\s*\d+\b|\b(?:Edition|Revision)\s*[A-Z0-9]{1,4}\b", plain, re.I)
    date = _DATE_RE.search(plain)
    return {"pdf_url": pdf_url, "revision": rev.group(0) if rev else None, "date": date.group(0) if date else None}


def parse_date(text):
    """'YYYY-MM-DD' from 'October 2, 2025', 'Oct 2, 2025' or '2025-10-02'; None when it isn't a date."""
    t = (text or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        return t
    m = re.fullmatch(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),\s+(\d{4})", t)
    if m:
        word = m.group(1).lower()
        for i, name in enumerate(_MONTHS.split("|"), 1):
            if word in (name.lower(), name.lower()[:3]):
                return f"{m.group(3)}-{i:02d}-{int(m.group(2)):02d}"
    return None


def parse_aim_page(html):
    """{'effective': 'YYYY-MM-DD' or None, 'change': 'Change N' or None} from the FAA's AIM start page."""
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))
    eff = re.search(rf"Effective(?:\s+Date)?\s*:?\s*((?:{_MONTHS})\s+\d{{1,2}},\s+\d{{4}}|\d{{4}}-\d{{2}}-\d{{2}})", plain, re.I)
    chg = re.search(r"\bChange\s+(\d+)\b", plain, re.I)
    return {"effective": parse_date(eff.group(1)) if eff else None, "change": f"Change {chg.group(1)}" if chg else None}


def validate_pdf(data):
    """(ok, reason). A file is only kept when it really is a modest-size PDF with nothing active in it."""
    if not data.startswith(b"%PDF"):
        return False, "not a PDF (doesn't start with %PDF)"
    if len(data) > MAX_PDF_BYTES:
        return False, "larger than the size limit"
    for tok in FORBIDDEN_PDF_TOKENS:
        # whole-name match so "/JS" doesn't trip on "/JSON" style names
        if re.search(re.escape(tok) + rb"(?![A-Za-z0-9])", data):
            return False, f"contains {tok.decode()}"
    return True, ""


def is_new_version(last_seen, current):
    """True only when we have seen this source before and the version changed.
    The first look just records a baseline."""
    return bool(last_seen) and bool(current) and last_seen != current


# ---------------------------------------------------------------------------
# Summary of changes
# ---------------------------------------------------------------------------

_UNIT_RE = re.compile(r"^\s*(?:§\s*)?(\d{1,4}(?:[.-]\d+)+[A-Za-z]?)\.?\s+(?=\S)")


def split_units(pages):
    """{section number: (text, first page)} for pages [(page_num, text)]. Falls back to
    one unit per page ('Page 3') when the text has no section numbers."""
    units, order = {}, []
    cur = None
    for num, text in pages:
        for line in (text or "").splitlines():
            m = _UNIT_RE.match(line)
            if m:
                cur = m.group(1)
                if cur not in units:
                    units[cur] = [num, []]
                    order.append(cur)
                units[cur][1].append(line.strip())
            elif cur:
                units[cur][1].append(line.strip())
    if len(units) < 3:
        units = {f"Page {n}": [n, [(t or "").strip()]] for n, t in pages if (t or "").strip()}
    return {k: (re.sub(r"\s+", " ", " ".join(v[1])).strip(), v[0]) for k, v in units.items()}


def _label(key):
    return key if key.startswith("Page ") else f"Section {key}"


def _snippet(text, n=140):
    text = text.strip()
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def _word_diff(old, new):
    a, b = old.split(), new.split()
    removed, added = [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op in ("delete", "replace"):
            removed.append(" ".join(a[i1:i2]))
        if op in ("insert", "replace"):
            added.append(" ".join(b[j1:j2]))
    return _snippet(" ".join(removed), 110), _snippet(" ".join(added), 110)


def build_summary(old_pages, new_pages):
    """Plain-words list of what changed between two [(page_num, text)] lists."""
    old, new = split_units(old_pages), split_units(new_pages)
    added = [k for k in new if k not in old]
    removed = [k for k in old if k not in new]
    changed = []
    for k in new:
        if k in old and old[k][0] != new[k][0]:
            ratio = difflib.SequenceMatcher(None, old[k][0], new[k][0], autojunk=False).ratio() if \
                len(old[k][0]) + len(new[k][0]) < 20000 else 0.5
            changed.append((len(new[k][0]) * (1 - ratio) + 1, k))
    changed.sort(reverse=True)
    if not (added or removed or changed):
        return "No wording changes found in the pages we could compare."
    unit = "pages" if any(k.startswith("Page ") for k in list(old) + list(new)) else "sections"
    lines = [f"{len(changed)} {unit} changed, {len(added)} added, {len(removed)} removed."]
    items = []
    for _w, k in changed:
        rem, add = _word_diff(old[k][0], new[k][0])
        bit = f"Changed: {_label(k)} (page {new[k][1]})"
        if rem:
            bit += f". Was: \"{rem}\""
        if add:
            bit += f". Now: \"{add}\""
        items.append(bit)
    items += [f"Added: {_label(k)} (page {new[k][1]}): {_snippet(new[k][0], 100)}" for k in added]
    items += [f"Removed: {_label(k)} (was page {old[k][1]})" for k in removed]
    if len(items) > MAX_SUMMARY_ITEMS:
        lines.append(f"Too many changes to list. Showing the biggest {MAX_SUMMARY_ITEMS} of {len(items)}.")
        items = items[:MAX_SUMMARY_ITEMS]
    return "\n".join(lines + items)


# ---------------------------------------------------------------------------
# Page text of a PDF
# ---------------------------------------------------------------------------

def extract_pages(pdf_path):
    import pdfplumber
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            pages.append((i, page.extract_text() or ""))
    return pages


# ---------------------------------------------------------------------------
# The scan itself
# ---------------------------------------------------------------------------

def _pdf_fingerprint(headers):
    """'hdr:...' from a PDF's size, ETag and Last-Modified headers, or None when the site gives none."""
    parts = [headers.get(k, "").strip() for k in ("etag", "content-length", "last-modified")]
    return "hdr:" + "|".join(parts) if any(parts) else None


def _look(src, fetch, res=None, sleep=time.sleep, pause=PAUSE):
    """What the source shows now: {'version', 'date', 'revision', 'pdf' (bytes or None), 'pdf_url',
    'alert' (text for an alert-only source, else None)}."""
    kind, url = src["source_kind"], src["source_url"]
    if kind == "ecfr":
        _s, _h, body = fetch(ecfr_api_url(url))
        d = parse_ecfr_versions(body)
        return {"version": d, "date": d, "revision": None, "pdf": None, "pdf_url": None}
    if kind == "ecfr_multi":
        return _look_ecfr_multi(src, fetch, res, sleep, pause)
    if kind == "faa_alert":
        return _look_aim(src, fetch, res)
    if kind == "faa_page":
        _s, _h, body = fetch(url)
        info = parse_faa_page(body.decode("utf-8", "replace"), url)
        if not (info["pdf_url"] or info["revision"] or info["date"]):
            raise ValueError("couldn't find an edition, date or PDF link on the page")
        key = "|".join(str(info[k] or "") for k in ("pdf_url", "revision", "date"))
        return {"version": key, "date": info["date"], "revision": info["revision"], "pdf": None,
                "pdf_url": info["pdf_url"]}
    # A direct PDF link: ask for the headers first, so an unchanged big PDF is never downloaded.
    try:
        _s, h, _b = fetch(url, MAX_PAGE_BYTES, method="HEAD")
        fp = _pdf_fingerprint(h or {})
    except BlockedHost:
        raise
    except Exception:
        fp = None
    if fp:
        return {"version": fp, "date": None, "revision": None, "pdf": None, "pdf_url": url}
    _s, _h, body = fetch(url, MAX_PDF_BYTES)
    return {"version": hashlib.sha256(body).hexdigest()[:16], "date": None, "revision": None, "pdf": body,
            "pdf_url": url}


def _look_ecfr_multi(src, fetch, res, sleep, pause):
    """Latest amendment date of each listed eCFR part. An alert only: no PDF is built or fetched."""
    parts = parse_ecfr_multi(src["source_url"])
    dates = {}
    for i, (title, part) in enumerate(parts):
        if i:
            sleep(pause)
        _s, _h, body = fetch(f"https://www.ecfr.gov/api/versioner/v1/versions/title-{title}.json?part={part}")
        dates[(title, part)] = parse_ecfr_versions(body)
    version = "|".join(f"{t}-{p}:{d}" for (t, p), d in dates.items())
    doc_date = parse_date(res["doc_date"]) if res else None
    if doc_date:
        changed = {k: d for k, d in dates.items() if d > doc_date}
    else:  # no usable date on the resource: compare with what the last check saw
        last = dict(x.split(":", 1) for x in (src["last_seen_version"] or "").split("|") if ":" in x)
        changed = {k: d for k, d in dates.items() if last and last.get(f"{k[0]}-{k[1]}") not in (None, d)}
    seen = {"version": version, "date": max(dates.values()), "revision": None, "pdf": None, "pdf_url": None,
            "alert": None, "alert_key": None}
    if changed:
        names = ", ".join(f"{t} CFR Part {p} (amended {d})" for (t, p), d in changed.items())
        base = f"the file's date {doc_date}" if doc_date else "the last check"
        seen["alert"] = (f"{len(changed)} of the {len(parts)} eCFR parts in this file changed since {base}: {names}. "
                         "Nothing was downloaded or replaced. Rebuild the combined PDF from the current eCFR text "
                         "and upload it with Edit, then dismiss this.")
        seen["alert_key"] = "|".join(f"{t}-{p}:{d}" for (t, p), d in changed.items())
        seen["date"] = max(changed.values())
    return seen


def _look_aim(src, fetch, res):
    """The AIM start page's 'Effective' date and 'Change N'. An alert only: no file swap is offered."""
    _s, _h, body = fetch(src["source_url"])
    info = parse_aim_page(body.decode("utf-8", "replace"))
    if not (info["effective"] or info["change"]):
        raise ValueError("couldn't find an Effective date or Change number on the page")
    version = f"{info['effective'] or ''}|{info['change'] or ''}"
    differs = None  # compare with what the resource row says, wherever that can be read
    if res:
        m = re.search(r"\d+", res["revision"] or "")
        if info["change"] and m:
            differs = int(m.group(0)) != int(info["change"].split()[1])
        eff = parse_date(res["effectivity"])
        if info["effective"] and eff and not differs:
            differs = eff != info["effective"]
    seen = {"version": version, "date": info["effective"], "revision": info["change"], "pdf": None,
            "pdf_url": None, "alert": None, "alert_key": None, "differs": differs}
    if differs is None and src["last_seen_version"]:
        differs = src["last_seen_version"] != version   # nothing on the row to compare: use the last check
    if differs:
        seen["alert"] = (f"The FAA's AIM page now shows {info['change'] or 'no change number'}, effective "
                         f"{info['effective'] or 'date not shown'}; this file says {res['revision'] or 'no revision'}"
                         f", effective {res['effectivity'] or 'date not shown'}. Nothing was downloaded or "
                         "replaced. Get the new AIM from the source link, update both AIM parts, then dismiss this.")
        seen["alert_key"] = version
    return seen


def acs_standards_summary(conn, rating_id, pdf_path):
    """For an ACS resource: what the new file changes in the Ground School course, in plain words
    (read-only; nothing is merged until an admin approves)."""
    import acs_sync
    try:
        areas = acs_sync.parse_pdf(pdf_path)
        report = acs_sync.diff_acs(conn, rating_id, areas)
    except Exception as e:
        return ("ACS standards: couldn't read the Tasks in the new file "
                f"({str(e)[:100]}). Approving will replace the file but leave the lessons as they are.")
    if acs_sync.looks_unreadable(report):
        return ("ACS standards: the new file showed too few of the course's Tasks to trust. Approving will "
                "replace the file but leave the lessons as they are.")
    text = acs_sync.format_report(report, acs_sync.lesson_task_keys(conn, rating_id))
    return text + "\nApproving also updates the Ground School course and flags the lesson plans that need a refresh."


def _file_update(conn, src, res, seen, fetch, extract, qdir):
    """Quarantine the new PDF (if there is one), index it and file the pending update."""
    data, blocked = seen["pdf"], ""
    if seen.get("alert"):
        cur = conn.execute(
            "INSERT INTO resource_updates (resource_id, source_id, version_key, new_filename, new_designation, "
            "new_doc_date, new_revision, new_effectivity, summary, blocked_reason, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?)",
            (res["id"], src["id"], seen["alert_key"], None, res["designation"], seen["date"], seen["revision"],
             None, seen["alert"], None, now_iso()))
        return cur.lastrowid
    if data is None and seen["pdf_url"]:
        data = fetch(seen["pdf_url"], MAX_PDF_BYTES)[2]
    new_file, new_pages = None, []
    if data is not None:
        ok, why = validate_pdf(data)
        if not ok:
            blocked = why
        else:
            os.makedirs(qdir, exist_ok=True)
            new_file = f"upd_{src['id']}_{int(time.time())}.pdf"
            path = os.path.join(qdir, new_file)
            with open(path, "wb") as fh:
                fh.write(data)
            try:
                new_pages = extract(path)
            except Exception:
                new_pages = []
    old_pages = [(r["page_num"], r["text"]) for r in conn.execute(
        "SELECT page_num, text FROM acs_resource_pages WHERE resource_id = ? ORDER BY page_num", (res["id"],))]
    facts = []
    if seen["date"]:
        facts.append(f"The source now shows date {seen['date']}" + (f" ({seen['revision']})" if seen["revision"] else "") + ".")
    if src["source_kind"] == "ecfr":
        facts = [f"eCFR shows this part was last amended on {seen['date']} (we last saw {src['last_seen_version']})."]
    if blocked:
        summary = " ".join(facts + [f"The new file was NOT saved because it failed the safety check ({blocked}). "
                                    "Open the source link to look at it yourself."])
    elif new_file and new_pages and old_pages:
        summary = "\n".join(facts + [build_summary(old_pages, new_pages)])
    elif new_file:
        summary = " ".join(facts + ["The new PDF is ready. There is no earlier page text to compare it with."])
    else:
        summary = " ".join(facts + ["No PDF could be fetched automatically. Open the source link, then upload the "
                                    "new PDF with Add Resource if you want it in the library."])
    if new_file and res["acs_rating_id"]:
        summary = "\n".join([summary, acs_standards_summary(conn, res["acs_rating_id"], os.path.join(qdir, new_file))])
    cur = conn.execute(
        "INSERT INTO resource_updates (resource_id, source_id, version_key, new_filename, new_designation, "
        "new_doc_date, new_revision, new_effectivity, summary, blocked_reason, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?)",
        (res["id"], src["id"], seen["version"], new_file, res["designation"], seen["date"], seen["revision"],
         None, summary, blocked or None, now_iso()))
    uid = cur.lastrowid
    conn.executemany("INSERT INTO resource_update_pages (update_id, page_num, text) VALUES (?,?,?)",
                     [(uid, n, t) for n, t in new_pages])
    return uid


def _legacy_pdf_baseline(src, seen):
    """A PDF link last seen by its hash (before big PDFs were checked by headers) or the other way round:
    the two kinds of version can't be compared, so the new one just becomes the baseline."""
    last, cur = src["last_seen_version"] or "", seen["version"]
    return src["source_kind"] == "pdf_link" and last.startswith("hdr:") != cur.startswith("hdr:")


def scan_source(conn, src, fetch=default_fetch, extract=extract_pages, qdir=None, sleep=time.sleep, pause=PAUSE):
    """Checks one source. Returns 'update', 'ok' or 'baseline'. Raises on a problem."""
    res = conn.execute("SELECT * FROM acs_resources WHERE id = ?", (src["resource_id"],)).fetchone()
    if not res:
        return "ok"
    seen = _look(src, fetch, res, sleep, pause)
    state, msg = "ok", None
    if seen.get("alert"):
        key = seen["alert_key"]
        # one notice per source address (both AIM rows share one), and never the same version twice
        dup = conn.execute("SELECT u.status FROM resource_updates u JOIN resource_sources s ON s.id = u.source_id "
                           "WHERE s.source_url = ? AND u.version_key = ?", (src["source_url"], key)).fetchone()
        if dup:
            msg = ("Update found (already in the list)" if dup["status"] == "pending"
                   else "Up to date (this change was already dealt with)")
        else:
            _file_update(conn, src, res, seen, fetch, extract, qdir or quarantine_dir())
            state = "update"
    elif src["source_kind"] in ("ecfr_multi", "faa_alert"):
        state = "ok" if src["last_seen_version"] else "baseline"
    elif not src["last_seen_version"] or _legacy_pdf_baseline(src, seen):
        state = "baseline"
    elif is_new_version(src["last_seen_version"], seen["version"]):
        dup = conn.execute("SELECT 1 FROM resource_updates WHERE source_id = ? AND version_key = ?",
                           (src["id"], seen["version"])).fetchone()
        if not dup:
            _file_update(conn, src, res, seen, fetch, extract, qdir or quarantine_dir())
            state = "update"
    msg = msg or {"ok": "Up to date", "baseline": "Up to date (now watching)", "update": "Update found"}[state]
    conn.execute("UPDATE resource_sources SET last_checked_at = ?, last_seen_version = ?, last_seen_date = ?, "
                 "status = ?, last_message = ? WHERE id = ?",
                 (now_iso(), seen["version"], seen["date"] or src["last_seen_date"],
                  "update" if msg.startswith("Update found") else state, msg, src["id"]))
    conn.commit()
    return state


def run_scan(conn, trigger="cron", fetch=default_fetch, extract=extract_pages, sleep=time.sleep,
             pause=PAUSE, qdir=None, run_id=None):
    """Checks every source, one at a time with a pause between, and never stops on one failure.
    Returns {'checked', 'found', 'failed'}."""
    if run_id is None:
        run_id = conn.execute("INSERT INTO resource_scan_runs (started_at, trigger) VALUES (?, ?)",
                              (now_iso(), trigger)).lastrowid
        conn.commit()
    checked = found = failed = 0
    sources = conn.execute("SELECT * FROM resource_sources ORDER BY id").fetchall()
    for i, src in enumerate(sources):
        if i:
            sleep(pause)
        checked += 1
        try:
            if scan_source(conn, src, fetch, extract, qdir, sleep, pause) == "update":
                found += 1
        except BlockedHost:
            failed += 1
            _record_failure(conn, src, "Blocked: not an allowed site")
        except Exception as e:  # one bad source never stops the rest
            failed += 1
            _record_failure(conn, src, f"Could not reach: {str(e)[:120]}")
    conn.execute("UPDATE resource_scan_runs SET finished_at = ?, checked = ?, found = ?, failed = ? WHERE id = ?",
                 (now_iso(), checked, found, failed, run_id))
    conn.commit()
    if found:
        notify_admins(conn, found)
    return {"checked": checked, "found": found, "failed": failed}


def _record_failure(conn, src, msg):
    conn.execute("UPDATE resource_sources SET last_checked_at = ?, status = 'failed', last_message = ? WHERE id = ?",
                 (now_iso(), msg, src["id"]))
    conn.commit()


def notify_admins(conn, found):
    """One phone alert per scan to the master admin and flight admins (the app's normal web push)."""
    try:
        import push
        admins = conn.execute(
            "SELECT id FROM users WHERE active = 1 AND (is_master_admin = 1 OR flight_role = 'admin' "
            "OR flight_roles LIKE '%admin%')").fetchall()
        body = f"{found} Flight Academy resource update{'s' if found != 1 else ''} waiting for your approval."
        for a in admins:
            push.queue_and_push(conn, a["id"], "Resource updates found", body, tag="resource-updates",
                                url="/flight/groundschool/resources")
    except Exception as e:
        print(f"alert failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    import db
    db.init_db()
    c = db.get_db()
    out = run_scan(c, "cron")
    c.close()
    print(f"{now_iso()} resource scan: {out['checked']} checked, {out['found']} updates found, {out['failed']} failed")
