"""Shipment tracking for Orders.

An order can carry a tracking number (and carrier - picked, or worked out
from the number's shape). The Orders page shows each pending order's latest
shipping status right on the list, refreshed in the background, and a click
expands the full scan history plus a link to the carrier's own page.

Live status comes from Shippo's tracking API (one plain HTTPS GET per
shipment, no extra package). It needs an API key in the environment, set
the same way as the FAA keys (a systemd override on the Pi):
    OPSHUB_SHIPPO_API_KEY=shippo_live_...
Without a key everything else still works - the number is saved and the
"track on UPS/FedEx/..." link opens the carrier's site - there's just no
live status on the list. Results are cached on the order and re-checked at
most every REFRESH_MINUTES, and never again once delivered.
"""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

SHIPPO_API_KEY = os.environ.get("OPSHUB_SHIPPO_API_KEY", "")
SHIPPO_TRACK_URL = "https://api.goshippo.com/tracks/{carrier}/{number}"
REFRESH_MINUTES = 30

CARRIERS = {
    "ups": {"label": "UPS", "shippo": "ups", "url": "https://www.ups.com/track?tracknum={n}"},
    "fedex": {"label": "FedEx", "shippo": "fedex", "url": "https://www.fedex.com/fedextrack/?trknbr={n}"},
    "usps": {"label": "USPS", "shippo": "usps", "url": "https://tools.usps.com/go/TrackConfirmAction?tLabels={n}"},
    "dhl": {"label": "DHL", "shippo": "dhl_express",
            "url": "https://www.dhl.com/us-en/home/tracking/tracking-express.html?submit=1&tracking-id={n}"},
}
CARRIER_CHOICES = [("", "Work it out from the number")] + [(k, v["label"]) for k, v in CARRIERS.items()] + \
                  [("other", "Other")]

STATUS_LABELS = {
    "PRE_TRANSIT": "Label created",
    "TRANSIT": "In transit",
    "DELIVERED": "Delivered",
    "RETURNED": "Returned to sender",
    "FAILURE": "Delivery problem",
    "UNKNOWN": "No updates yet",
}


def clean_number(raw):
    """Tracking number as typed/pasted -> uppercase, no spaces or dashes."""
    return re.sub(r"[\s-]+", "", (raw or "")).upper()[:40]


def guess_carrier(number):
    n = clean_number(number)
    if not n:
        return ""
    if re.fullmatch(r"1Z[0-9A-Z]{16}", n):
        return "ups"
    if re.fullmatch(r"(94|93|92|95)\d{20}", n) or re.fullmatch(r"[A-Z]{2}\d{9}US", n) or re.fullmatch(r"\d{22}", n):
        return "usps"
    if re.fullmatch(r"\d{12}|\d{15}|\d{20}", n):
        return "fedex"
    if re.fullmatch(r"\d{10}", n):
        return "dhl"
    return ""


def carrier_for(order):
    c = (order["tracking_carrier"] or "").lower()
    return c if c in CARRIERS else guess_carrier(order["tracking_number"])


def carrier_label(order):
    c = carrier_for(order)
    return CARRIERS[c]["label"] if c else "carrier"


def carrier_url(order):
    c = carrier_for(order)
    n = clean_number(order["tracking_number"])
    if not c or not n:
        return None
    return CARRIERS[c]["url"].format(n=urllib.parse.quote(n))


def needs_refresh(order, now=None):
    if not order["tracking_number"] or order["tracking_delivered_at"]:
        return False
    if not order["tracking_checked_at"]:
        return True
    try:
        checked = datetime.strptime(order["tracking_checked_at"], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True
    return (now or datetime.now()) - checked > timedelta(minutes=REFRESH_MINUTES)


def _place(loc):
    if not isinstance(loc, dict):
        return ""
    return ", ".join(x for x in (loc.get("city"), loc.get("state")) if x)


def _day(ts):
    return (ts or "")[:10] if isinstance(ts, str) else ""


def fetch_status(order):
    """Asks Shippo for the latest status. Returns a dict of the order's
    tracking_* columns to save, or {"error": "..."} if it couldn't."""
    if not SHIPPO_API_KEY:
        return {"error": "no_key"}
    c = carrier_for(order)
    if not c:
        return {"error": "unknown_carrier"}
    url = SHIPPO_TRACK_URL.format(carrier=CARRIERS[c]["shippo"],
                                  number=urllib.parse.quote(clean_number(order["tracking_number"])))
    req = urllib.request.Request(url, headers={"Authorization": f"ShippoToken {SHIPPO_API_KEY}",
                                               "User-Agent": "OpsHub/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"error": f"lookup_failed: {e}"[:200]}
    if not isinstance(data, dict):
        return {"error": "lookup_failed"}
    status = data.get("tracking_status") if isinstance(data.get("tracking_status"), dict) else {}
    code = (status.get("status") or "UNKNOWN").upper()
    events = []
    for ev in (data.get("tracking_history") or [])[::-1][:25]:
        if isinstance(ev, dict):
            events.append({"when": (ev.get("status_date") or "")[:16].replace("T", " "),
                           "status": STATUS_LABELS.get((ev.get("status") or "").upper(), ev.get("status") or ""),
                           "detail": (ev.get("status_details") or "")[:200],
                           "where": _place(ev.get("location"))})
    return {
        "tracking_status": code,
        "tracking_detail": (status.get("status_details") or "")[:300],
        "tracking_location": _place(status.get("location")),
        "tracking_eta": _day(data.get("eta")),
        "tracking_events": json.dumps(events),
        "tracking_delivered_at": ((_day(status.get("status_date")) or datetime.now().strftime("%Y-%m-%d"))
                                  if code == "DELIVERED" else None),
    }


def to_json(order):
    """What the Orders page needs to draw one order's tracking badge."""
    try:
        events = json.loads(order["tracking_events"] or "[]")
    except ValueError:
        events = []
    code = order["tracking_status"] or ""
    return {
        "number": order["tracking_number"],
        "carrier": carrier_label(order),
        "carrier_url": carrier_url(order),
        "status": code,
        "label": STATUS_LABELS.get(code, "") if code else "",
        "detail": order["tracking_detail"] or "",
        "location": order["tracking_location"] or "",
        "eta": order["tracking_eta"] or "",
        "delivered_at": order["tracking_delivered_at"] or "",
        "checked_at": order["tracking_checked_at"] or "",
        "events": events,
        "live": bool(SHIPPO_API_KEY),
    }
