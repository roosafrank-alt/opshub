-- Shop Inventory Tracker schema

-- Master login: one account per person, used for both Shop Inventory and
-- Flight School. What they can see/do in each program is driven by the role
-- columns below rather than separate per-program credentials.
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    password_plain TEXT, -- kept alongside the hash so an admin can look up a forgotten password; set whenever a password is created/reset
    is_master_admin INTEGER NOT NULL DEFAULT 0, -- full access to everything, both programs
    shop_role TEXT, -- NULL (no Shop Inventory access), 'admin', 'tech', or 'student'
    flight_role TEXT, -- NULL (no Flight School access), 'cfi', or 'student'
    shop_roles TEXT, -- every Maintenance role the account holds, comma-separated (shop_role is the main one)
    flight_roles TEXT, -- every Flight School role the account holds, comma-separated (flight_role is the main one)
    can_bill INTEGER NOT NULL DEFAULT 0, -- Flight School: can see $ totals / outstanding balances and manage billing (master admins always can)
    active INTEGER NOT NULL DEFAULT 1,
    email TEXT, -- for email reminders (low stock, maintenance due, flight reminders)
    phone TEXT, -- for text message reminders, e.g. +15551234567
    notify_email INTEGER NOT NULL DEFAULT 0, -- opted in to receive reminder emails
    notify_sms INTEGER NOT NULL DEFAULT 0, -- opted in to receive reminder texts
    notify_low_stock INTEGER NOT NULL DEFAULT 0, -- receives low parts stock alerts
    notify_maintenance INTEGER NOT NULL DEFAULT 0, -- receives maintenance/inspection due alerts
    notify_flight_reminders INTEGER NOT NULL DEFAULT 0, -- receives upcoming scheduled flight reminders
    notify_push_session_alerts INTEGER NOT NULL DEFAULT 0, -- phone push alerts: 30 min left / time's up / flight running late (see push.py) - 1 if any of the three below is on
    notify_push_30min INTEGER NOT NULL DEFAULT 0, -- phone push: "30 minutes left" in a scheduled flying block
    notify_push_timeup INTEGER NOT NULL DEFAULT 0, -- phone push: "flight time is up" at the end of the block
    notify_push_late INTEGER NOT NULL DEFAULT 0, -- phone push: "flight is running late" (15+ min past, repeats every 15 min)
    academy_access INTEGER NOT NULL DEFAULT 0, -- Flight Academy tile (phase 3: student progress/ratings/lesson plans) - admin-assigned per account, master admins always have it
    groundschool_access INTEGER NOT NULL DEFAULT 0, -- Ground School button in the Fly with Kate! menu - admin-assigned per account, master admins and academy_access accounts always have it
    tour_seen_shop INTEGER NOT NULL DEFAULT 0, -- dismissed (or finished) the Shop Inventory guided tour at least once
    tour_seen_flight INTEGER NOT NULL DEFAULT 0, -- dismissed (or finished) the Flight School guided tour at least once
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Site-wide notification delivery settings (SMTP for email, Twilio for text
-- messages). One row per key, edited from the admin Notification Settings
-- page. Values may be blank/NULL until an admin configures them.
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Real invoices made in Wave (waveapps.com) from Billing - see wave_billing.py.
-- kind 'project' = a shop job (ref_id = projects.id), 'student' = a flight
-- student's unpaid flights (ref_id = students.id; the flights point back via
-- flights.wave_invoice_id). paid_applied_at is set once Wave said PAID and
-- the job/flights were marked paid here, so it's never applied twice.
CREATE TABLE IF NOT EXISTS wave_invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL, -- 'project' | 'student'
    ref_id INTEGER NOT NULL,
    wave_invoice_id TEXT NOT NULL,
    account INTEGER, -- which of the Admin > Wave accounts (1-3) it was made in
    business_id TEXT, -- the Wave business it was made in
    invoice_number TEXT,
    status TEXT, -- Wave's own status: SAVED, SENT, VIEWED, PARTIAL, OVERDUE, PAID...
    view_url TEXT, -- customer-facing page with Wave's Pay now button
    pdf_url TEXT,
    total REAL,
    amount_due REAL,
    amount_paid REAL,
    customer_name TEXT,
    customer_email TEXT,
    sent_at TEXT, -- when Wave emailed it from OpsHub (NULL = not emailed from here)
    last_checked_at TEXT,
    paid_applied_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_wave_invoices_ref ON wave_invoices(kind, ref_id);

-- Prevents re-sending the same reminder every time the daily check runs.
-- ref_key lets the same ref_id be notified again if its urgency level
-- changes (e.g. due_soon -> overdue).
CREATE TABLE IF NOT EXISTS notification_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL, -- 'low_stock' | 'maintenance' | 'flight_reminder'
    ref_id INTEGER NOT NULL,
    ref_key TEXT NOT NULL DEFAULT '',
    sent_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_notification_log_lookup ON notification_log(category, ref_id, ref_key);

CREATE TABLE IF NOT EXISTS parts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    barcode TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    short_name TEXT, -- brief name for printed barcode labels, when the full name is too long to fit
    part_number TEXT, -- manufacturer P/N, used in logbook entries (optional; the barcode is used when blank)
    description TEXT,
    category TEXT,
    location TEXT,
    unit TEXT DEFAULT 'ea',
    qty_on_hand REAL NOT NULL DEFAULT 0,
    reorder_point REAL NOT NULL DEFAULT 0,
    unit_cost REAL DEFAULT 0,
    sell_price REAL DEFAULT 0, -- what you charge the customer, separate from what you paid
    supplier TEXT,
    notify_low_stock INTEGER NOT NULL DEFAULT 1, -- whether this part's low-stock reminder emails/texts are sent at all - lets a noisy/low-priority part be silenced individually without turning off low-stock alerts entirely
    retired_at TEXT, -- set instead of deleting once a part has real usage history (see part_delete/part_retire) - hides it from the parts list and scan lookups but keeps every past transaction intact
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag TEXT UNIQUE NOT NULL, -- tail/serial number, e.g. N12345
    name TEXT,
    make TEXT,
    model TEXT,
    serial_number TEXT,
    year TEXT,
    owner TEXT,
    current_hours REAL, -- legacy single hours field, superseded by hobbs_hours/tach_hours
    hours_updated_at TEXT,
    hobbs_hours REAL, -- manually-updated Hobbs meter reading
    hobbs_updated_at TEXT,
    hobbs_updated_by TEXT, -- e.g. "Shop - Frank" or "Owner - Jane Smith"
    tach_hours REAL, -- manually-updated tach time reading, used for maintenance intervals
    tach_updated_at TEXT,
    tach_updated_by TEXT,
    engine_make TEXT,
    engine_model TEXT,
    engine_serial TEXT,
    prop_make TEXT,
    prop_model TEXT,
    prop_serial TEXT,
    rental_rate REAL, -- Flight School: $/hr charged to students for this aircraft (billed on Hobbs time)
    is_flight_asset INTEGER NOT NULL DEFAULT 0, -- linked to Flight School: shows up in its plane list/flight log; NOT every asset here is a flight school plane, so this isn't automatic
    is_owner_placeholder INTEGER NOT NULL DEFAULT 0, -- a student's own plane, auto-created for scheduling only (see schedule.py's "Student's own plane" toggle) - not in the Fleet or Maintenance until promoted (see asset_detail.html)
    owner_student_id INTEGER REFERENCES students(id), -- which student this placeholder belongs to; kept even after promoting to a real fleet asset, just for history
    is_simulator INTEGER NOT NULL DEFAULT 0, -- a flight simulator added from Planes > Add Simulator, not a real aircraft - no Hobbs/Tach/maintenance to track
    sim_rate REAL, -- simulator's own base $/hr rate, set on its profile; used unless a student has their own Sim Rate override
    schedule_color TEXT, -- Flight School schedule color for this plane (admin-picked on Planes > Edit; never the same as a CFI color); solo bookings show it in neon unless solo_color overrides that below
    solo_color TEXT, -- admin-picked override for this plane's solo-booking color (Planes > Edit); NULL = fall back to the auto neon version of schedule_color
    solo_allowed INTEGER NOT NULL DEFAULT 1, -- Planes > Edit checkbox; unchecked blocks booking this plane solo regardless of the student's own solo sign-off
    icao24_hex TEXT, -- Mode S / ICAO24 hex address (e.g. "A12345"), admin-entered, used for live ADS-B tracking on the Active Flight map; blank = not tracked
    notes TEXT,
    deleted_at TEXT, -- soft-delete: set when moved to Recently Deleted
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Note: no CREATE INDEX here for deleted_at - on an existing (pre-migration)
-- database this table already exists without that column, and CREATE TABLE
-- IF NOT EXISTS is then a no-op, so an index on it here would fail before
-- _migrate() gets a chance to ALTER TABLE it in. _migrate() creates this
-- index itself, unconditionally, after making sure the column exists.

-- Customer portal: aircraft owners get their own login (separate from the
-- staff `users` table - a customer has no shop_role/flight_role and can't
-- reach anything but their own linked aircraft), scoped to only the
-- aircraft customer_assets links them to. An owner can be linked to more
-- than one plane (or a plane to more than one owner, e.g. a partnership),
-- hence the join table rather than an owner_id on assets.
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    password_plain TEXT, -- kept alongside the hash so an admin can look up a forgotten password, same as users.password_plain
    phone TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS customer_assets (
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    asset_id INTEGER NOT NULL REFERENCES assets(id),
    PRIMARY KEY (customer_id, asset_id)
);
CREATE INDEX IF NOT EXISTS idx_customer_assets_asset ON customer_assets(asset_id);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE, -- auto-numbered, e.g. 26-001 (YY-sequence)
    name TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active', -- active | completed | on_hold | archived
    asset_tag TEXT, -- legacy free-text tag, superseded by asset_id
    asset_id INTEGER REFERENCES assets(id), -- the plane/equipment this project is for
    deleted_at TEXT, -- soft-delete: set when moved to Recently Deleted
    scheduled_date TEXT, -- optional planned/scheduled date (YYYY-MM-DD), shown on the maintenance calendar
    scheduled_end_date TEXT, -- optional end date (YYYY-MM-DD) for a multi-day scheduled block; NULL = single day
    scheduled_color TEXT, -- optional hex color override for calendar display, e.g. "#0d6efd"
    prework_checklist TEXT, -- things to check before starting work, one per line
    standard_items TEXT, -- standard items/steps performed on this kind of job, one per line - printed together with prework_checklist as a job sheet
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    intake_status TEXT, -- new-project intake form: NULL (older project, never asked) | pending | done | skipped
    intake_json TEXT, -- the filled-in intake form (checks, squawks, damage) as JSON
    intake_at TEXT,
    intake_by TEXT,
    customer_confirmed_at TEXT, -- customer portal: set when the aircraft owner confirms this appointment (scheduled_date)
    customer_reschedule_requested_at TEXT, -- customer portal: set when they ask to reschedule instead - clears customer_confirmed_at
    customer_reschedule_note TEXT -- what the customer said they need (shown to admin on the Maintenance dashboard until dismissed)
);
CREATE INDEX IF NOT EXISTS idx_projects_asset_tag ON projects(asset_tag);
CREATE INDEX IF NOT EXISTS idx_projects_asset_id ON projects(asset_id);
-- No CREATE INDEX here for deleted_at / scheduled_date / scheduled_end_date -
-- see the note above the assets table. _migrate() creates all such indexes
-- unconditionally.

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    part_id INTEGER NOT NULL REFERENCES parts(id),
    project_id INTEGER REFERENCES projects(id),
    type TEXT NOT NULL, -- 'in' | 'out' | 'adjust'
    qty REAL NOT NULL,
    note TEXT,
    performed_by TEXT,
    section TEXT, -- sub-area within the project, e.g. "Brakes", "Engine" - free text, scoped per project
    source TEXT, -- how this transaction was entered: 'camera_scan' | 'usb_scanner' | 'assigned' (manual)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_transactions_project_section ON transactions(project_id, section);

-- Flight School (its own tile/section, separate from the shop/maintenance
-- side above except where it reads/updates an asset's rate and hour meters).
CREATE TABLE IF NOT EXISTS cfis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    rate_per_hour REAL NOT NULL DEFAULT 0, -- what this CFI bills students for instruction time
    pay_rate_per_hour REAL, -- what the school pays this CFI, separate from rate_per_hour - visible only to admin + this CFI
    is_admin INTEGER NOT NULL DEFAULT 0, -- legacy flag, superseded by users.is_master_admin
    active INTEGER NOT NULL DEFAULT 1,
    is_station INTEGER NOT NULL DEFAULT 0, -- generic "station" login (e.g. "Shop") - grants CFI-level access but isn't a real instructor: excluded from every instructor picker/legend
    color TEXT, -- admin-picked schedule color (hex); NULL = fall back to the deterministic hash-based color
    user_id INTEGER REFERENCES users(id), -- links this CFI profile to its master login
    cred_cfi INTEGER NOT NULL DEFAULT 0, -- credential checkboxes, admin-set, hidden from students
    cred_cfii INTEGER NOT NULL DEFAULT 0,
    cred_mei INTEGER NOT NULL DEFAULT 0,
    cred_agi INTEGER NOT NULL DEFAULT 0,
    cred_bgi INTEGER NOT NULL DEFAULT 0,
    cred_igi INTEGER NOT NULL DEFAULT 0,
    cred_high_performance INTEGER NOT NULL DEFAULT 0, -- aircraft-category endorsements: which planes in the fleet this CFI can fly/instruct in
    cred_complex INTEGER NOT NULL DEFAULT 0,
    cred_tailwheel INTEGER NOT NULL DEFAULT 0,
    gender TEXT, -- admin-set, optional: 'M' | 'F' | blank/not set - only used to power the "woman only instructor" filter on the flexible flight finder
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    medical_class TEXT, -- FAA medical: first | second | third | basicmed (blank = not tracked)
    medical_expires TEXT, -- YYYY-MM-DD the medical runs out
    cfi_cert_number TEXT, -- flight instructor certificate #, printed on students' logbook entries (pilotlog.py)
    cfi_cert_expires TEXT, -- YYYY-MM-DD
    signature TEXT, -- drawn signature, PNG data URL (My CFI Profile) - copied onto logbook entries
    signature_updated_at TEXT
);

CREATE TABLE IF NOT EXISTS cfi_time_off (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cfi_id INTEGER NOT NULL REFERENCES cfis(id),
    off_date TEXT NOT NULL, -- YYYY-MM-DD
    start_time TEXT, -- HH:MM; NULL together with end_time = the whole day off
    end_time TEXT, -- HH:MM
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cfi_time_off_cfi_date ON cfi_time_off(cfi_id, off_date);

CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    rate_override REAL, -- optional custom instruction rate for this student; NULL = use the CFI's rate
    plane_rate_override REAL, -- optional custom plane rate for this student; NULL = use the plane's rate (set by an admin on the Flight School side)
    sim_rate_override REAL, -- optional custom simulator rate for this student; NULL = use the simulator's own rate (assets.sim_rate)
    solo_signoff_date TEXT, -- YYYY-MM-DD the CFI signed this student off for solo; NULL = none on file
    solo_signoff_expires TEXT, -- YYYY-MM-DD the solo sign-off runs out (defaults to sign-off + 90 days); a solo booked after this is flagged for review
    solo_currency_days INTEGER, -- days since last dual flight before this student needs a CFI checkout before soloing again; NULL = use the default (DEFAULT_SOLO_CURRENCY_DAYS in flight.py) - a newer/lower-time student might need a shorter interval than a more experienced one
    active INTEGER NOT NULL DEFAULT 1,
    is_station INTEGER NOT NULL DEFAULT 0, -- generic "station" account (e.g. "Shop") - can log in and book the schedule but isn't a real trainee: no solo-currency tracking, no personal dashboard widgets, no instructor color
    created_by_cfi_id INTEGER REFERENCES cfis(id),
    user_id INTEGER REFERENCES users(id), -- links this student profile to its master login
    balance REAL NOT NULL DEFAULT 0, -- running credit(+)/owed(-) balance, denormalized from student_ledger
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    medical_class TEXT, -- FAA medical: first | second | third | basicmed (blank = not tracked)
    medical_expires TEXT, -- YYYY-MM-DD the medical runs out
    pilot_certificate TEXT, -- student | sport | recreational | private | commercial | atp (badge tier)
    pilot_ratings TEXT, -- comma-separated add-ons: instrument,complex,high_performance,tailwheel,multi_engine,cfi,cfii,mei
    first_solo_date TEXT, -- YYYY-MM-DD first solo completed (CFI/admin sets it); NULL = pre-solo, landing currency hidden
    tsa_verified_date TEXT, -- YYYY-MM-DD a CFI/admin verified this student's TSA status; NULL = not verified yet
    pay_preference TEXT -- how this student usually pays (Cash | Check | Card, same text as End Flight's How Paid select); NULL = not set
);

-- Every billing balance change for a student - funds added, or a flight's
-- cost auto-deducted when it's logged. students.balance is kept in sync
-- with this as a running total (see flight.py) so pages don't need to SUM
-- it on every load, but this table is the source of truth / audit trail.
CREATE TABLE IF NOT EXISTS student_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id),
    entry_type TEXT NOT NULL, -- 'funds_added' | 'flight_deduction' | 'adjustment' | 'payment'
    amount REAL NOT NULL, -- positive = credit (funds added), negative = debit (owed/deducted)
    flight_id INTEGER REFERENCES flights(id),
    note TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_student_ledger_student ON student_ledger(student_id, created_at);

CREATE TABLE IF NOT EXISTS flights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cfi_id INTEGER REFERENCES cfis(id), -- who instructed; NULL when solo=1 (no instructor aboard)
    student_id INTEGER NOT NULL REFERENCES students(id),
    asset_id INTEGER NOT NULL REFERENCES assets(id),
    flight_date TEXT NOT NULL,
    hobbs_start REAL,
    hobbs_end REAL,
    tach_start REAL,
    tach_end REAL,
    oil_added_qt REAL, -- quarts of oil added on this flight, if any
    notes TEXT, -- squawks/issues or general notes from the CFI
    ground_time_hours REAL, -- instructor ground time billed separately from flight time (no plane charge)
    solo INTEGER NOT NULL DEFAULT 0, -- 1 = student flew this leg solo, no instructor aboard
    paid INTEGER NOT NULL DEFAULT 0, -- has this flight's charge been paid/billed yet
    squawk INTEGER NOT NULL DEFAULT 0, -- flagged by the CFI as a maintenance issue the shop needs to see
    squawk_acknowledged_at TEXT, -- when shop staff acknowledged the squawk (seen, not necessarily fixed yet)
    squawk_acknowledged_by TEXT, -- who acknowledged it
    squawk_repaired_at TEXT, -- when the squawk was actually fixed/addressed
    squawk_repaired_by TEXT, -- who repaired/addressed it
    squawk_assigned_to INTEGER REFERENCES users(id), -- tech this squawk was handed off to (separate from just acknowledging it)
    squawk_worker_acknowledged_at TEXT, -- when that tech confirmed they've seen the assignment
    squawk_worker_acknowledged_by TEXT, -- that tech's name, for display
    squawk_repair_confirm_requested_at TEXT, -- set when someone marks it repaired; stays pending until an Inspector/admin confirms (squawk_repaired_at)
    squawk_repair_confirm_requested_by TEXT, -- who marked it repaired, awaiting confirmation
    scheduled_flight_id INTEGER REFERENCES scheduled_flights(id), -- the booking this flight was started/logged from, if any
    started_at TEXT, -- clock time the CFI tapped "Start Flight" - set only when logged via the start/stop flow
    ended_at TEXT, -- clock time the CFI tapped "End Flight" - pairs with started_at
    instructor_clock_hours REAL, -- elapsed real time from started_at to ended_at; used to bill the instructor instead of Hobbs/Tach hours when set (covers pre/post-flight time with the student, not just time in the air)
    day_landings_fs INTEGER, -- day full-stop landings this flight
    day_landings_tg INTEGER, -- day touch-and-go landings this flight
    night_landings_fs INTEGER, -- night full-stop landings (counts for night passenger currency)
    night_landings_tg INTEGER, -- night touch-and-go landings
    paused_at TEXT, -- set while the start/stop clock is paused; NULL when running or not started
    paused_seconds INTEGER NOT NULL DEFAULT 0, -- accumulated paused duration, excluded from elapsed/clock billing
    stopped_at TEXT, -- End Flight pressed: clock stopped, waiting for Hobbs end + paid/unpaid before it's logged (ended_at)
    session_warning_sent_at TEXT, -- "30 min left" push already sent for this flight's scheduled block
    session_expired_sent_at TEXT, -- "time's up" push already sent for this flight's scheduled block
    overdue_alert_sent_at TEXT, -- most recent "flight is overdue" push send time
    overdue_acknowledged_at TEXT, -- instructor tapped Acknowledge; overdue push/red-outline snoozes for 15 min from here
    eta_at TEXT, -- updated ETA (local YYYY-MM-DD HH:MM:SS) an instructor gave for a late flight; later bookings it runs into get flagged
    eta_set_by TEXT, -- who entered that ETA
    solo_hours REAL, -- part of a dual flight the student flew alone: not billed for the instructor, logged as solo/PIC
    guest_name TEXT, -- flown by a guest (Guest / Intro placeholder student): their name
    recorded_hours REAL, -- a student's-own-plane flight's single recorded time box, instead of Hobbs/Tach
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_flights_asset ON flights(asset_id);
CREATE INDEX IF NOT EXISTS idx_flights_student ON flights(student_id);
CREATE INDEX IF NOT EXISTS idx_flights_cfi ON flights(cfi_id);

CREATE TABLE IF NOT EXISTS project_sections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, name)
);

CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    part_id INTEGER REFERENCES parts(id),
    project_id INTEGER REFERENCES projects(id),
    asset_id INTEGER REFERENCES assets(id),
    filename TEXT NOT NULL, -- stored under static/uploads/
    caption TEXT,
    is_cover INTEGER NOT NULL DEFAULT 0, -- the one photo shown first/on top for this part, project, or asset
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    part_id INTEGER REFERENCES parts(id), -- nullable: order may be for a part not yet in inventory
    description TEXT NOT NULL, -- what was ordered (free text, esp. if part_id is null)
    qty_ordered REAL NOT NULL DEFAULT 1,
    supplier TEXT,
    unit_cost REAL DEFAULT 0,
    project_id INTEGER REFERENCES projects(id), -- optional: this order is for a specific job
    status TEXT NOT NULL DEFAULT 'pending', -- pending | received | cancelled
    ordered_date TEXT NOT NULL DEFAULT (datetime('now')),
    expected_date TEXT,
    received_date TEXT,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A lighter-weight "things to order" list, separate from actual placed
-- orders above - for jotting down what's needed before anyone's committed
-- to a vendor/quantity/cost yet, with an urgency so the shop knows what to
-- prioritize. "Start Order" on one of these carries it into a real order
-- (the `orders` table) and marks it ordered here.
CREATE TABLE IF NOT EXISTS order_wishlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL,
    part_id INTEGER REFERENCES parts(id), -- optional: link to an existing inventory part
    urgency TEXT NOT NULL DEFAULT 'no_rush', -- 'rush' | 'needed_now' | 'no_rush'
    notes TEXT,
    requested_by TEXT,
    status TEXT NOT NULL DEFAULT 'open', -- 'open' | 'ordered' | 'dismissed'
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_order_wishlist_status ON order_wishlist(status);

-- Simple per-plane to-do list (squawks are for logged flight issues; this is
-- for anything else worth tracking against a specific aircraft - a part to
-- swap, a cosmetic fix, a "check on this next annual" note).
CREATE TABLE IF NOT EXISTS plane_todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES assets(id),
    description TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    created_by TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_plane_todos_asset ON plane_todos(asset_id);

-- Quick squawks: an issue reported straight against a plane, without going
-- through Flight School's "log a flight" flow (which is where a squawk
-- normally comes from - see flights.squawk). Same acknowledge/repair
-- workflow as a flight squawk; the two are merged together wherever
-- squawks are listed (see get_open_squawks() in app.py).
CREATE TABLE IF NOT EXISTS plane_squawks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES assets(id),
    notes TEXT NOT NULL,
    reported_by TEXT,
    reported_at TEXT NOT NULL DEFAULT (datetime('now')),
    acknowledged_at TEXT,
    acknowledged_by TEXT,
    repaired_at TEXT,
    repaired_by TEXT,
    assigned_to INTEGER REFERENCES users(id), -- tech this squawk was handed off to (separate from just acknowledging it)
    worker_acknowledged_at TEXT, -- when that tech confirmed they've seen the assignment
    worker_acknowledged_by TEXT, -- that tech's name, for display
    repair_confirm_requested_at TEXT, -- set when someone marks it repaired; stays pending until an Inspector/admin confirms (repaired_at)
    repair_confirm_requested_by TEXT -- who marked it repaired, awaiting confirmation
);
CREATE INDEX IF NOT EXISTS idx_plane_squawks_asset ON plane_squawks(asset_id);

-- Flight School Reports tab: anyone logged in (student or CFI) can flag a
-- plane issue, a missing checklist, a concerning issue, or a suggestion -
-- not just CFIs logging a flight. A 'plane_issue' report against a plane
-- also creates a plane_squawks row (see flight.reports_new()) so it still
-- flows into the Maintenance side's normal squawk workflow; this table is
-- what makes it visible inside Flight School too, and covers the other
-- three categories, which have no other home.
CREATE TABLE IF NOT EXISTS flight_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL, -- 'plane_issue' | 'missing_checklist' | 'concerning_issue' | 'suggestion'
    asset_id INTEGER REFERENCES assets(id),
    notes TEXT NOT NULL,
    reported_by TEXT,
    reported_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT,
    resolved_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_flight_reports_open ON flight_reports(resolved_at, category);

-- A landing that happened outside a logged flight (another school, a rental,
-- before this system was in use) but still needs to count toward a
-- student's 90-day landing currency - entered by hand on their profile.
-- Folded into the same landings_90/night_fs_90 totals as logged flights,
-- see flight._student_activity().
CREATE TABLE IF NOT EXISTS manual_landings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id),
    landing_date TEXT NOT NULL,
    day_landings INTEGER NOT NULL DEFAULT 0,
    night_landings INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_manual_landings_student ON manual_landings(student_id, landing_date);

-- Short-lived generic cache for outside-API lookups keyed by cache_key:
-- the dashboard weather widget (ZIP forecast, civil twilight, N89 METAR -
-- see weather.py, ~20min TTL) and live ADS-B aircraft positions (see
-- adsb.py, ~15sec TTL) both use this same table rather than each needing
-- their own. TTL is enforced in code, not by the table itself.
CREATE TABLE IF NOT EXISTS weather_cache (
    cache_key TEXT PRIMARY KEY,
    payload TEXT NOT NULL, -- JSON blob
    fetched_at TEXT NOT NULL
);

-- Breadcrumb trail for school planes, so searching/highlighting one on the
-- Active Flight live map can draw where it's been, not just where it is
-- right now. Only written for school planes (see adsb.py) on each genuinely
-- fresh OpenSky fetch (not every 15s poll, since most of those are served
-- from weather_cache) and pruned to a rolling window so this never grows
-- unbounded - see _prune_track_points in adsb.py.
CREATE TABLE IF NOT EXISTS adsb_track_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    icao24 TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_adsb_track_icao24 ON adsb_track_points(icao24, recorded_at);

CREATE TABLE IF NOT EXISTS maintenance_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES assets(id),
    name TEXT NOT NULL, -- e.g. "Oil Change", "Annual Inspection"
    type TEXT NOT NULL DEFAULT 'hours', -- 'hours' | 'calendar'
    category TEXT NOT NULL DEFAULT 'scheduled_maint', -- 'annual' | '100hour' | 'oil_change' | 'scheduled_maint' - drives calendar color coding
    hour_type TEXT NOT NULL DEFAULT 'tach', -- 'tach' | 'hobbs' - which meter an hours-based item tracks
    interval_hours REAL,
    interval_days INTEGER,
    last_done_hours REAL, -- asset hours reading when this was last completed
    last_done_date TEXT, -- date this was last completed (calendar items)
    remind_lead REAL, -- how far before due to flag as "due soon"; hours for hours-type items, days for calendar-type items. NULL = default (10% of interval)
    checklist TEXT, -- prework checklist, one item per line
    reference_info TEXT, -- basic info / reference notes for the job
    notes TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_maintenance_items_asset ON maintenance_items(asset_id);

CREATE TABLE IF NOT EXISTS maintenance_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES maintenance_items(id),
    completed_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_hours REAL,
    performed_by TEXT,
    note TEXT,
    project_id INTEGER REFERENCES projects(id)
);
CREATE INDEX IF NOT EXISTS idx_maintenance_log_item ON maintenance_log(item_id);

CREATE TABLE IF NOT EXISTS laborers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    code TEXT UNIQUE NOT NULL, -- scannable code, e.g. LABOR-XXXXXXXX
    rate REAL NOT NULL DEFAULT 0, -- hourly labor rate
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS labor_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    laborer_id INTEGER NOT NULL REFERENCES laborers(id),
    project_id INTEGER REFERENCES projects(id), -- NULL = General Shop (non-project) time
    section TEXT, -- which task/sub-area of the project, e.g. "Brakes"
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at TEXT, -- NULL while the timer is running
    hours REAL, -- computed when clocked out
    rate REAL, -- laborer's rate snapshotted at clock-in time
    cost REAL, -- hours * rate, computed when clocked out
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_labor_sessions_laborer ON labor_sessions(laborer_id);
CREATE INDEX IF NOT EXISTS idx_labor_sessions_project ON labor_sessions(project_id);
CREATE INDEX IF NOT EXISTS idx_labor_sessions_open ON labor_sessions(laborer_id, ended_at);
-- QA fix qa-labor-double-clock-in: a worker can never have two open timers -
-- enforced here (not just in app.py's check-then-insert) so two clock-in
-- scans arriving at the same instant can't both slip through.
CREATE UNIQUE INDEX IF NOT EXISTS idx_labor_sessions_one_open ON labor_sessions(laborer_id) WHERE ended_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_transactions_part ON transactions(part_id);
CREATE INDEX IF NOT EXISTS idx_transactions_project ON transactions(project_id);
CREATE INDEX IF NOT EXISTS idx_parts_barcode ON parts(barcode);
CREATE INDEX IF NOT EXISTS idx_photos_part ON photos(part_id);
CREATE INDEX IF NOT EXISTS idx_photos_project ON photos(project_id);
-- No CREATE INDEX here for photos.asset_id - on an existing (pre-migration)
-- database this table already exists without that column, and CREATE TABLE
-- IF NOT EXISTS is then a no-op, so an index on it here would fail before
-- _migrate() gets a chance to ALTER TABLE it in. _migrate() creates this
-- index itself, unconditionally, after making sure the column exists.
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

-- Cylinder compression checks, tracked per plane like oil - up to 6
-- cylinders (blank ones just aren't used on a 4-cylinder engine).
CREATE TABLE IF NOT EXISTS compression_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES assets(id),
    checked_date TEXT NOT NULL,
    hours REAL, -- tach/hobbs hours at time of check, for the x-axis of the trend chart
    master_orifice REAL, -- master orifice / reference reading, e.g. 80
    cyl1 REAL, cyl2 REAL, cyl3 REAL, cyl4 REAL, cyl5 REAL, cyl6 REAL,
    performed_by TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_compression_checks_asset ON compression_checks(asset_id);

-- Scheduled (future) flights - separate from `flights`, which is the log of
-- flights already flown. A CFI books a plane/student/time here; it shows on
-- the Flight School schedule calendar until it's flown (at which point it's
-- logged separately via Log a Flight) or cancelled.
CREATE TABLE IF NOT EXISTS scheduled_flights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES assets(id),
    cfi_id INTEGER REFERENCES cfis(id), -- NULL = solo, no instructor
    student_id INTEGER NOT NULL REFERENCES students(id),
    scheduled_date TEXT NOT NULL, -- YYYY-MM-DD
    scheduled_time TEXT, -- HH:MM, optional
    duration_hours REAL, -- planned block length, for display only
    notes TEXT,
    private_notes TEXT, -- admin/CFI eyes only - never rendered anywhere a student (incl. self-service booking) can see it
    status TEXT NOT NULL DEFAULT 'scheduled', -- 'scheduled' | 'cancelled'
    created_by TEXT,
    solo INTEGER NOT NULL DEFAULT 0, -- explicit "meant to fly with no instructor" flag - decoupled from cfi_id
                                      -- being NULL, since a dual flight can also have no instructor picked yet
    needs_review INTEGER NOT NULL DEFAULT 0, -- flagged for admin/CFI attention: either a solo flight scheduled
                                              -- despite the student exceeding their solo currency interval, or a
                                              -- dual flight booked with no instructor assigned yet. Clears itself
                                              -- once a CFI is added (see flight.py schedule_edit).
    review_reason TEXT, -- human-readable reason shown on the calendar/schedule when needs_review is set
    part_solo INTEGER NOT NULL DEFAULT 0, -- dual booking where the student also flies part of it solo
    guest_name TEXT, -- booked for the Guest / Intro placeholder student: the person's name (no profile)
    guest_phone TEXT,
    guest_email TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_scheduled_flights_date ON scheduled_flights(scheduled_date);
CREATE INDEX IF NOT EXISTS idx_scheduled_flights_asset ON scheduled_flights(asset_id);
CREATE INDEX IF NOT EXISTS idx_scheduled_flights_cfi ON scheduled_flights(cfi_id);

-- Generic per-field audit trail for click-to-drill-down values (a student's
-- plane-rate/instructor-rate override, etc.) - who changed it and when. The
-- Account/balance side already has this via student_ledger.
CREATE TABLE IF NOT EXISTS field_change_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL, -- 'student' | 'cfi' | 'plane'
    entity_id INTEGER NOT NULL,
    field_name TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    changed_by TEXT,
    changed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_field_change_log_entity ON field_change_log(entity_type, entity_id, field_name);

-- Web Push (RFC 8030) subscriptions - one row per browser/device that
-- opted in to phone alerts for flying sessions. See push.py.
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    endpoint TEXT NOT NULL UNIQUE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_push_subscriptions_user ON push_subscriptions(user_id);

-- The push message itself carries no data (no payload encryption needed -
-- see push.py); a push just wakes the service worker, which fetches its
-- pending alerts from here (and this table is cleared as they're read).
CREATE TABLE IF NOT EXISTS push_pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    body TEXT,
    tag TEXT,
    url TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_push_pending_user ON push_pending(user_id);

-- Flight School Alerts tab (see flight._sync_flight_alerts): one row per
-- time a booking was flagged for review. Open until the booking is no
-- longer flagged, then kept as the resolved archive.
CREATE TABLE IF NOT EXISTS flight_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scheduled_flight_id INTEGER NOT NULL,
    student_id INTEGER,
    reason TEXT,
    created_at TEXT NOT NULL,
    acknowledged_at TEXT,
    acknowledged_by TEXT,
    resolved_at TEXT,
    resolved_by TEXT,
    resolution TEXT
);
CREATE INDEX IF NOT EXISTS idx_flight_alerts_open ON flight_alerts(resolved_at, scheduled_flight_id);

-- Unaccounted Hobbs time (flight._hobbs_gaps): the Hobbs went up between
-- one logged flight's end and the plane's next flight's start. Shown to
-- master admins on Alerts until one of them marks it reviewed here.
CREATE TABLE IF NOT EXISTS hobbs_gap_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER,
    from_flight_id INTEGER NOT NULL,
    to_flight_id INTEGER NOT NULL,
    gap_hours REAL,
    note TEXT,
    reviewed_by TEXT,
    reviewed_at TEXT NOT NULL,
    UNIQUE(from_flight_id, to_flight_id)
);

-- Flight Academy leaderboard: flying a student logs for themselves (distance,
-- extra landings, cross-countries, night/instrument time) - see academy.py.
CREATE TABLE IF NOT EXISTS academy_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id),
    entry_date TEXT NOT NULL, -- YYYY-MM-DD flown
    kind TEXT NOT NULL, -- distance_nm | landings | night_landings | xc_flights | night_hours | instrument_hours
    value REAL NOT NULL,
    note TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_academy_entries_student ON academy_entries(student_id, entry_date);

-- Maintenance logbook entries (see logbook.py): drafted on a project's
-- Logbook Starter from its sub areas and parts, edited, then printed as a
-- sticker for the paper Airframe / Engine / Propeller logbook.
CREATE TABLE IF NOT EXISTS logbook_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER REFERENCES assets(id),
    project_id INTEGER REFERENCES projects(id),
    section TEXT, -- the project sub area this entry covers; NULL = the combined entry for the whole project
    log_type TEXT NOT NULL DEFAULT 'airframe', -- airframe | engine | propeller
    project_type TEXT, -- annual | 100hour | oil_change | repair (which template it started from)
    entry_date TEXT NOT NULL, -- YYYY-MM-DD
    tach_hours REAL,
    hobbs_hours REAL,
    body TEXT NOT NULL,
    signed_by TEXT,
    cert_number TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_logbook_entries_asset ON logbook_entries(asset_id, log_type, entry_date);
CREATE INDEX IF NOT EXISTS idx_logbook_entries_project ON logbook_entries(project_id);

-- The combined-entry wording per project type + logbook (Manage > Logbook >
-- Templates). No row = the built-in wording in logbook.py.
CREATE TABLE IF NOT EXISTS logbook_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_type TEXT NOT NULL,
    log_type TEXT NOT NULL,
    body TEXT NOT NULL,
    updated_by TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_type, log_type)
);

-- Flight Academy pilot logbook (pilotlog.py): one entry per logged flight,
-- pre-filled from the flight, pending until the student completes and
-- approves it.
CREATE TABLE IF NOT EXISTS pilot_logbook (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flight_id INTEGER UNIQUE REFERENCES flights(id),
    student_id INTEGER NOT NULL REFERENCES students(id),
    cfi_id INTEGER REFERENCES cfis(id),
    asset_id INTEGER REFERENCES assets(id),
    entry_date TEXT NOT NULL,
    tail TEXT,
    make_model TEXT,
    route_from TEXT,
    route_to TEXT,
    total REAL NOT NULL DEFAULT 0, -- Tach time (else Hobbs)
    dual REAL NOT NULL DEFAULT 0, -- dual received (= the instructor's dual given)
    solo REAL NOT NULL DEFAULT 0,
    pic REAL,
    xc REAL,
    night REAL,
    actual_inst REAL,
    sim_inst REAL,
    approaches INTEGER,
    day_ldg INTEGER,
    night_ldg INTEGER,
    night_fs INTEGER, -- night full-stop landings
    remarks TEXT,
    instructor_name TEXT,
    instructor_cert TEXT,
    instructor_cert_exp TEXT,
    instructor_signature TEXT, -- the CFI's drawn signature at the time (PNG data URL)
    status TEXT NOT NULL DEFAULT 'pending', -- pending | complete (student approved)
    approved_at TEXT,
    approved_by TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pilot_logbook_student ON pilot_logbook(student_id, entry_date);

-- Progress requirements an instructor checks off (knowledge test, long
-- cross-country, endorsements...) - pilotlog.TRACKS.
CREATE TABLE IF NOT EXISTS academy_milestones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id),
    track TEXT NOT NULL,
    item TEXT NOT NULL,
    done_date TEXT,
    signed_by TEXT,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(student_id, track, item)
);

-- Airport locations for the Flight Academy map (pilotlog.airport_map),
-- looked up once per identifier from aviationweather.gov and kept here.
-- found = 0 means the lookup came back empty (retried after a week).
CREATE TABLE IF NOT EXISTS airport_coords (
    code TEXT PRIMARY KEY,
    lat REAL,
    lon REAL,
    name TEXT,
    found INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL
);
