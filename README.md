# Shop Inventory Tracker

A simple barcode-driven inventory system for a maintenance shop. Scan parts in and out,
track stock levels, and charge parts to jobs/projects.

## Features

- **Scan In / Scan Out** — use a USB/Bluetooth barcode scanner (acts like a keyboard) on
  a shop PC, or use a phone/tablet camera to scan barcodes right in the browser.
- **Live inventory** — quantity on hand, reorder points, low-stock alerts on the dashboard.
- **Projects** — create a job/project, assign parts to it (by scanning or manually), and
  see running parts cost per project.
- **Barcode generation & printing** — for parts that don't already have a usable barcode,
  generate one and print a label sheet (3-up, cut-out labels) straight from the browser.
- **Manual stock counts** — adjust on-hand quantity after a physical count; it's logged
  as an "adjust" transaction so you keep a full audit trail.

## Requirements

- Python 3.9+
- pip

## Setup

```bash
cd shopinv
pip install -r requirements.txt
python3 app.py
```

The app starts on **http://localhost:5050**. On first run it automatically creates
`instance/shopinv.db` (a SQLite file — no separate database server needed).

Phones reach the live OpsHub on the Pi at **https://opshub.taila1bcc5.ts.net**
(Tailscale Funnel, see `CLAUDE.md`), with no Wi-Fi or Tailscale app needed.

On the shop Wi-Fi you can also use the Pi's local address directly: find its IP on the
`192.168.9.x` network (`hostname -I` on the Pi) and visit `https://<that IP>:5050`.
When `cert.pem` and `key.pem` are in the app folder the app serves HTTPS itself (see the
bottom of `app.py`), which phone cameras need for scanning. The browser will warn once
about the self-signed certificate. Without those files the app falls back to plain
`http://`, and camera scanning then only works on `localhost`. The USB scanner workflow
on the shop PC works either way.

## Day-to-day use

1. **Add parts** once via "Add Part" (Parts page). If a part has no barcode, check
   "generate one for me" — the app creates one and you print a label for it.
2. **Scan page** is the main daily screen:
   - Toggle **STOCK IN** (receiving/restocking) or **STOCK OUT** (using parts).
   - On Stock Out, optionally pick a **Project** so the cost is tracked against that job.
   - Scan barcodes with a USB scanner (click into the text box first) or the camera tab.
3. **Projects** page shows each job's parts usage and total parts cost, and lets you
   mark jobs active / on hold / completed.
4. **Dashboard** shows low-stock items and recent activity at a glance.

## Notes on the barcode tech

- Barcodes are rendered/printed as **Code128** using the JsBarcode library (loaded from
  a CDN in the browser).
- Camera scanning uses the `html5-qrcode` library (also CDN-loaded), which decodes most
  common 1D barcodes (Code128, EAN, UPC, Code39) as well as QR codes.
- A USB/Bluetooth barcode scanner needs no special driver or app — to the browser it's
  just a very fast keyboard, so the Scan page's text box captures it directly.
- Internet access is only needed to load those two small JS libraries (once, then your
  browser caches them) — the inventory data itself is 100% local, in the SQLite file.

## Data model

- `parts` — barcode, name, category, location, unit, qty_on_hand, reorder_point, cost, supplier
- `projects` — name, description, status (active/on_hold/completed)
- `transactions` — every scan in/out, manual adjustment, and project assignment, with
  timestamps — this is your full audit trail.

## Backing up

Everything lives in `instance/shopinv.db`. Copy that one file to back up or move your
data to another machine.
Pipeline test Oct 1, 2026: idea to deploy with no Allow prompts.

## Deploying to the Pi

The live OpsHub on the Raspberry Pi installs the `main` branch of this repo by itself,
within about 2 minutes of every push (`pi-scripts/opshub-pull.sh`, run from cron). So
nothing is copied to the Pi by hand: push to a branch, get it onto `main` (the Idea
Queue's Deploy to Pi button does this), and the Pi follows. Run
`python3 tools/predeploy_check.py` before pushing; the Pi runs it again and refuses a
commit that fails it. Details in `CLAUDE.md`, "How code reaches the Pi".
