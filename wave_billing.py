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

Nothing here talks to the database except the settings/customer-cache
helpers - the callers in app.py / flight.py decide what goes on an invoice
and what happens once it's paid (see sync_open_invoices)."""
import json
import urllib.error
import urllib.request

from db import now_iso

API_URL = "https://gql.waveapps.com/graphql/public"

SETTINGS_KEYS = ["wave_access_token", "wave_business_id",
                 "wave_labor_product_id", "wave_parts_product_id", "wave_flight_product_id"]

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


def is_connected(settings):
    return bool(settings["wave_access_token"] and settings["wave_business_id"])


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

def _customer_cache_key(email):
    return "wavecust:" + email.strip().lower()


def find_or_create_customer(conn, settings, name, email):
    """Wave customer id for this person. Remembered per email address in
    app_settings, then looked up in Wave by email (so a customer Frank
    already has in Wave is reused, not duplicated), then created."""
    token, business_id = settings["wave_access_token"], settings["wave_business_id"]
    email = (email or "").strip()
    name = (name or "").strip() or email
    if email:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (_customer_cache_key(email),)).fetchone()
        if row and row["value"]:
            return row["value"]
        try:
            data = _gql(token, """query ($businessId: ID!, $email: String) { business(id: $businessId) {
                customers(page: 1, pageSize: 5, email: $email) { edges { node { id email } } } } }""",
                        {"businessId": business_id, "email": email})
            for e in data["business"]["customers"]["edges"]:
                if (e["node"].get("email") or "").strip().lower() == email.lower():
                    _remember_customer(conn, email, e["node"]["id"])
                    return e["node"]["id"]
        except WaveError:
            pass  # lookup is only a nicety - fall through and create
    data = _gql(token, """mutation ($input: CustomerCreateInput!) { customerCreate(input: $input) {
        didSucceed inputErrors { code message path } customer { id } } }""",
                {"input": {"businessId": business_id, "name": name, **({"email": email} if email else {})}})
    result = _check_mutation(data.get("customerCreate"), "add the customer")
    cust_id = result["customer"]["id"]
    if email:
        _remember_customer(conn, email, cust_id)
    return cust_id


def _remember_customer(conn, email, cust_id):
    conn.execute("INSERT INTO app_settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (_customer_cache_key(email), cust_id))
    conn.commit()


def forget_customer(conn, email):
    if email:
        conn.execute("DELETE FROM app_settings WHERE key = ?", (_customer_cache_key(email),))
        conn.commit()


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------

_INVOICE_FIELDS = "id invoiceNumber status viewUrl pdfUrl total { value } amountDue { value }"


def create_invoice(conn, settings, customer_name, customer_email, items, memo="", po_number=""):
    """Creates an approved (SAVED, not draft) invoice. items: dicts with
    productId, description, quantity, unitPrice. Returns Wave's invoice
    (id, invoiceNumber, status, viewUrl, pdfUrl, total, amountDue)."""
    if not items:
        raise WaveError("Nothing to invoice - no charges found.")
    customer_id = find_or_create_customer(conn, settings, customer_name, customer_email)
    variables = {"input": {
        "businessId": settings["wave_business_id"],
        "customerId": customer_id,
        "status": "SAVED",
        "items": [{"productId": i["productId"], "description": i["description"][:255],
                   "quantity": i["quantity"], "unitPrice": i["unitPrice"]} for i in items],
        **({"memo": memo} if memo else {}),
        **({"poNumber": po_number} if po_number else {}),
    }}
    query = "mutation ($input: InvoiceCreateInput!) { invoiceCreate(input: $input) { didSucceed inputErrors { code message path } invoice { %s } } }" % _INVOICE_FIELDS
    try:
        data = _gql(settings["wave_access_token"], query, variables)
        return _check_mutation(data.get("invoiceCreate"), "create the invoice")["invoice"]
    except WaveError as e:
        # A remembered customer that was since deleted in Wave - forget it
        # so the next try makes a fresh one.
        if "customer" in str(e).lower():
            forget_customer(conn, customer_email)
        raise


def send_invoice(settings, invoice_id, to_email, message=""):
    """Has Wave email the invoice (with its PDF and online Pay link)."""
    if not to_email:
        raise WaveError("No email address to send the invoice to.")
    data = _gql(settings["wave_access_token"],
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

def record_invoice(conn, kind, ref_id, inv, customer_name, customer_email, created_by, sent=False):
    """Saves the Wave invoice against the job ('project') or student
    ('student') it was made for. Returns the new wave_invoices.id."""
    cur = conn.execute("""INSERT INTO wave_invoices (kind, ref_id, wave_invoice_id, invoice_number, status, view_url,
                              pdf_url, total, amount_due, customer_name, customer_email, sent_at, created_at, created_by)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (kind, ref_id, inv["id"], inv.get("invoiceNumber"), inv.get("status"), inv.get("viewUrl"),
                        inv.get("pdfUrl"), money(inv.get("total")), money(inv.get("amountDue")),
                        customer_name, customer_email, now_iso() if sent else None, now_iso(), created_by))
    return cur.lastrowid


def sync_open_invoices(conn, on_paid, only_id=None):
    """Asks Wave about every invoice that isn't paid yet (or just only_id)
    and saves its status. For each one that's newly paid, calls
    on_paid(conn, wave_invoices_row) so the caller can mark the job/flights
    paid. Returns (checked, newly_paid, errors)."""
    settings = get_settings(conn)
    if not is_connected(settings):
        raise WaveError("Wave isn't connected yet - set it up under Admin > Wave.")
    sql = "SELECT * FROM wave_invoices WHERE paid_applied_at IS NULL"
    params = []
    if only_id:
        sql += " AND id = ?"
        params.append(only_id)
    checked, newly_paid, errors = 0, 0, []
    for row in conn.execute(sql, params).fetchall():
        try:
            inv = get_invoice(settings["wave_access_token"], settings["wave_business_id"], row["wave_invoice_id"])
        except WaveError as e:
            errors.append(f"#{row['invoice_number'] or row['id']}: {e}")
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


def project_lines(conn, settings, project_id):
    """The whole job (all dates, same as the Invoice CSV): parts at sell
    price per area worked on, then labor per worker/area/rate."""
    labor_product, parts_product = settings["wave_labor_product_id"], settings["wave_parts_product_id"]
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
        raise WaveError("Pick the Wave product to use for parts under Admin > Wave first.")
    if labor and not labor_product:
        raise WaveError("Pick the Wave product to use for labor under Admin > Wave first.")
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


def flight_lines(settings, flights):
    """Each flight's plane / instructor / ground charges as its own line,
    from flight.py's already-costed rows (_row_with_cost)."""
    product = settings["wave_flight_product_id"]
    if not product:
        raise WaveError("Pick the Wave product to use for flight training under Admin > Wave first.")
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
