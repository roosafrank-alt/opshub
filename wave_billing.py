"""Wave (waveapps.com) invoicing - the real-invoice side of Billing.

OpsHub already works out what each job / student owes. This module turns
that into an actual Wave invoice (customer, line items, due date), has Wave
email it with its own "Pay now" link (card / bank through Wave Payments,
when that's turned on in Wave), and checks back later so a job or its
flights flip to Paid on their own once the customer pays in Wave.

Wave only has a GraphQL API (https://gql.waveapps.com/graphql/public). It
is reached with a full-access token from the Wave developer portal, kept in
app_settings like the SMTP/Twilio settings (Admin > Wave). Every invoice
line needs a Wave product, so Admin > Wave also picks which of the
business's products stand for shop labor, parts and flight training.

Up to three Wave accounts can be set up (ACCOUNTS), each one Wave business
with its own products. An account can share another account's Wave login
(two businesses under one login) or have its own token. Each program - the
Shop and the Flight School - is given any of the accounts plus a default
one; the Invoice in Wave box preselects the default and can swap to another
of that program's accounts for any single invoice. Every invoice remembers
the account and business it was made in, so its payment is always looked up
in the right place.

Nothing here talks to the database except the settings/customer-cache
helpers - the callers in app.py / flight.py decide what goes on an invoice
and what happens once it's paid (see sync_open_invoices)."""
import json
import urllib.error
import urllib.request

from db import now_iso

API_URL = "https://gql.waveapps.com/graphql/public"

ACCOUNTS = (1, 2, 3)
ACCOUNT_FIELDS = ("name", "token", "token_from", "business_id", "business_name",
                  "labor_product_id", "parts_product_id", "flight_product_id")
PROGRAMS = {"shop": {"label": "Shop", "products": ("labor", "parts")},
            "flight": {"label": "Flight School", "products": ("flight",)}}
PRODUCT_LABELS = {"labor": "shop labor", "parts": "parts", "flight": "flight training"}
# Which program an invoice kind belongs to.
KIND_PROGRAM = {"project": "shop", "student": "flight"}


def account_key(n, field):
    return f"wave_acct{n}_{field}"


def program_key(program, field):
    """field: 'accounts' (comma list of account numbers) or 'default'."""
    return f"wave_{program}_{field}"


# Admin > Wave's on/off switch. Off hides every Invoice in Wave / Check
# Wave button and pauses the background payment check; all settings and
# invoices are kept for when it's switched back on. Missing = on.
ENABLED_KEY = "wave_enabled"

SETTINGS_KEYS = ([account_key(n, f) for n in ACCOUNTS for f in ACCOUNT_FIELDS]
                 + [program_key(p, f) for p in PROGRAMS for f in ("accounts", "default")]
                 + [ENABLED_KEY])

# Wave's own invoice statuses. Anything in here is finished - no need to
# keep asking Wave about it.
CLOSED_STATUSES = ("PAID",)


class WaveError(Exception):
    """Anything that went wrong talking to Wave, worded for the flash
    message Frank will see."""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def get_settings(conn):
    rows = conn.execute("SELECT key, value FROM app_settings WHERE key LIKE 'wave\\_%' ESCAPE '\\'").fetchall()
    found = {r["key"]: r["value"] for r in rows}
    return {k: found.get(k) or "" for k in SETTINGS_KEYS}


def save_settings(conn, values):
    """values: key -> new value (only SETTINGS_KEYS are saved)."""
    for key in SETTINGS_KEYS:
        if key in values:
            conn.execute("INSERT INTO app_settings (key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                         (key, (values[key] or "").strip()))
    conn.commit()


def account_config(raw, n):
    """Account n from get_settings(): {n, name, token (resolved - its own,
    or the login it shares), own_token, token_from, business_id,
    business_name, labor_product, parts_product, flight_product}."""
    def v(field):
        return raw.get(account_key(n, field), "")
    token_from = v("token_from")
    token = v("token")
    if not token and token_from.isdigit() and int(token_from) in ACCOUNTS and int(token_from) != n:
        token = raw.get(account_key(int(token_from), "token"), "")
    return {"n": n, "name": v("name") or v("business_name") or f"Account {n}", "own_token": v("token"),
            "token": token, "token_from": token_from, "business_id": v("business_id"),
            "business_name": v("business_name"), "labor_product": v("labor_product_id"),
            "parts_product": v("parts_product_id"), "flight_product": v("flight_product_id")}


def is_connected(cfg):
    return bool(cfg and cfg["token"] and cfg["business_id"])


def is_enabled(raw):
    return raw.get(ENABLED_KEY, "") != "0"


TURNED_OFF = "Wave invoicing is turned off - switch it back on under Admin > Wave."


def program_accounts(conn, program):
    """(connected accounts given to this program, default account number or
    None). The default is the saved one if it's still connected, else the
    first connected account."""
    raw = get_settings(conn)
    if not is_enabled(raw):
        return [], None
    wanted = [int(x) for x in raw[program_key(program, "accounts")].split(",") if x.strip().isdigit()]
    accounts = [account_config(raw, n) for n in ACCOUNTS if n in wanted]
    accounts = [a for a in accounts if is_connected(a)]
    default = raw[program_key(program, "default")]
    numbers = [a["n"] for a in accounts]
    default = int(default) if default.isdigit() and int(default) in numbers else (numbers[0] if numbers else None)
    return accounts, default


def choose_account(conn, program, requested=None):
    """The account to invoice from: the one picked in the Invoice in Wave
    box (must be one of this program's), else the program's default."""
    if not is_enabled(get_settings(conn)):
        raise WaveError(TURNED_OFF)
    accounts, default = program_accounts(conn, program)
    if not accounts:
        raise WaveError(f"Wave isn't connected for the {PROGRAMS[program]['label']} yet - set it up under Admin > Wave.")
    n = default
    if requested not in (None, ""):
        try:
            n = int(requested)
        except (TypeError, ValueError):
            n = -1
        if n not in [a["n"] for a in accounts]:
            raise WaveError(f"That Wave account isn't set up for the {PROGRAMS[program]['label']}.")
    return next(a for a in accounts if a["n"] == n)


# ---------------------------------------------------------------------------
# GraphQL transport
# ---------------------------------------------------------------------------

def _gql(token, query, variables=None, timeout=20):
    """POSTs one GraphQL request and returns its "data". Raises WaveError
    for a bad token, a network problem, or GraphQL errors."""
    if not token:
        raise WaveError("Wave isn't connected yet - add the access token under Admin > Wave.")
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "User-Agent": "OpsHub/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise WaveError("Wave refused the access token - check it under Admin > Wave.")
        raise WaveError(f"Wave answered with an error (HTTP {e.code}).")
    except Exception as e:  # URLError, timeout, bad JSON
        raise WaveError(f"Couldn't reach Wave: {e}")
    if payload.get("errors"):
        msgs = "; ".join(err.get("message", "unknown error") for err in payload["errors"])
        if any((err.get("extensions") or {}).get("code") == "UNAUTHENTICATED" for err in payload["errors"]):
            raise WaveError("Wave refused the access token - check it under Admin > Wave.")
        raise WaveError("Wave: " + msgs)
    return payload.get("data") or {}


def _check_mutation(result, what):
    """Wave mutations answer didSucceed + inputErrors instead of GraphQL
    errors when the input itself is wrong."""
    if not result or not result.get("didSucceed"):
        errs = (result or {}).get("inputErrors") or []
        detail = "; ".join(e.get("message", "") for e in errs if e.get("message")) or "no reason given"
        raise WaveError(f"Wave couldn't {what}: {detail}")
    return result


# ---------------------------------------------------------------------------
# Reads (Admin > Wave pickers, payment check)
# ---------------------------------------------------------------------------

def list_businesses(token):
    data = _gql(token, """query { businesses(page: 1, pageSize: 50) {
        edges { node { id name isPersonal currency { code } } } } }""")
    return [e["node"] for e in data["businesses"]["edges"]]


def list_products(token, business_id):
    data = _gql(token, """query ($businessId: ID!) { business(id: $businessId) {
        products(page: 1, pageSize: 200) { edges { node { id name unitPrice isSold isArchived } } } } }""",
                {"businessId": business_id})
    products = [e["node"] for e in data["business"]["products"]["edges"]]
    return [p for p in products if p.get("isSold", True) and not p.get("isArchived")]


def get_invoice(token, business_id, invoice_id):
    data = _gql(token, """query ($businessId: ID!, $invoiceId: ID!) { business(id: $businessId) {
        invoice(id: $invoiceId) { id invoiceNumber status viewUrl pdfUrl
                                  total { value } amountDue { value } amountPaid { value } } } }""",
                {"businessId": business_id, "invoiceId": invoice_id})
    inv = (data.get("business") or {}).get("invoice")
    if not inv:
        raise WaveError("That invoice no longer exists in Wave.")
    return inv


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

def _customer_cache_key(business_id, email):
    return f"wavecust:{business_id}:{email.strip().lower()}"


def forget_business_customers(conn, business_id):
    """Drops remembered customer ids for a business (after disconnecting it)."""
    conn.execute("DELETE FROM app_settings WHERE key LIKE ?", (f"wavecust:{business_id}:%",))


def find_or_create_customer(conn, cfg, name, email):
    """Wave customer id for this person in this account's business. Remembered
    per business + email address in app_settings, then looked up in Wave by
    email (so a customer Frank already has in Wave is reused, not
    duplicated), then created."""
    token, business_id = cfg["token"], cfg["business_id"]
    email = (email or "").strip()
    name = (name or "").strip() or email
    if email:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (_customer_cache_key(business_id, email),)).fetchone()
        if row and row["value"]:
            return row["value"]
        try:
            data = _gql(token, """query ($businessId: ID!, $email: String) { business(id: $businessId) {
                customers(page: 1, pageSize: 5, email: $email) { edges { node { id email } } } } }""",
                        {"businessId": business_id, "email": email})
            for e in data["business"]["customers"]["edges"]:
                if (e["node"].get("email") or "").strip().lower() == email.lower():
                    _remember_customer(conn, business_id, email, e["node"]["id"])
                    return e["node"]["id"]
        except WaveError:
            pass  # lookup is only a nicety - fall through and create
    data = _gql(token, """mutation ($input: CustomerCreateInput!) { customerCreate(input: $input) {
        didSucceed inputErrors { code message path } customer { id } } }""",
                {"input": {"businessId": business_id, "name": name, **({"email": email} if email else {})}})
    result = _check_mutation(data.get("customerCreate"), "add the customer")
    cust_id = result["customer"]["id"]
    if email:
        _remember_customer(conn, business_id, email, cust_id)
    return cust_id


def _remember_customer(conn, business_id, email, cust_id):
    conn.execute("INSERT INTO app_settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (_customer_cache_key(business_id, email), cust_id))
    conn.commit()


def forget_customer(conn, business_id, email):
    if email:
        conn.execute("DELETE FROM app_settings WHERE key = ?", (_customer_cache_key(business_id, email),))
        conn.commit()


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------

_INVOICE_FIELDS = "id invoiceNumber status viewUrl pdfUrl total { value } amountDue { value }"


def create_invoice(conn, cfg, customer_name, customer_email, items, memo="", po_number="", status="SAVED"):
    """Creates an approved (SAVED) invoice, or a DRAFT one (the Admin > Wave
    test - drafts stay out of Wave's books and can't be sent). items: dicts with
    productId, description, quantity, unitPrice. Returns Wave's invoice
    (id, invoiceNumber, status, viewUrl, pdfUrl, total, amountDue)."""
    if not items:
        raise WaveError("Nothing to invoice - no charges found.")
    if not is_connected(cfg):
        raise WaveError(f"Wave {cfg['name']} isn't connected - check it under Admin > Wave.")
    customer_id = find_or_create_customer(conn, cfg, customer_name, customer_email)
    variables = {"input": {
        "businessId": cfg["business_id"],
        "customerId": customer_id,
        "status": status,
        "items": [{"productId": i["productId"], "description": i["description"][:255],
                   "quantity": i["quantity"], "unitPrice": i["unitPrice"]} for i in items],
        **({"memo": memo} if memo else {}),
        **({"poNumber": po_number} if po_number else {}),
    }}
    query = "mutation ($input: InvoiceCreateInput!) { invoiceCreate(input: $input) { didSucceed inputErrors { code message path } invoice { %s } } }" % _INVOICE_FIELDS
    try:
        data = _gql(cfg["token"], query, variables)
        return _check_mutation(data.get("invoiceCreate"), "create the invoice")["invoice"]
    except WaveError as e:
        # A remembered customer that was since deleted in Wave - forget it
        # so the next try makes a fresh one.
        if "customer" in str(e).lower():
            forget_customer(conn, cfg["business_id"], customer_email)
        raise


def send_invoice(cfg, invoice_id, to_email, message=""):
    """Has Wave email the invoice (with its PDF and online Pay link)."""
    if not to_email:
        raise WaveError("No email address to send the invoice to.")
    data = _gql(cfg["token"],
                """mutation ($input: InvoiceSendInput!) { invoiceSend(input: $input) {
                    didSucceed inputErrors { code message path } } }""",
                {"input": {"invoiceId": invoice_id, "to": [to_email.strip()], "attachPDF": True,
                           **({"message": message} if message else {})}})
    _check_mutation(data.get("invoiceSend"), "email the invoice")


def money(v):
    """Wave's Money {value} -> float (value comes back as a string)."""
    try:
        return round(float((v or {}).get("value") or 0), 2)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Local invoice records (wave_invoices table)
# ---------------------------------------------------------------------------

def record_invoice(conn, kind, ref_id, inv, customer_name, customer_email, created_by, cfg, sent=False):
    """Saves the Wave invoice against the job ('project') or student
    ('student') it was made for, and the account (cfg) it was made in.
    Returns the new wave_invoices.id."""
    cur = conn.execute("""INSERT INTO wave_invoices (kind, ref_id, wave_invoice_id, account, business_id, invoice_number, status,
                              view_url, pdf_url, total, amount_due, customer_name, customer_email, sent_at, created_at, created_by)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (kind, ref_id, inv["id"], cfg["n"], cfg["business_id"], inv.get("invoiceNumber"), inv.get("status"),
                        inv.get("viewUrl"), inv.get("pdfUrl"), money(inv.get("total")), money(inv.get("amountDue")),
                        customer_name, customer_email, now_iso() if sent else None, now_iso(), created_by))
    return cur.lastrowid


def has_any_connected(conn):
    raw = get_settings(conn)
    return is_enabled(raw) and any(is_connected(account_config(raw, n)) for n in ACCOUNTS)


def sync_open_invoices(conn, on_paid, only_id=None, program=None):
    """Asks Wave about every invoice that isn't paid yet (just one
    program's with program=, or just only_id) and saves its status. Each is
    asked in the account and business it was made in. For each one that's
    newly paid, calls on_paid(conn, wave_invoices_row) so the caller can
    mark the job/flights paid. Returns (checked, newly_paid, errors)."""
    raw = get_settings(conn)
    if not is_enabled(raw):
        raise WaveError(TURNED_OFF)
    accounts = {n: account_config(raw, n) for n in ACCOUNTS}
    if not any(is_connected(a) for a in accounts.values()):
        raise WaveError("Wave isn't connected yet - set it up under Admin > Wave.")
    sql = "SELECT * FROM wave_invoices WHERE paid_applied_at IS NULL"
    params = []
    if program:
        kinds = [k for k, p in KIND_PROGRAM.items() if p == program]
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"
        params += kinds
    if only_id:
        sql += " AND id = ?"
        params.append(only_id)
    checked, newly_paid, errors = 0, 0, []
    for row in conn.execute(sql, params).fetchall():
        cfg = accounts.get(row["account"] or 1)
        label = f"#{row['invoice_number'] or row['id']}"
        if not cfg or not cfg["token"]:
            errors.append(f"{label}: made in Wave account {row['account'] or 1}, which isn't connected any more.")
            continue
        try:
            inv = get_invoice(cfg["token"], row["business_id"] or cfg["business_id"], row["wave_invoice_id"])
        except WaveError as e:
            errors.append(f"{label}: {e}")
            continue
        checked += 1
        conn.execute("""UPDATE wave_invoices SET status = ?, amount_due = ?, amount_paid = ?, view_url = COALESCE(?, view_url),
                               pdf_url = COALESCE(?, pdf_url), last_checked_at = ? WHERE id = ?""",
                     (inv.get("status"), money(inv.get("amountDue")), money(inv.get("amountPaid")),
                      inv.get("viewUrl"), inv.get("pdfUrl"), now_iso(), row["id"]))
        if inv.get("status") in CLOSED_STATUSES:
            on_paid(conn, row)
            conn.execute("UPDATE wave_invoices SET paid_applied_at = ? WHERE id = ?", (now_iso(), row["id"]))
            newly_paid += 1
        conn.commit()
    return checked, newly_paid, errors


def test_account(conn, n):
    """Admin > Wave > Test connection: read-only checks on account n as
    saved. Returns [(ok, message)] - the login, the business, and each
    product the programs it's ticked for need."""
    raw = get_settings(conn)
    cfg = account_config(raw, n)
    results = []
    if not cfg["token"]:
        return [(False, "No access token saved" + (" (the account it shares a login with has none)" if cfg["token_from"] else "") + ".")]
    try:
        businesses = list_businesses(cfg["token"])
    except WaveError as e:
        return [(False, str(e))]
    results.append((True, f"Wave accepted the access token ({len(businesses)} business{'es' if len(businesses) != 1 else ''} on this login)."))
    biz = next((b for b in businesses if b["id"] == cfg["business_id"]), None)
    if not cfg["business_id"]:
        return results + [(False, "No business picked yet.")]
    if not biz:
        return results + [(False, "The picked business isn't on this Wave login any more - pick it again.")]
    results.append((True, f"Business found: {biz['name']}."))
    try:
        products = {p["id"]: p["name"] for p in list_products(cfg["token"], cfg["business_id"])}
    except WaveError as e:
        return results + [(False, str(e))]
    programs = [p for p in PROGRAMS if str(n) in raw[program_key(p, "accounts")].split(",")]
    if not programs:
        results.append((False, "Not ticked for the Shop or the Flight School, so no Billing page uses it yet."))
    for p in programs:
        for kind in PROGRAMS[p]["products"]:
            pid = cfg[kind + "_product"]
            if not pid:
                results.append((False, f"{PROGRAMS[p]['label']}: no product picked for {PRODUCT_LABELS[kind]}."))
            elif pid not in products:
                results.append((False, f"{PROGRAMS[p]['label']}: the {PRODUCT_LABELS[kind]} product is gone or archived in Wave - pick another."))
            else:
                results.append((True, f"{PROGRAMS[p]['label']}: {PRODUCT_LABELS[kind]} goes under \"{products[pid]}\"."))
    return results


def create_test_invoice(conn, n):
    """Admin > Wave > Make a test invoice: a $1.00 DRAFT in account n for a
    customer called "OpsHub test", using the first product it has. Proves
    the whole invoice path works without anything reaching Wave's books or
    anyone's inbox. Not recorded against any job or student."""
    cfg = account_config(get_settings(conn), n)
    if not is_connected(cfg):
        raise WaveError(f"{cfg['name']} isn't connected yet - save its token and business first.")
    product = cfg["labor_product"] or cfg["parts_product"] or cfg["flight_product"]
    if not product:
        raise WaveError(f"Pick at least one product for {cfg['name']} first.")
    return create_invoice(conn, cfg, "OpsHub test", "",
                          [{"productId": product, "description": "OpsHub test invoice - safe to delete", "quantity": 1, "unitPrice": 1.0}],
                          memo="Test from OpsHub Admin > Wave. This draft can be deleted.", status="DRAFT")


def apply_paid(conn, row):
    """What "Wave says it's paid" means here - the same end state as the
    Billing pages' own Mark Paid: a shop job goes to Paid (method "Wave"),
    a student's flights on that invoice get their Paid tick. Never touches
    anything already paid by hand."""
    if row["kind"] == "project":
        conn.execute("""UPDATE projects SET payment_status = 'paid', paid_at = ?, paid_by = 'Wave', paid_method = 'Wave'
                        WHERE id = ? AND COALESCE(payment_status, '') != 'paid'""", (now_iso(), row["ref_id"]))
    elif row["kind"] == "student":
        conn.execute("UPDATE flights SET paid = 1 WHERE wave_invoice_id = ? AND paid = 0", (row["id"],))


def latest_for(conn, kind, ref_ids):
    """{ref_id: newest wave_invoices row} for the Billing pages."""
    ref_ids = list(ref_ids)
    if not ref_ids:
        return {}
    marks = ",".join("?" * len(ref_ids))
    rows = conn.execute(f"SELECT * FROM wave_invoices WHERE kind = ? AND ref_id IN ({marks}) ORDER BY id",
                        [kind] + ref_ids).fetchall()
    return {r["ref_id"]: dict(r) for r in rows}


# ---------------------------------------------------------------------------
# Invoice lines
# ---------------------------------------------------------------------------

def _line(product_id, description, amount, quantity=None, rate=None):
    """One invoice line. Shows hours x rate when that multiplies out to the
    exact amount OpsHub charged, otherwise 1 x amount so Wave's total always
    matches OpsHub's to the cent."""
    amount = round(amount or 0, 2)
    if quantity and rate and abs(round(quantity, 2) * rate - amount) < 0.005:
        return {"productId": product_id, "description": description, "quantity": round(quantity, 2),
                "unitPrice": round(rate, 2), "amount": amount}
    return {"productId": product_id, "description": description, "quantity": 1, "unitPrice": amount, "amount": amount}


def project_lines(conn, cfg, project_id):
    """The whole job (all dates, same as the Invoice CSV): parts at sell
    price per area worked on, then labor per worker/area/rate."""
    labor_product, parts_product = cfg["labor_product"], cfg["parts_product"]
    usage = conn.execute("""
        SELECT p.name, p.unit, COALESCE(p.sell_price, 0) as sell_price, t.section as section,
               SUM(CASE WHEN t.type='out' THEN t.qty ELSE -t.qty END) as qty_used
        FROM transactions t JOIN parts p ON p.id = t.part_id
        WHERE t.project_id = ? AND t.type IN ('out', 'in')
        GROUP BY p.id, t.section HAVING qty_used > 0 ORDER BY t.section, p.name""", (project_id,)).fetchall()
    labor = conn.execute("""
        SELECT l.name as laborer_name, ls.section, ls.rate, SUM(ls.hours) as hours, SUM(ls.cost) as cost
        FROM labor_sessions ls JOIN laborers l ON l.id = ls.laborer_id
        WHERE ls.project_id = ? AND ls.ended_at IS NOT NULL
        GROUP BY l.id, ls.section, ls.rate ORDER BY ls.section, l.name""", (project_id,)).fetchall()
    if usage and not parts_product:
        raise WaveError(f"Pick the Wave product for parts on {cfg['name']} under Admin > Wave first.")
    if labor and not labor_product:
        raise WaveError(f"Pick the Wave product for shop labor on {cfg['name']} under Admin > Wave first.")
    lines = []
    for u in usage:
        amount = (u["qty_used"] or 0) * (u["sell_price"] or 0)
        if amount <= 0:
            continue
        area = f"{u['section']}: " if u["section"] else ""
        qty = u["qty_used"]
        qty_txt = f"{qty:g}" + (f" {u['unit']}" if u["unit"] else "")
        lines.append(_line(parts_product, f"{area}{u['name']} ({qty_txt})", amount, qty, u["sell_price"]))
    for s in labor:
        if not s["cost"] or s["cost"] <= 0:
            continue
        lines.append(_line(labor_product,
                           f"Labor - {s['section'] or 'General'} ({s['laborer_name']}, {s['hours'] or 0:.2f} hr @ ${s['rate'] or 0:.2f}/hr)",
                           s["cost"], s["hours"], s["rate"]))
    return lines


def flight_lines(cfg, flights):
    """Each flight's plane / instructor / ground charges as its own line,
    from flight.py's already-costed rows (_row_with_cost)."""
    product = cfg["flight_product"]
    if not product:
        raise WaveError(f"Pick the Wave product for flight training on {cfg['name']} under Admin > Wave first.")
    lines = []
    for f in flights:
        when = f["flight_date"] or ""
        if len(when) == 10:
            when = f"{when[5:7]}/{when[8:10]}/{when[0:4]}"
        plane = f["plane_tag"] or "Plane"
        if f["plane_cost"] > 0:
            lines.append(_line(product, f"{when} {plane} - {f['hours']:.1f} hr", f["plane_cost"],
                               f["hours"], f.get("plane_rate")))
        if f["instructor_cost"] > 0:
            lines.append(_line(product, f"{when} Flight instruction - {f['cfi_name'] or 'Instructor'} "
                                        f"({f['instructor_hours']:.1f} hr)",
                               f["instructor_cost"], f["instructor_hours"], f.get("instructor_rate")))
        if f["ground_cost"] > 0:
            lines.append(_line(product, f"{when} Ground instruction - {f['cfi_name'] or 'Instructor'} "
                                        f"({f['ground_hours']:.1f} hr)",
                               f["ground_cost"], f["ground_hours"], f.get("instructor_rate")))
    return lines
