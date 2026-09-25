import sqlite3
import os
import random
import string
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "shopinv.db")
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())
    conn.commit()
    _migrate(conn)
    conn.close()


def _migrate(conn):
    """Lightweight in-place migrations for databases created before a schema change."""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(projects)").fetchall()]
    if "code" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN code TEXT")
        conn.commit()

    # Backfill any projects that don't have a code yet (older rows, or the
    # column was just added above), oldest first so numbering stays sane.
    missing = conn.execute(
        "SELECT id, created_at FROM projects WHERE code IS NULL OR code = '' ORDER BY id"
    ).fetchall()
    for row in missing:
        yy = (row["created_at"] or now_iso())[2:4]
        code = gen_project_code(conn, yy)
        conn.execute("UPDATE projects SET code = ? WHERE id = ?", (code, row["id"]))
        conn.commit()

    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_code ON projects(code)")
    conn.commit()

    if "asset_tag" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN asset_tag TEXT")
        conn.commit()
        conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_asset_tag ON projects(asset_tag)")
        conn.commit()

    part_cols = [r["name"] for r in conn.execute("PRAGMA table_info(parts)").fetchall()]
    if "sell_price" not in part_cols:
        conn.execute("ALTER TABLE parts ADD COLUMN sell_price REAL DEFAULT 0")
        conn.commit()
    if "notify_low_stock" not in part_cols:
        conn.execute("ALTER TABLE parts ADD COLUMN notify_low_stock INTEGER NOT NULL DEFAULT 1")
        conn.commit()

    tx_cols = [r["name"] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()]
    if "section" not in tx_cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN section TEXT")
        conn.commit()
        conn.execute("CREATE INDEX IF NOT EXISTS idx_transactions_project_section ON transactions(project_id, section)")
        conn.commit()

    # Assets (planes/equipment) as first-class profiles, replacing the old
    # free-text projects.asset_tag field. A project can now point at a real
    # asset record instead of just carrying a matching string.
    conn.execute("""CREATE TABLE IF NOT EXISTS assets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tag TEXT UNIQUE NOT NULL,
        name TEXT,
        make TEXT,
        model TEXT,
        serial_number TEXT,
        year TEXT,
        owner TEXT,
        current_hours REAL,
        hours_updated_at TEXT,
        notes TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.commit()

    if "asset_id" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN asset_id INTEGER REFERENCES assets(id)")
        conn.commit()
        conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_asset_id ON projects(asset_id)")
        conn.commit()

    # Backfill: turn every distinct old asset_tag string into a real asset
    # row (once), and point those projects at it.
    if "asset_tag" in cols:
        old_tags = conn.execute("""
            SELECT DISTINCT asset_tag FROM projects
            WHERE asset_tag IS NOT NULL AND TRIM(asset_tag) != '' AND asset_id IS NULL
        """).fetchall()
        for row in old_tags:
            tag = row["asset_tag"].strip()
            existing = conn.execute("SELECT id FROM assets WHERE tag = ?", (tag,)).fetchone()
            if existing:
                asset_id = existing["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO assets (tag, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (tag, tag, now_iso(), now_iso()))
                asset_id = cur.lastrowid
            conn.execute("UPDATE projects SET asset_id = ? WHERE asset_tag = ? AND asset_id IS NULL",
                         (asset_id, tag))
            conn.commit()

    # Time-/hours-sensitive maintenance tracking, per asset.
    conn.execute("""CREATE TABLE IF NOT EXISTS maintenance_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset_id INTEGER NOT NULL REFERENCES assets(id),
        name TEXT NOT NULL,
        type TEXT NOT NULL DEFAULT 'hours',
        category TEXT NOT NULL DEFAULT 'scheduled_maint',
        interval_hours REAL,
        interval_days INTEGER,
        last_done_hours REAL,
        last_done_date TEXT,
        checklist TEXT,
        reference_info TEXT,
        notes TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_maintenance_items_asset ON maintenance_items(asset_id)")
    conn.commit()

    conn.execute("""CREATE TABLE IF NOT EXISTS maintenance_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER NOT NULL REFERENCES maintenance_items(id),
        completed_at TEXT NOT NULL DEFAULT (datetime('now')),
        completed_hours REAL,
        performed_by TEXT,
        note TEXT,
        project_id INTEGER REFERENCES projects(id)
    )""")
    conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_maintenance_log_item ON maintenance_log(item_id)")
    conn.commit()

    # Hobbs + tach time as two separate meters, replacing the single
    # current_hours field. Existing current_hours readings get copied into
    # both meters as a starting point.
    asset_cols = [r["name"] for r in conn.execute("PRAGMA table_info(assets)").fetchall()]
    for col, ddl in (
        ("hobbs_hours", "ALTER TABLE assets ADD COLUMN hobbs_hours REAL"),
        ("hobbs_updated_at", "ALTER TABLE assets ADD COLUMN hobbs_updated_at TEXT"),
        ("tach_hours", "ALTER TABLE assets ADD COLUMN tach_hours REAL"),
        ("tach_updated_at", "ALTER TABLE assets ADD COLUMN tach_updated_at TEXT"),
        ("deleted_at", "ALTER TABLE assets ADD COLUMN deleted_at TEXT"),
    ):
        if col not in asset_cols:
            conn.execute(ddl)
            conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_assets_deleted_at ON assets(deleted_at)")
    conn.commit()

    if "current_hours" in asset_cols:
        conn.execute("""UPDATE assets SET hobbs_hours = current_hours, hobbs_updated_at = hours_updated_at
                         WHERE current_hours IS NOT NULL AND hobbs_hours IS NULL""")
        conn.execute("""UPDATE assets SET tach_hours = current_hours, tach_updated_at = hours_updated_at
                         WHERE current_hours IS NOT NULL AND tach_hours IS NULL""")
        conn.commit()

    # Maintenance items: which meter (tach or hobbs) an hours-based item tracks.
    mi_cols = [r["name"] for r in conn.execute("PRAGMA table_info(maintenance_items)").fetchall()]
    if "hour_type" not in mi_cols:
        conn.execute("ALTER TABLE maintenance_items ADD COLUMN hour_type TEXT NOT NULL DEFAULT 'tach'")
        conn.commit()

    # Soft-delete ("Recently Deleted") support for projects.
    if "deleted_at" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN deleted_at TEXT")
        conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_deleted_at ON projects(deleted_at)")
    conn.commit()

    # Maintenance calendar: optional scheduled/planned date for a project.
    if "scheduled_date" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN scheduled_date TEXT")
        conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_scheduled_date ON projects(scheduled_date)")
    conn.commit()

    # Labor tracking: laborers (with a scannable code + hourly rate) and
    # timed labor_sessions (clock-in/clock-out via scan). Both are brand new
    # tables, so creating them plus their indexes together here is safe even
    # on an existing database (no retroactive-index-on-missing-column issue,
    # since the table doesn't exist yet either way).
    conn.execute("""CREATE TABLE IF NOT EXISTS laborers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        code TEXT UNIQUE NOT NULL,
        rate REAL NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.commit()

    conn.execute("""CREATE TABLE IF NOT EXISTS labor_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        laborer_id INTEGER NOT NULL REFERENCES laborers(id),
        project_id INTEGER NOT NULL REFERENCES projects(id),
        section TEXT,
        started_at TEXT NOT NULL DEFAULT (datetime('now')),
        ended_at TEXT,
        hours REAL,
        rate REAL,
        cost REAL,
        note TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_labor_sessions_laborer ON labor_sessions(laborer_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_labor_sessions_project ON labor_sessions(project_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_labor_sessions_open ON labor_sessions(laborer_id, ended_at)")
    conn.commit()

    # Maintenance items: configurable "remind me when" lead time, replacing
    # the hardcoded 10%-of-interval due-soon threshold. Hours for hours-type
    # items, days for calendar-type items. NULL = fall back to the default.
    mi_cols = [r["name"] for r in conn.execute("PRAGMA table_info(maintenance_items)").fetchall()]
    if "remind_lead" not in mi_cols:
        conn.execute("ALTER TABLE maintenance_items ADD COLUMN remind_lead REAL")
        conn.commit()

    # Asset profiles: engine + propeller make/model/serial.
    asset_cols = [r["name"] for r in conn.execute("PRAGMA table_info(assets)").fetchall()]
    for col, ddl in (
        ("engine_make", "ALTER TABLE assets ADD COLUMN engine_make TEXT"),
        ("engine_model", "ALTER TABLE assets ADD COLUMN engine_model TEXT"),
        ("engine_serial", "ALTER TABLE assets ADD COLUMN engine_serial TEXT"),
        ("prop_make", "ALTER TABLE assets ADD COLUMN prop_make TEXT"),
        ("prop_model", "ALTER TABLE assets ADD COLUMN prop_model TEXT"),
        ("prop_serial", "ALTER TABLE assets ADD COLUMN prop_serial TEXT"),
    ):
        if col not in asset_cols:
            conn.execute(ddl)
            conn.commit()

    # Calendar: multi-day schedule blocks (end date) and a per-project color override.
    if "scheduled_end_date" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN scheduled_end_date TEXT")
        conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_scheduled_end_date ON projects(scheduled_end_date)")
    conn.commit()

    if "scheduled_color" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN scheduled_color TEXT")
        conn.commit()

    # Flight School: aircraft rental rate, billed against Hobbs time.
    asset_cols = [r["name"] for r in conn.execute("PRAGMA table_info(assets)").fetchall()]
    if "rental_rate" not in asset_cols:
        conn.execute("ALTER TABLE assets ADD COLUMN rental_rate REAL")
        conn.commit()

    # Not every asset in the maintenance tile is a Flight School plane, so
    # showing up there is an explicit per-asset link, not automatic.
    if "is_flight_asset" not in asset_cols:
        conn.execute("ALTER TABLE assets ADD COLUMN is_flight_asset INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    # Flight School tables - all brand new, safe to create with their indexes
    # together even on an existing database.
    conn.execute("""CREATE TABLE IF NOT EXISTS cfis (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        rate_per_hour REAL NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.commit()
    cfi_cols = [r["name"] for r in conn.execute("PRAGMA table_info(cfis)").fetchall()]
    if "is_admin" not in cfi_cols:
        conn.execute("ALTER TABLE cfis ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    conn.execute("""CREATE TABLE IF NOT EXISTS students (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        rate_override REAL,
        active INTEGER NOT NULL DEFAULT 1,
        created_by_cfi_id INTEGER REFERENCES cfis(id),
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.commit()
    student_cols_early = [r["name"] for r in conn.execute("PRAGMA table_info(students)").fetchall()]
    if "plane_rate_override" not in student_cols_early:
        conn.execute("ALTER TABLE students ADD COLUMN plane_rate_override REAL")
        conn.commit()

    conn.execute("""CREATE TABLE IF NOT EXISTS flights (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cfi_id INTEGER REFERENCES cfis(id),
        student_id INTEGER NOT NULL REFERENCES students(id),
        asset_id INTEGER NOT NULL REFERENCES assets(id),
        flight_date TEXT NOT NULL,
        hobbs_start REAL,
        hobbs_end REAL,
        tach_start REAL,
        tach_end REAL,
        oil_added_qt REAL,
        notes TEXT,
        solo INTEGER NOT NULL DEFAULT 0,
        paid INTEGER NOT NULL DEFAULT 0,
        squawk INTEGER NOT NULL DEFAULT 0,
        squawk_acknowledged_at TEXT,
        squawk_acknowledged_by TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_flights_asset ON flights(asset_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_flights_student ON flights(student_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_flights_cfi ON flights(cfi_id)")
    conn.commit()

    # Older databases already have a flights table with cfi_id NOT NULL and
    # none of the columns below. SQLite can't relax a NOT NULL constraint
    # with ALTER TABLE, so when that's still the case, rebuild the table
    # (cfi_id becomes nullable - a solo flight has no instructor - and the
    # new columns come along in the same rebuild); otherwise just ADD COLUMN
    # anything still missing.
    flight_cols_info = conn.execute("PRAGMA table_info(flights)").fetchall()
    flight_cols = [r["name"] for r in flight_cols_info]
    cfi_id_notnull = next((r["notnull"] for r in flight_cols_info if r["name"] == "cfi_id"), 0)
    if cfi_id_notnull:
        conn.execute("""CREATE TABLE flights_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cfi_id INTEGER REFERENCES cfis(id),
            student_id INTEGER NOT NULL REFERENCES students(id),
            asset_id INTEGER NOT NULL REFERENCES assets(id),
            flight_date TEXT NOT NULL,
            hobbs_start REAL,
            hobbs_end REAL,
            tach_start REAL,
            tach_end REAL,
            oil_added_qt REAL,
            notes TEXT,
            solo INTEGER NOT NULL DEFAULT 0,
            paid INTEGER NOT NULL DEFAULT 0,
            squawk INTEGER NOT NULL DEFAULT 0,
            squawk_acknowledged_at TEXT,
            squawk_acknowledged_by TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.execute("""INSERT INTO flights_new (id, cfi_id, student_id, asset_id, flight_date, hobbs_start,
                         hobbs_end, tach_start, tach_end, oil_added_qt, notes, created_at)
                         SELECT id, cfi_id, student_id, asset_id, flight_date, hobbs_start, hobbs_end, tach_start,
                                tach_end, oil_added_qt, notes, created_at FROM flights""")
        conn.execute("DROP TABLE flights")
        conn.execute("ALTER TABLE flights_new RENAME TO flights")
        conn.commit()
        conn.execute("CREATE INDEX IF NOT EXISTS idx_flights_asset ON flights(asset_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_flights_student ON flights(student_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_flights_cfi ON flights(cfi_id)")
        conn.commit()
        flight_cols = [r["name"] for r in conn.execute("PRAGMA table_info(flights)").fetchall()]

    for col, ddl in (
        ("solo", "ALTER TABLE flights ADD COLUMN solo INTEGER NOT NULL DEFAULT 0"),
        ("paid", "ALTER TABLE flights ADD COLUMN paid INTEGER NOT NULL DEFAULT 0"),
        ("squawk", "ALTER TABLE flights ADD COLUMN squawk INTEGER NOT NULL DEFAULT 0"),
        ("squawk_acknowledged_at", "ALTER TABLE flights ADD COLUMN squawk_acknowledged_at TEXT"),
        ("squawk_acknowledged_by", "ALTER TABLE flights ADD COLUMN squawk_acknowledged_by TEXT"),
        ("ground_time_hours", "ALTER TABLE flights ADD COLUMN ground_time_hours REAL"),
    ):
        if col not in flight_cols:
            conn.execute(ddl)
            conn.commit()

    # Transactions: how each one was entered (camera scan / USB scanner / manually assigned).
    if "source" not in tx_cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN source TEXT")
        conn.commit()

    # Project sub-areas ("sections") that can be created ahead of time, without
    # first scanning a part into them. Brand-new table, safe to create with its
    # index together even on an existing database.
    conn.execute("""CREATE TABLE IF NOT EXISTS project_sections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL REFERENCES projects(id),
        name TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(project_id, name)
    )""")
    conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_project_sections_project ON project_sections(project_id)")
    conn.commit()
    proj_sect_cols = [r["name"] for r in conn.execute("PRAGMA table_info(project_sections)").fetchall()]
    if "completed_at" not in proj_sect_cols:
        conn.execute("ALTER TABLE project_sections ADD COLUMN completed_at TEXT")
        conn.commit()

    # Preset Sub Areas for a Quick Type (New/Edit Project's Annual, 100hr,
    # Oil Change, Maintenance buttons) - admin-managed from Manage > Task
    # Templates (see app.task_templates). Picking that Quick Type on a new
    # project auto-creates these as the project's Sub Areas.
    conn.execute("""CREATE TABLE IF NOT EXISTS task_template_areas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        quick_type TEXT NOT NULL,
        name TEXT NOT NULL,
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(quick_type, name)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_template_areas_type ON task_template_areas(quick_type)")
    conn.commit()

    # Master login: one users table shared by Shop Inventory and Flight
    # School. Brand new table, safe to create directly even on an existing
    # database.
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        password_plain TEXT,
        is_master_admin INTEGER NOT NULL DEFAULT 0,
        shop_role TEXT,
        flight_role TEXT,
        can_bill INTEGER NOT NULL DEFAULT 0,
        academy_access INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.commit()
    user_cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    for col, ddl in (
        ("password_plain", "ALTER TABLE users ADD COLUMN password_plain TEXT"),
        ("can_bill", "ALTER TABLE users ADD COLUMN can_bill INTEGER NOT NULL DEFAULT 0"),
        ("email", "ALTER TABLE users ADD COLUMN email TEXT"),
        ("phone", "ALTER TABLE users ADD COLUMN phone TEXT"),
        ("notify_email", "ALTER TABLE users ADD COLUMN notify_email INTEGER NOT NULL DEFAULT 0"),
        ("notify_sms", "ALTER TABLE users ADD COLUMN notify_sms INTEGER NOT NULL DEFAULT 0"),
        ("notify_low_stock", "ALTER TABLE users ADD COLUMN notify_low_stock INTEGER NOT NULL DEFAULT 0"),
        ("notify_maintenance", "ALTER TABLE users ADD COLUMN notify_maintenance INTEGER NOT NULL DEFAULT 0"),
        ("notify_flight_reminders", "ALTER TABLE users ADD COLUMN notify_flight_reminders INTEGER NOT NULL DEFAULT 0"),
        ("academy_access", "ALTER TABLE users ADD COLUMN academy_access INTEGER NOT NULL DEFAULT 0"),
    ):
        if col not in user_cols:
            conn.execute(ddl)
            conn.commit()

    # New in this version - app_settings (SMTP/Twilio config) and
    # notification_log (dedup for the daily reminder check) tables. Both are
    # brand new, so CREATE TABLE IF NOT EXISTS from schema.sql handles a
    # fresh DB; this covers an existing DB whose schema.sql run was a no-op
    # because the file already existed before those tables were added.
    conn.execute("""CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS notification_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        ref_id INTEGER NOT NULL,
        ref_key TEXT NOT NULL DEFAULT '',
        sent_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notification_log_lookup ON notification_log(category, ref_id, ref_key)")
    conn.commit()

    # Link existing CFI/student profiles to a master login account.
    cfi_cols2 = [r["name"] for r in conn.execute("PRAGMA table_info(cfis)").fetchall()]
    if "user_id" not in cfi_cols2:
        conn.execute("ALTER TABLE cfis ADD COLUMN user_id INTEGER REFERENCES users(id)")
        conn.commit()
    student_cols = [r["name"] for r in conn.execute("PRAGMA table_info(students)").fetchall()]
    if "user_id" not in student_cols:
        conn.execute("ALTER TABLE students ADD COLUMN user_id INTEGER REFERENCES users(id)")
        conn.commit()

    # Calendar color coding: which category a maintenance item is, so the
    # calendar can color-code annual/100-hour/oil-change/other-scheduled
    # items distinctly instead of one flat "maintenance" color.
    mi_cols2 = [r["name"] for r in conn.execute("PRAGMA table_info(maintenance_items)").fetchall()]
    if "category" not in mi_cols2:
        conn.execute("ALTER TABLE maintenance_items ADD COLUMN category TEXT NOT NULL DEFAULT 'scheduled_maint'")
        conn.commit()
        # One-time best-guess backfill from each existing item's name, since
        # this column didn't exist before - a user-edited category later
        # will never be touched again (this whole block only runs once,
        # guarded by the column-not-present check above).
        conn.execute("UPDATE maintenance_items SET category='annual' WHERE lower(name) LIKE '%annual%'")
        conn.execute("UPDATE maintenance_items SET category='100hour' WHERE lower(name) LIKE '%100%'")
        conn.execute("UPDATE maintenance_items SET category='oil_change' WHERE lower(name) LIKE '%oil%'")
        conn.commit()

    # Barcode labels: an optional brief name, for parts whose full name is
    # too long to read comfortably on a small printed label.
    part_cols2 = [r["name"] for r in conn.execute("PRAGMA table_info(parts)").fetchall()]
    if "short_name" not in part_cols2:
        conn.execute("ALTER TABLE parts ADD COLUMN short_name TEXT")
        conn.commit()
    # Logbook entries: the manufacturer part number (optional; the barcode
    # stands in when it's blank - see logbook.part_number()).
    if "part_number" not in part_cols2:
        conn.execute("ALTER TABLE parts ADD COLUMN part_number TEXT")
        conn.commit()

    # Cover photo: which one photo (of possibly several) shows first/on top
    # for a part or project.
    photo_cols = [r["name"] for r in conn.execute("PRAGMA table_info(photos)").fetchall()]
    if "is_cover" not in photo_cols:
        conn.execute("ALTER TABLE photos ADD COLUMN is_cover INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    # Photos for assets too (not just parts/projects).
    photo_cols2 = [r["name"] for r in conn.execute("PRAGMA table_info(photos)").fetchall()]
    if "asset_id" not in photo_cols2:
        conn.execute("ALTER TABLE photos ADD COLUMN asset_id INTEGER REFERENCES assets(id)")
        conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_asset ON photos(asset_id)")
    conn.commit()

    # Squawks: a separate "repaired" step from "acknowledged" - acknowledging
    # just means someone on the shop side has seen it, repairing means the
    # issue is actually fixed.
    flight_cols = [r["name"] for r in conn.execute("PRAGMA table_info(flights)").fetchall()]
    if "squawk_repaired_at" not in flight_cols:
        conn.execute("ALTER TABLE flights ADD COLUMN squawk_repaired_at TEXT")
        conn.execute("ALTER TABLE flights ADD COLUMN squawk_repaired_by TEXT")
        conn.commit()

    # Project job sheets: a prework checklist (things to check before
    # starting) and a standard-items-performed list, both printable together.
    proj_cols2 = [r["name"] for r in conn.execute("PRAGMA table_info(projects)").fetchall()]
    if "prework_checklist" not in proj_cols2:
        conn.execute("ALTER TABLE projects ADD COLUMN prework_checklist TEXT")
        conn.execute("ALTER TABLE projects ADD COLUMN standard_items TEXT")
        conn.commit()

    # Start/stop flight clock: linking a logged flight back to the booking
    # it came from, plus the clock-in/clock-out timestamps used to bill the
    # instructor for actual time spent (not just Hobbs/Tach flight time).
    flight_cols2 = [r["name"] for r in conn.execute("PRAGMA table_info(flights)").fetchall()]
    if "started_at" not in flight_cols2:
        conn.execute("ALTER TABLE flights ADD COLUMN scheduled_flight_id INTEGER REFERENCES scheduled_flights(id)")
        conn.execute("ALTER TABLE flights ADD COLUMN started_at TEXT")
        conn.execute("ALTER TABLE flights ADD COLUMN ended_at TEXT")
        conn.execute("ALTER TABLE flights ADD COLUMN instructor_clock_hours REAL")
        conn.commit()

    # Per-student solo currency interval: how many days since their last
    # dual (CFI) flight before they need a checkout - NULL uses the
    # program-wide default (DEFAULT_SOLO_CURRENCY_DAYS in flight.py), lets
    # a newer/lower-time student be held to a shorter interval than a more
    # experienced one.
    student_cols3 = [r["name"] for r in conn.execute("PRAGMA table_info(students)").fetchall()]
    if "solo_currency_days" not in student_cols3:
        conn.execute("ALTER TABLE students ADD COLUMN solo_currency_days INTEGER")
        conn.commit()

    # Student pilot solo sign-off (CFI endorsement) date and its 90-day
    # expiry. flight.py fills the expiry in as sign-off + 90 days when it's
    # left blank, and a solo booked past the expiry is flagged for review
    # the same way an over-currency solo is (_schedule_review_flag).
    for col in ("solo_signoff_date", "solo_signoff_expires"):
        if col not in student_cols3:
            conn.execute(f"ALTER TABLE students ADD COLUMN {col} TEXT")
            conn.commit()

    # FAA medical for students and CFIs: class ('first' | 'second' |
    # 'third' | 'basicmed') and the date it runs out. flight.py blocks
    # booking/starting a flight with a CFI whose medical has expired, and
    # flags a solo whose student's medical has expired (dual is still OK).
    # Blank = not tracked, nothing is checked.
    # Student pilot certificate (student | sport | recreational | private |
    # commercial | atp) and add-on ratings (comma-separated codes, e.g.
    # "instrument,tailwheel,cfi") - drive the badge on the Students list.
    student_cols4 = [r["name"] for r in conn.execute("PRAGMA table_info(students)").fetchall()]
    for col in ("pilot_certificate", "pilot_ratings"):
        if col not in student_cols4:
            conn.execute(f"ALTER TABLE students ADD COLUMN {col} TEXT")
            conn.commit()

    for table in ("students", "cfis"):
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        for col in ("medical_class", "medical_expires"):
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
                conn.commit()

    # Flag a scheduled flight for admin/CFI review instead of refusing to
    # save it outright: a solo flight booked despite the student being over
    # their solo currency interval, or a dual flight booked with no
    # instructor picked yet ("assign later"). `solo` is now tracked
    # explicitly so the two "no cfi_id" cases (genuine solo vs. dual/TBD)
    # can be told apart.
    sched_cols = [r["name"] for r in conn.execute("PRAGMA table_info(scheduled_flights)").fetchall()]
    if "needs_review" not in sched_cols:
        conn.execute("ALTER TABLE scheduled_flights ADD COLUMN solo INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE scheduled_flights ADD COLUMN needs_review INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE scheduled_flights ADD COLUMN review_reason TEXT")
        conn.commit()
        # Backfill: any existing flight with no cfi_id was, under the old
        # rules, always meant as solo (there was no other way to get a NULL
        # cfi_id) - preserve that reading rather than treating pre-existing
        # bookings as newly "needs instructor".
        conn.execute("UPDATE scheduled_flights SET solo = 1 WHERE cfi_id IS NULL")
        conn.commit()

    # A needs-review flight gets front-and-center treatment on the dashboard
    # (same pattern as a Flight School squawk on the Maintenance dashboard):
    # a CFI/admin can acknowledge it to quiet the alert without having fixed
    # the underlying issue yet. Unlike a squawk there's no separate "repair"
    # step - _schedule_review_flag() clears needs_review on its own once a
    # CFI is actually assigned, and re-flagging (needs_review saved as 1
    # again on any later edit) resets these two columns back to NULL so an
    # unresolved flight surfaces for acknowledgment again instead of staying
    # silently dismissed.
    if "needs_review_acknowledged_at" not in sched_cols:
        conn.execute("ALTER TABLE scheduled_flights ADD COLUMN needs_review_acknowledged_at TEXT")
        conn.execute("ALTER TABLE scheduled_flights ADD COLUMN needs_review_acknowledged_by TEXT")
        conn.commit()

    # Live ADS-B tracking (Active Flight tab): each plane's Mode S / ICAO24
    # hex address, entered once by an admin from the plane's profile. Left
    # blank, that plane just doesn't show on the live map - there's no safe
    # way to derive this from the N-number without risking showing the
    # wrong aircraft's position, so it's admin-entered rather than computed.
    asset_cols5 = [r["name"] for r in conn.execute("PRAGMA table_info(assets)").fetchall()]
    if "icao24_hex" not in asset_cols5:
        conn.execute("ALTER TABLE assets ADD COLUMN icao24_hex TEXT")
        conn.commit()

    # Admin-pickable schedule color per CFI (nullable - falls back to the
    # deterministic hash-based color if never set, so existing CFIs don't
    # need a data backfill).
    cfi_cols = [r["name"] for r in conn.execute("PRAGMA table_info(cfis)").fetchall()]
    if "color" not in cfi_cols:
        conn.execute("ALTER TABLE cfis ADD COLUMN color TEXT")
        conn.commit()

    # Admin-pickable color per plane (set from the plane's own profile,
    # asset_form.html) - same idea as a CFI's color above, used to give
    # each plane a consistent accent on the flight dashboard's same-time
    # chip rows instead of an arbitrarily assigned one. Nullable - falls
    # back to a deterministic hash-based color if never set.
    asset_cols6 = [r["name"] for r in conn.execute("PRAGMA table_info(assets)").fetchall()]
    if "color" not in asset_cols6:
        conn.execute("ALTER TABLE assets ADD COLUMN color TEXT")
        conn.commit()

    # "Station" accounts (e.g. a generic "Shop" login): flight-side access
    # and can book the schedule like any student, but aren't a real trainee
    # - no solo-currency tracking, no personal dashboard widgets, and never
    # gets its own instructor color on the calendar (station bookings render
    # in the neutral "Solo" color same as any un-crewed flight).
    student_cols = [r["name"] for r in conn.execute("PRAGMA table_info(students)").fetchall()]
    if "is_station" not in student_cols:
        conn.execute("ALTER TABLE students ADD COLUMN is_station INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    # Guided-tour "seen" flags, one per side (Shop / Flight School) since a
    # given account might only ever touch one of them - keeps the pop-up
    # walkthrough from re-showing itself every login once dismissed, while
    # still letting anyone replay it from My Account.
    user_cols_tour = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "tour_seen_shop" not in user_cols_tour:
        conn.execute("ALTER TABLE users ADD COLUMN tour_seen_shop INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    if "tour_seen_flight" not in user_cols_tour:
        conn.execute("ALTER TABLE users ADD COLUMN tour_seen_flight INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    # Admin/CFI-only scheduling notes - a second notes field kept separate
    # from the existing (student-visible) `notes` column on purpose, so
    # self-service bookings and student-facing schedule views never render
    # it. See flight.py schedule_new/schedule_edit and schedule_form.html.
    sched_cols = [r["name"] for r in conn.execute("PRAGMA table_info(scheduled_flights)").fetchall()]
    if "private_notes" not in sched_cols:
        conn.execute("ALTER TABLE scheduled_flights ADD COLUMN private_notes TEXT")
        conn.commit()

    # Student billing credit/debit balance - a running ledger rather than a
    # single stored number, so every adjustment (funds added, a flight's
    # cost auto-deducted on log) has a record. `students.balance` is a
    # denormalized running total kept in sync by flight.py, so the Students
    # list/finance-row rendering doesn't need to SUM the ledger on every
    # page load.
    student_cols2 = [r["name"] for r in conn.execute("PRAGMA table_info(students)").fetchall()]
    if "balance" not in student_cols2:
        conn.execute("ALTER TABLE students ADD COLUMN balance REAL NOT NULL DEFAULT 0")
        conn.commit()
    conn.execute("""CREATE TABLE IF NOT EXISTS student_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL REFERENCES students(id),
        entry_type TEXT NOT NULL, -- 'funds_added' | 'flight_deduction' | 'adjustment'
        amount REAL NOT NULL, -- positive = credit (funds added), negative = debit (owed/deducted)
        flight_id INTEGER REFERENCES flights(id), -- set for entry_type='flight_deduction'
        note TEXT,
        created_by TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_student_ledger_student ON student_ledger(student_id, created_at)")
    conn.commit()

    # Landing counts per flight (day/night x full-stop/touch-and-go), for
    # FAA passenger-carrying currency (3 landings/90 days, night = full stop)
    # - separate from the existing "solo currency" (days since last dual).
    # Pause/resume on the start-stop clock: paused_at is set while paused,
    # and paused_seconds accumulates total paused time so instructor clock
    # billing and elapsed-time display can exclude it.
    flight_cols2 = [r["name"] for r in conn.execute("PRAGMA table_info(flights)").fetchall()]
    for col, ddl in [
        ("day_landings_fs", "ALTER TABLE flights ADD COLUMN day_landings_fs INTEGER"),
        ("day_landings_tg", "ALTER TABLE flights ADD COLUMN day_landings_tg INTEGER"),
        ("night_landings_fs", "ALTER TABLE flights ADD COLUMN night_landings_fs INTEGER"),
        ("night_landings_tg", "ALTER TABLE flights ADD COLUMN night_landings_tg INTEGER"),
        ("paused_at", "ALTER TABLE flights ADD COLUMN paused_at TEXT"),
        ("paused_seconds", "ALTER TABLE flights ADD COLUMN paused_seconds INTEGER NOT NULL DEFAULT 0"),
    ]:
        if col not in flight_cols2:
            conn.execute(ddl)
            conn.commit()

    # CFI ratings/credentials (admin-set, hidden from students) and a
    # separate pay rate (what the school pays the CFI, vs rate_per_hour
    # which is what students are billed) - visible only to admin + that CFI.
    cfi_cols2 = [r["name"] for r in conn.execute("PRAGMA table_info(cfis)").fetchall()]
    for col, ddl in [
        ("pay_rate_per_hour", "ALTER TABLE cfis ADD COLUMN pay_rate_per_hour REAL"),
        ("cred_cfi", "ALTER TABLE cfis ADD COLUMN cred_cfi INTEGER NOT NULL DEFAULT 0"),
        ("cred_cfii", "ALTER TABLE cfis ADD COLUMN cred_cfii INTEGER NOT NULL DEFAULT 0"),
        ("cred_mei", "ALTER TABLE cfis ADD COLUMN cred_mei INTEGER NOT NULL DEFAULT 0"),
        ("cred_agi", "ALTER TABLE cfis ADD COLUMN cred_agi INTEGER NOT NULL DEFAULT 0"),
        ("cred_bgi", "ALTER TABLE cfis ADD COLUMN cred_bgi INTEGER NOT NULL DEFAULT 0"),
        ("cred_igi", "ALTER TABLE cfis ADD COLUMN cred_igi INTEGER NOT NULL DEFAULT 0"),
        # Aircraft-category endorsements - which planes in the fleet this
        # CFI is qualified to fly/instruct in - separate from the
        # instructor certificates above.
        ("cred_high_performance", "ALTER TABLE cfis ADD COLUMN cred_high_performance INTEGER NOT NULL DEFAULT 0"),
        ("cred_complex", "ALTER TABLE cfis ADD COLUMN cred_complex INTEGER NOT NULL DEFAULT 0"),
        ("cred_tailwheel", "ALTER TABLE cfis ADD COLUMN cred_tailwheel INTEGER NOT NULL DEFAULT 0"),
        # Optional, admin-set - only used to power the "woman only
        # instructor" filter on the flexible flight finder.
        ("gender", "ALTER TABLE cfis ADD COLUMN gender TEXT"),
    ]:
        if col not in cfi_cols2:
            conn.execute(ddl)
            conn.commit()

    # Generic per-field audit trail, so a value like a student's plane-rate
    # or instructor-rate override can be clicked on to see who changed it
    # and when (the Account/balance side already has this via
    # student_ledger - this covers everything else).
    conn.execute("""CREATE TABLE IF NOT EXISTS field_change_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_type TEXT NOT NULL, -- 'student' | 'cfi' | 'plane'
        entity_id INTEGER NOT NULL,
        field_name TEXT NOT NULL, -- 'plane_rate_override' | 'rate_override' | ...
        old_value TEXT,
        new_value TEXT,
        changed_by TEXT,
        changed_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_field_change_log_entity ON field_change_log(entity_type, entity_id, field_name)")
    conn.commit()

    # Phone push notifications for flying sessions (30 min left / time's up /
    # overdue) - opt-in per user (notify_push_session_alerts), one row per
    # subscribed browser/device in push_subscriptions (Web Push, RFC 8030),
    # and push_pending is the "outbox" a service worker fetches from when a
    # (payload-less) push wakes it - see push.py for why it's built this way.
    user_cols3 = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "notify_push_session_alerts" not in user_cols3:
        conn.execute("ALTER TABLE users ADD COLUMN notify_push_session_alerts INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    # Split the single "flying session time" opt-in into one checkbox per
    # alert type (30 min left / time's up / running late). Each new column
    # starts out matching the old combined setting, so nobody who already
    # opted in loses alerts. notify_push_session_alerts stays as a "any of
    # the three is on" summary for older code paths.
    # Admin-picked schedule color per flight-school plane (Planes > Edit) -
    # the booking's block color on the Schedule, instructor color is the
    # stripe. NULL until an admin picks one.
    asset_cols_sc = [r["name"] for r in conn.execute("PRAGMA table_info(assets)").fetchall()]
    if "schedule_color" not in asset_cols_sc:
        conn.execute("ALTER TABLE assets ADD COLUMN schedule_color TEXT")
        conn.commit()

    user_cols4 = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    for col in ("notify_push_30min", "notify_push_timeup", "notify_push_late"):
        if col not in user_cols4:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
            conn.execute(f"UPDATE users SET {col} = notify_push_session_alerts")
    conn.commit()

    flight_cols3 = [r["name"] for r in conn.execute("PRAGMA table_info(flights)").fetchall()]
    for col, ddl in [
        ("session_warning_sent_at", "ALTER TABLE flights ADD COLUMN session_warning_sent_at TEXT"),
        ("session_expired_sent_at", "ALTER TABLE flights ADD COLUMN session_expired_sent_at TEXT"),
        ("overdue_alert_sent_at", "ALTER TABLE flights ADD COLUMN overdue_alert_sent_at TEXT"),
        ("overdue_acknowledged_at", "ALTER TABLE flights ADD COLUMN overdue_acknowledged_at TEXT"),
        # Updated ETA for a late flight (see flight.log_set_eta) - the later
        # bookings it runs into are worked out live from this, nothing is
        # stored on them, so the flag clears itself when the flight ends.
        ("eta_at", "ALTER TABLE flights ADD COLUMN eta_at TEXT"),
        ("eta_set_by", "ALTER TABLE flights ADD COLUMN eta_set_by TEXT"),
    ]:
        if col not in flight_cols3:
            conn.execute(ddl)
            conn.commit()

    conn.execute("""CREATE TABLE IF NOT EXISTS push_subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        endpoint TEXT NOT NULL UNIQUE,
        p256dh TEXT NOT NULL,
        auth TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_push_subscriptions_user ON push_subscriptions(user_id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS push_pending (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        title TEXT NOT NULL,
        body TEXT,
        tag TEXT,
        url TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_push_pending_user ON push_pending(user_id)")
    # Flight School Alerts tab: one row per time a booking was flagged for
    # review (no instructor, solo sign-off expired, over solo currency).
    # Kept in step with scheduled_flights.needs_review by
    # flight._sync_flight_alerts(); the row stays open (resolved_at NULL)
    # until the booking stops being flagged, then it moves to the resolved
    # archive with who/when/how. Acknowledging only fills acknowledged_*.
    conn.execute("""CREATE TABLE IF NOT EXISTS flight_alerts (
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
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_flight_alerts_open ON flight_alerts(resolved_at, scheduled_flight_id)")
    conn.commit()

    # Flight School Reports tab: plane issue / missing checklist / concerning
    # issue / suggestion, reportable by any logged-in user - see schema.sql
    # for the full comment and flight.reports_new()/reports_list().
    conn.execute("""CREATE TABLE IF NOT EXISTS flight_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        asset_id INTEGER REFERENCES assets(id),
        notes TEXT NOT NULL,
        reported_by TEXT,
        reported_at TEXT NOT NULL DEFAULT (datetime('now')),
        resolved_at TEXT,
        resolved_by TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_flight_reports_open ON flight_reports(resolved_at, category)")
    conn.commit()

    # Landings entered by hand on a student's profile (another school, a
    # rental, before this system) - still counted toward 90-day landing
    # currency. See schema.sql for the full comment.
    conn.execute("""CREATE TABLE IF NOT EXISTS manual_landings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL REFERENCES students(id),
        landing_date TEXT NOT NULL,
        day_landings INTEGER NOT NULL DEFAULT 0,
        night_landings INTEGER NOT NULL DEFAULT 0,
        note TEXT,
        created_by TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_manual_landings_student ON manual_landings(student_id, landing_date)")
    conn.commit()

    # Per-student opt-out for the "confirm your flight" push notification
    # (see flight._notify_booking_confirm) - the unconfirmed/confirmed mark
    # on the schedule and dashboard still always shows either way.
    student_cols_notify = [r["name"] for r in conn.execute("PRAGMA table_info(students)").fetchall()]
    if "notify_booking_confirm" not in student_cols_notify:
        conn.execute("ALTER TABLE students ADD COLUMN notify_booking_confirm INTEGER NOT NULL DEFAULT 1")
        conn.commit()

    # Flight Academy leaderboard: flying a student logs for themselves on
    # top of what the school's logged flights already count (distance,
    # extra landings, cross-countries, night/instrument time) - see academy.py.
    conn.execute("""CREATE TABLE IF NOT EXISTS academy_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL REFERENCES students(id),
        entry_date TEXT NOT NULL,
        kind TEXT NOT NULL,
        value REAL NOT NULL,
        note TEXT,
        created_by TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_academy_entries_student ON academy_entries(student_id, entry_date)")
    conn.commit()

    # Maintenance logbook entries + their per-project-type templates (see
    # logbook.py). Brand-new tables, safe to create with their indexes.
    conn.execute("""CREATE TABLE IF NOT EXISTS logbook_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset_id INTEGER REFERENCES assets(id),
        project_id INTEGER REFERENCES projects(id),
        section TEXT,
        log_type TEXT NOT NULL DEFAULT 'airframe',
        project_type TEXT,
        entry_date TEXT NOT NULL,
        tach_hours REAL,
        hobbs_hours REAL,
        body TEXT NOT NULL,
        signed_by TEXT,
        cert_number TEXT,
        created_by TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        deleted_at TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logbook_entries_asset ON logbook_entries(asset_id, log_type, entry_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logbook_entries_project ON logbook_entries(project_id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS logbook_templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_type TEXT NOT NULL,
        log_type TEXT NOT NULL,
        body TEXT NOT NULL,
        updated_by TEXT,
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(project_type, log_type)
    )""")
    conn.commit()

    # Flight Academy pilot logbook (see pilotlog.py): the CFI certificate
    # printed on students' entries, a flight's solo portion, and "part dual,
    # part solo" bookings.
    for table, col, ddl in (
        ("cfis", "cfi_cert_number", "ALTER TABLE cfis ADD COLUMN cfi_cert_number TEXT"),
        ("cfis", "cfi_cert_expires", "ALTER TABLE cfis ADD COLUMN cfi_cert_expires TEXT"),
        # Drawn signature (PNG data URL) the CFI makes on My CFI Profile -
        # copied onto each logbook entry they're the instructor on.
        ("cfis", "signature", "ALTER TABLE cfis ADD COLUMN signature TEXT"),
        ("cfis", "signature_updated_at", "ALTER TABLE cfis ADD COLUMN signature_updated_at TEXT"),
        ("flights", "solo_hours", "ALTER TABLE flights ADD COLUMN solo_hours REAL"),
        ("scheduled_flights", "part_solo", "ALTER TABLE scheduled_flights ADD COLUMN part_solo INTEGER NOT NULL DEFAULT 0"),
        # Guest bookings (intro flights / one-time flyers with no profile) -
        # see GUEST_USERNAME in flight.py.
        ("scheduled_flights", "guest_name", "ALTER TABLE scheduled_flights ADD COLUMN guest_name TEXT"),
        ("scheduled_flights", "guest_phone", "ALTER TABLE scheduled_flights ADD COLUMN guest_phone TEXT"),
        ("flights", "guest_name", "ALTER TABLE flights ADD COLUMN guest_name TEXT"),
        # First solo completed (set by a CFI/admin on the student profile) -
        # landing currency only shows once this is set.
        ("students", "first_solo_date", "ALTER TABLE students ADD COLUMN first_solo_date TEXT"),
        ("scheduled_flights", "guest_email", "ALTER TABLE scheduled_flights ADD COLUMN guest_email TEXT"),
        # End Flight stops the clock first (stopped_at); the flight is only
        # logged (ended_at) once Hobbs end + paid/unpaid are filled in.
        ("flights", "stopped_at", "ALTER TABLE flights ADD COLUMN stopped_at TEXT"),
        # New-project intake form (app.project_intake): NULL = project made
        # before intake existed (never asked), 'pending' | 'done' | 'skipped'.
        ("projects", "intake_status", "ALTER TABLE projects ADD COLUMN intake_status TEXT"),
        ("projects", "intake_json", "ALTER TABLE projects ADD COLUMN intake_json TEXT"),
        ("projects", "intake_at", "ALTER TABLE projects ADD COLUMN intake_at TEXT"),
        ("projects", "intake_by", "ALTER TABLE projects ADD COLUMN intake_by TEXT"),
        # TSA verification on file (set by a CFI/admin on the student
        # profile) - NULL = not verified yet.
        ("students", "tsa_verified_date", "ALTER TABLE students ADD COLUMN tsa_verified_date TEXT"),
        # A flight simulator added from the Planes tab (flight.simulator_new)
        # rather than a real aircraft in the Maintenance tile - still an
        # is_flight_asset row (so it's bookable), but has no Hobbs/Tach/
        # maintenance items to track.
        ("assets", "is_simulator", "ALTER TABLE assets ADD COLUMN is_simulator INTEGER NOT NULL DEFAULT 0"),
        # How this student usually pays (Cash/Check/Card, same option text
        # as End Flight's "How Paid" select) - shown as a hint there so the
        # instructor knows what to expect. NULL = not set.
        ("students", "pay_preference", "ALTER TABLE students ADD COLUMN pay_preference TEXT"),
        # A generic "station" CFI login (e.g. "Shop") - same idea as
        # students.is_station: grants CFI-level page access to whoever logs
        # in with it, but isn't a real instructor, so it's excluded from
        # every instructor picker/legend/dropdown (see the "AND is_station
        # = 0" added to the CFI queries in flight.py) while still being
        # manageable from Manage > CFIs.
        ("cfis", "is_station", "ALTER TABLE cfis ADD COLUMN is_station INTEGER NOT NULL DEFAULT 0"),
        # A simulator's own base rate ($/hr), set on its profile (Manage >
        # Planes > Add/Edit Simulator) - unlike a real plane, a sim has no
        # per-student override baked into the design by default, so this is
        # what's charged unless a student has their own Sim Rate override.
        ("assets", "sim_rate", "ALTER TABLE assets ADD COLUMN sim_rate REAL"),
        # Per-student override of the simulator rate above (Students > Edit),
        # same idea as plane_rate_override but specific to simulator time -
        # a student can be charged differently for the sim than for a plane.
        ("students", "sim_rate_override", "ALTER TABLE students ADD COLUMN sim_rate_override REAL"),
    ):
        if col not in [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]:
            conn.execute(ddl)
            conn.commit()
    conn.execute("""CREATE TABLE IF NOT EXISTS pilot_logbook (
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
        total REAL NOT NULL DEFAULT 0,
        dual REAL NOT NULL DEFAULT 0,
        solo REAL NOT NULL DEFAULT 0,
        pic REAL,
        xc REAL,
        night REAL,
        actual_inst REAL,
        sim_inst REAL,
        approaches INTEGER,
        day_ldg INTEGER,
        night_ldg INTEGER,
        night_fs INTEGER,
        remarks TEXT,
        instructor_name TEXT,
        instructor_cert TEXT,
        instructor_cert_exp TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        approved_at TEXT,
        approved_by TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pilot_logbook_student ON pilot_logbook(student_id, entry_date)")
    if "instructor_signature" not in [r["name"] for r in conn.execute("PRAGMA table_info(pilot_logbook)").fetchall()]:
        conn.execute("ALTER TABLE pilot_logbook ADD COLUMN instructor_signature TEXT")
    conn.execute("""CREATE TABLE IF NOT EXISTS academy_milestones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL REFERENCES students(id),
        track TEXT NOT NULL,
        item TEXT NOT NULL,
        done_date TEXT,
        signed_by TEXT,
        note TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(student_id, track, item)
    )""")
    conn.commit()
    # The shared "Guest / Intro" placeholder student that guest bookings use
    # (a station-type account: no login, no currency tracking, not on the
    # leaderboard).
    if not conn.execute("SELECT 1 FROM students WHERE username = '__guest__'").fetchone():
        import secrets
        from werkzeug.security import generate_password_hash
        conn.execute("""INSERT INTO students (name, username, password_hash, is_station, active, created_at)
                        VALUES ('Guest / Intro (no profile)', '__guest__', ?, 1, 1, datetime('now'))""",
                     (generate_password_hash(secrets.token_hex(24), method="pbkdf2:sha256"),))
        conn.commit()
    if not conn.execute("SELECT 1 FROM app_settings WHERE key = 'pilot_logbook_hobbs'").fetchone():
        # Logbook time switched from Tach to Hobbs: re-figure the entries
        # students haven't approved yet (approved ones are left as signed).
        # Runs once.
        import pilotlog
        for r in conn.execute("SELECT flight_id FROM pilot_logbook WHERE status = 'pending' AND flight_id IS NOT NULL").fetchall():
            pilotlog.ensure_entry(conn, r["flight_id"])
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('pilot_logbook_hobbs', datetime('now'))")
        conn.commit()
    if not conn.execute("SELECT 1 FROM app_settings WHERE key = 'first_solo_backfilled'").fetchone():
        # Once: students who already have a solo flight logged count as past
        # their first solo (dated by that flight), so their landing currency
        # doesn't vanish. Anyone else waits for a CFI/admin to tick it.
        conn.execute("""UPDATE students SET first_solo_date = (
                            SELECT MIN(f.flight_date) FROM flights f WHERE f.student_id = students.id AND f.solo = 1)
                        WHERE first_solo_date IS NULL
                          AND EXISTS (SELECT 1 FROM flights f WHERE f.student_id = students.id AND f.solo = 1)""")
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('first_solo_backfilled', datetime('now'))")
        conn.commit()
    if not conn.execute("SELECT 1 FROM app_settings WHERE key = 'pilot_logbook_backfilled'").fetchone():
        # First run: give every flight already logged a pending entry so
        # students start with their history (and their progress bars do too).
        # Runs once (flag in app_settings).
        import pilotlog
        for r in conn.execute("""SELECT id FROM flights WHERE ended_at IS NOT NULL OR started_at IS NULL
                                ORDER BY flight_date, id""").fetchall():
            pilotlog.ensure_entry(conn, r["id"])
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('pilot_logbook_backfilled', ?)", (now_iso(),))
        conn.commit()

    # Lets an admin opt a plane out of the Active Flight live map without
    # clearing its ICAO24 hex (e.g. a plane temporarily away for
    # maintenance) - see asset_form.html and adsb.py:_school_plane_map.
    # Defaults on so every plane that already has a hex keeps showing.
    asset_cols7 = [r["name"] for r in conn.execute("PRAGMA table_info(assets)").fetchall()]
    if "show_on_map" not in asset_cols7:
        conn.execute("ALTER TABLE assets ADD COLUMN show_on_map INTEGER NOT NULL DEFAULT 1")
        conn.commit()

    # Free-text maintenance preferences for a plane (oil type, tire pressure,
    # etc.) - kept separate from the general Notes field so it's quick to
    # find on the Aircraft page. See asset_form.html and asset_detail.html.
    asset_cols8 = [r["name"] for r in conn.execute("PRAGMA table_info(assets)").fetchall()]
    if "maint_prefs" not in asset_cols8:
        conn.execute("ALTER TABLE assets ADD COLUMN maint_prefs TEXT")
        conn.commit()

    _migrate_flight_accounts_to_users(conn)
    _carry_over_project_photos_to_assets(conn)

    # Customer portal: aircraft owners' own login, scoped to their linked
    # aircraft only. See schema.sql's comment above the customers table.
    conn.execute("""CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        password_plain TEXT,
        phone TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.commit()
    conn.execute("""CREATE TABLE IF NOT EXISTS customer_assets (
        customer_id INTEGER NOT NULL REFERENCES customers(id),
        asset_id INTEGER NOT NULL REFERENCES assets(id),
        PRIMARY KEY (customer_id, asset_id)
    )""")
    conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_customer_assets_asset ON customer_assets(asset_id)")
    conn.commit()

    proj_cols_cust = [r["name"] for r in conn.execute("PRAGMA table_info(projects)").fetchall()]
    for col in ("customer_confirmed_at", "customer_reschedule_requested_at", "customer_reschedule_note"):
        if col not in proj_cols_cust:
            conn.execute(f"ALTER TABLE projects ADD COLUMN {col} TEXT")
            conn.commit()

    # End Flight, marked Paid: how much was actually collected right then and
    # by what method, plus how much of it (if any) came out of the student's
    # existing credit - see flight.log_end. NULL/0 for a flight logged Unpaid
    # (its cost still auto-deducts from the balance, left owed as before).
    flight_cols_pay = [r["name"] for r in conn.execute("PRAGMA table_info(flights)").fetchall()]
    for col, ddl in (
        ("payment_method", "ALTER TABLE flights ADD COLUMN payment_method TEXT"),
        ("payment_amount", "ALTER TABLE flights ADD COLUMN payment_amount REAL"),
        ("credit_applied", "ALTER TABLE flights ADD COLUMN credit_applied REAL"),
    ):
        if col not in flight_cols_pay:
            conn.execute(ddl)
            conn.commit()

    # Maintenance Preferences split into quick-reference boxes (oil type,
    # tire pressures, anything else) instead of one free-text blob, so they
    # can show as a compact strip above Maintenance on the Aircraft page -
    # see asset_form.html/asset_detail.html. The old maint_prefs column's
    # text is carried over into maint_other once, so nothing already on file
    # is lost; maint_prefs itself is left in the database unused after that.
    asset_cols_maint = [r["name"] for r in conn.execute("PRAGMA table_info(assets)").fetchall()]
    for col, ddl in (
        ("maint_oil_type", "ALTER TABLE assets ADD COLUMN maint_oil_type TEXT"),
        ("maint_tire_nose", "ALTER TABLE assets ADD COLUMN maint_tire_nose TEXT"),
        ("maint_tire_mains", "ALTER TABLE assets ADD COLUMN maint_tire_mains TEXT"),
        ("maint_other", "ALTER TABLE assets ADD COLUMN maint_other TEXT"),
    ):
        if col not in asset_cols_maint:
            conn.execute(ddl)
            conn.commit()
    if not conn.execute("SELECT 1 FROM app_settings WHERE key = 'maint_prefs_split'").fetchone():
        conn.execute("UPDATE assets SET maint_other = maint_prefs WHERE maint_prefs IS NOT NULL AND maint_prefs != '' AND (maint_other IS NULL OR maint_other = '')")
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('maint_prefs_split', ?)", (now_iso(),))
        conn.commit()

    # A plane profile started from the "+ Add New" quick-create in the plane
    # dropdown (see project_form.html / app.asset_quick_new) has only a tail
    # number - flagged incomplete until someone opens Edit Profile and saves
    # the rest, which clears the flag (see app.asset_edit).
    if "profile_incomplete" not in asset_cols_maint:
        conn.execute("ALTER TABLE assets ADD COLUMN profile_incomplete INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    # Student confirmation of a booking a CFI/admin made for them (see
    # flight.py's _booking_confirm_state/schedule_confirm_booking).
    # confirm_required is only set on that one case - a student booking
    # their own flight, or a guest/intro with no login, has nobody else who
    # needs to confirm it, so those never show an unconfirmed/confirmed
    # mark at all. confirmed_at is NULL while still awaiting the student,
    # then the moment they confirmed (or, for a booking that never needed
    # confirming, the moment it was made).
    sf_cols_confirm = [r["name"] for r in conn.execute("PRAGMA table_info(scheduled_flights)").fetchall()]
    if "confirmed_at" not in sf_cols_confirm:
        conn.execute("ALTER TABLE scheduled_flights ADD COLUMN confirmed_at TEXT")
        # Bookings made before this feature existed were never asked for a
        # confirmation, so don't retroactively flag the whole existing
        # schedule as unconfirmed - only bookings created from here on go
        # through the new confirm step.
        conn.execute("UPDATE scheduled_flights SET confirmed_at = created_at")
        conn.commit()
    if "confirm_required" not in sf_cols_confirm:
        conn.execute("ALTER TABLE scheduled_flights ADD COLUMN confirm_required INTEGER NOT NULL DEFAULT 0")
        conn.commit()



    # ADS-B breadcrumb trail (Active Flight live map "track" line) - defined
    # in schema.sql but that file only runs for a brand-new database, so an
    # existing one (like this app's) never actually got this table without
    # this migration guard. Without it, every write/read against it (see
    # adsb.py's _record_track_points/get_track) was silently failing inside
    # their own try/except and the track line never had anything to draw -
    # this is that fix.
    conn.execute("""CREATE TABLE IF NOT EXISTS adsb_track_points (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        icao24 TEXT NOT NULL,
        lat REAL NOT NULL,
        lon REAL NOT NULL,
        recorded_at TEXT NOT NULL
    )""")
    conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_adsb_track_icao24 ON adsb_track_points(icao24, recorded_at)")
    conn.commit()

    # A plane's solo-booking color, admin-picked on Flight School > Planes >
    # Edit alongside its regular Schedule Color. Previously this was always
    # just an auto-brightened ("neon") version of the plane color - NULL
    # here still falls back to that, so nothing changes for a plane that
    # never gets one set.
    asset_cols_solo = [r["name"] for r in conn.execute("PRAGMA table_info(assets)").fetchall()]
    if "solo_color" not in asset_cols_solo:
        conn.execute("ALTER TABLE assets ADD COLUMN solo_color TEXT")
        conn.commit()

def _carry_over_project_photos_to_assets(conn):
    """One-time: an aircraft/asset with no photo of its own gets a COPY of
    its newest project's cover photo (or newest project photo), so planes
    that only had a picture on their maintenance project now show one on
    the Aircraft page too. It's a separate file + photos row, so the
    project's photo and the aircraft's photo can be changed or deleted
    independently afterwards. Guarded by an app_settings flag so deleting
    an aircraft photo later doesn't make it come back on the next restart."""
    import shutil
    flag = conn.execute("SELECT value FROM app_settings WHERE key = 'asset_photos_carried_over'").fetchone()
    if flag:
        return
    assets = conn.execute("""SELECT a.id FROM assets a
                             WHERE NOT EXISTS (SELECT 1 FROM photos ph WHERE ph.asset_id = a.id)""").fetchall()
    copied = 0
    for a in assets:
        src = conn.execute("""SELECT ph.filename FROM photos ph
                              JOIN projects p ON p.id = ph.project_id
                              WHERE p.asset_id = ? AND p.deleted_at IS NULL
                              ORDER BY p.created_at DESC, ph.is_cover DESC, ph.created_at DESC
                              LIMIT 1""", (a["id"],)).fetchone()
        if not src:
            continue
        src_path = os.path.join(UPLOAD_DIR, src["filename"])
        if not os.path.exists(src_path):
            continue
        ext = src["filename"].rsplit(".", 1)[-1].lower() if "." in src["filename"] else "jpg"
        unique = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
        new_name = f"{unique}.{ext}"
        try:
            shutil.copyfile(src_path, os.path.join(UPLOAD_DIR, new_name))
        except OSError:
            continue
        conn.execute("INSERT INTO photos (asset_id, filename, is_cover, created_at) VALUES (?, ?, 1, ?)",
                     (a["id"], new_name, now_iso()))
        copied += 1
    conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('asset_photos_carried_over', ?)",
                 (f"{now_iso()} ({copied} copied)",))
    conn.commit()


def _migrate_flight_accounts_to_users(conn):
    """One-time backfill: give every existing CFI/student profile a row in
    the shared `users` table (master login), carrying over their username
    and password so nobody has to reset anything. A CFI that was flagged
    `is_admin` becomes a master admin (full access to both programs)."""
    cfis_needing = conn.execute("SELECT * FROM cfis WHERE user_id IS NULL").fetchall()
    for c in cfis_needing:
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (c["username"],)).fetchone()
        if existing:
            user_id = existing["id"]
            if c["is_admin"]:
                conn.execute("UPDATE users SET is_master_admin = 1, flight_role = 'cfi' WHERE id = ?", (user_id,))
        else:
            cur = conn.execute(
                "INSERT INTO users (name, username, password_hash, is_master_admin, flight_role, active, created_at) "
                "VALUES (?, ?, ?, ?, 'cfi', ?, ?)",
                (c["name"], c["username"], c["password_hash"], 1 if c["is_admin"] else 0, c["active"], c["created_at"]))
            user_id = cur.lastrowid
        conn.execute("UPDATE cfis SET user_id = ? WHERE id = ?", (user_id, c["id"]))
    if cfis_needing:
        conn.commit()

    students_needing = conn.execute("SELECT * FROM students WHERE user_id IS NULL").fetchall()
    for s in students_needing:
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (s["username"],)).fetchone()
        if existing:
            user_id = existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO users (name, username, password_hash, flight_role, active, created_at) "
                "VALUES (?, ?, ?, 'student', ?, ?)",
                (s["name"], s["username"], s["password_hash"], s["active"], s["created_at"]))
            user_id = cur.lastrowid
        conn.execute("UPDATE students SET user_id = ? WHERE id = ?", (user_id, s["id"]))
    if students_needing:
        conn.commit()


def ensure_flight_profile(conn, user_row):
    """Make sure a user with flight_role set has the matching cfis/students
    profile row to hold their rate info, creating an empty one if needed
    (e.g. an admin just granted someone CFI access from the accounts page)."""
    if user_row["flight_role"] == "cfi":
        row = conn.execute("SELECT id FROM cfis WHERE user_id = ?", (user_row["id"],)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO cfis (name, username, password_hash, rate_per_hour, active, user_id, created_at) "
                "VALUES (?, ?, ?, 0, ?, ?, ?)",
                (user_row["name"], user_row["username"], user_row["password_hash"], user_row["active"],
                 user_row["id"], now_iso()))
            conn.commit()
    elif user_row["flight_role"] == "student":
        row = conn.execute("SELECT id FROM students WHERE user_id = ?", (user_row["id"],)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO students (name, username, password_hash, active, user_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_row["name"], user_row["username"], user_row["password_hash"], user_row["active"],
                 user_row["id"], now_iso()))
            conn.commit()


def gen_internal_barcode(conn):
    """Generate a unique internal barcode value for parts with no manufacturer barcode.
    Format: SHOP-XXXXXXXX (8 random alphanumeric chars), Code128-friendly (uppercase + digits)."""
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "SHOP-" + "".join(random.choices(alphabet, k=8))
        existing = conn.execute("SELECT 1 FROM parts WHERE barcode = ?", (code,)).fetchone()
        if not existing:
            return code


def gen_labor_code(conn):
    """Generate a unique scannable code for a laborer, e.g. LABOR-XXXXXXXX."""
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "LABOR-" + "".join(random.choices(alphabet, k=8))
        existing = conn.execute("SELECT 1 FROM laborers WHERE code = ?", (code,)).fetchone()
        if not existing:
            return code


def gen_project_code(conn, year=None):
    """Generate the next auto-numbered project code for a given year, e.g. 26-001, 26-002...
    `year` may be a 2 or 4 digit year string/int; defaults to the current year."""
    yy = str(year if year is not None else datetime.now().strftime("%y"))[-2:]
    rows = conn.execute("SELECT code FROM projects WHERE code LIKE ?", (f"{yy}-%",)).fetchall()
    max_num = 0
    for r in rows:
        try:
            num = int(r["code"].split("-", 1)[1])
            max_num = max(max_num, num)
        except (IndexError, ValueError, TypeError):
            continue
    return f"{yy}-{max_num + 1:03d}"


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "uploads")
ALLOWED_IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp", "heic"}


# ---------------------------------------------------------------------------
# Maintenance-item due/overdue logic, shared between the Maintenance side
# (app.py, every asset) and Flight School (flight.py, which surfaces oil
# change / 100-hour status for its own planes on the dashboard). Lives here
# rather than in app.py so flight.py can import it without a circular import
# (app.py already imports from flight.py, not the other way around).
# ---------------------------------------------------------------------------

MAINT_CATEGORY_COLORS = {
    "annual": "#6f42c1",         # purple
    "100hour": "#fd7e14",        # orange
    "oil_change": "#20c997",     # teal
    "scheduled_maint": "#0dcaf0",  # cyan - anything else
}
MAINT_CATEGORY_LABELS = {
    "annual": "Annual Inspection",
    "100hour": "100-Hour Inspection",
    "oil_change": "Oil Change",
    "scheduled_maint": "Scheduled Maintenance",
}


def asset_meter(asset, hour_type):
    """Returns the asset's current reading for the given meter ('tach' or 'hobbs')."""
    return asset["tach_hours"] if hour_type != "hobbs" else asset["hobbs_hours"]


def maintenance_status(item, current_hours):
    """Computes where a maintenance item stands relative to due, given the
    asset's current hours reading. `item` is a row/dict with the
    maintenance_items columns; `current_hours` is the asset's current_hours
    (may be None). Returns a dict: urgency ('overdue'|'due_soon'|'ok'|'unknown'),
    label (human string), remaining (numeric, hours or days), next_due."""
    if item["type"] == "calendar":
        if not item["interval_days"]:
            return {"urgency": "unknown", "label": "No interval set", "remaining": None, "next_due": None}
        from datetime import timedelta
        if item["last_done_date"]:
            try:
                last_date = datetime.strptime(str(item["last_done_date"])[:10], "%Y-%m-%d")
            except ValueError:
                last_date = datetime.now()
        else:
            last_date = datetime.now()
        next_due = last_date + timedelta(days=item["interval_days"])
        remaining = (next_due.date() - datetime.now().date()).days
        try:
            remind_lead = item["remind_lead"]
        except (KeyError, IndexError):
            remind_lead = None
        due_soon_threshold = remind_lead if remind_lead is not None else item["interval_days"] * 0.1
        if remaining <= 0:
            urgency = "overdue"
            label = f"{abs(remaining)} day{'s' if abs(remaining) != 1 else ''} overdue"
        elif remaining <= due_soon_threshold:
            urgency = "due_soon"
            label = f"{remaining} day{'s' if remaining != 1 else ''} remaining"
        else:
            urgency = "ok"
            label = f"{remaining} days remaining"
        return {"urgency": urgency, "label": label, "remaining": remaining, "next_due": next_due.strftime("%Y-%m-%d")}
    else:  # hours-based
        if not item["interval_hours"] or current_hours is None:
            return {"urgency": "unknown", "label": "Needs an hours reading", "remaining": None, "next_due": None}
        last = item["last_done_hours"] if item["last_done_hours"] is not None else 0
        next_due = last + item["interval_hours"]
        remaining = next_due - current_hours
        try:
            remind_lead = item["remind_lead"]
        except (KeyError, IndexError):
            remind_lead = None
        due_soon_threshold = remind_lead if remind_lead is not None else item["interval_hours"] * 0.1
        if remaining <= 0:
            urgency = "overdue"
            label = f"{abs(remaining):g} hrs overdue"
        elif remaining <= due_soon_threshold:
            urgency = "due_soon"
            label = f"{remaining:g} hrs remaining"
        else:
            urgency = "ok"
            label = f"{remaining:g} hrs remaining"
        return {"urgency": urgency, "label": label, "remaining": remaining, "next_due": next_due}


def allowed_image(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXT


def save_upload(file_storage):
    """Save an uploaded image with a collision-proof name. Returns the stored
    filename (not the full path) to keep in the photos table."""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    ext = file_storage.filename.rsplit(".", 1)[1].lower()
    alphabet = string.ascii_lowercase + string.digits
    unique = "".join(random.choices(alphabet, k=12))
    stored_name = f"{unique}.{ext}"
    file_storage.save(os.path.join(UPLOAD_DIR, stored_name))
    return stored_name
