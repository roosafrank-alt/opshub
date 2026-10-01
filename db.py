import sqlite3
import os
import random
import string
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "shopinv.db")
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    _track_request_conn(conn)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets readers keep working while a writer (e.g. resource PDF
    # indexing) is mid-transaction, and busy_timeout makes any remaining
    # contention retry for up to 30s instead of failing instantly with
    # "database is locked".
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _track_request_conn(conn):
    """Remembers every connection opened while handling a web request so
    close_request_conns() can roll back + close any that a route left open.
    Without this, a route that crashes between an UPDATE and its commit()
    keeps holding SQLite's write lock, and every other save in the app
    (scans, logins, flights) stalls for up to 30s with "database is locked"
    until Python happens to garbage-collect the dead connection. Outside a
    request (startup, background threads, scripts) this is a no-op."""
    try:
        from flask import g, has_request_context
    except ImportError:  # pragma: no cover
        return
    if has_request_context():
        g.setdefault("_opshub_db_conns", []).append(conn)


def close_request_conns(exc=None):
    """Flask teardown hook (registered in app.py): roll back and close any
    connection a route didn't close itself. Closing an already-closed
    sqlite3 connection is harmless, so well-behaved routes are unaffected."""
    try:
        from flask import g
    except ImportError:  # pragma: no cover
        return
    for conn in g.pop("_opshub_db_conns", []):
        try:
            conn.rollback()
        except sqlite3.ProgrammingError:
            pass  # already closed by the route - the normal case
        try:
            conn.close()
        except sqlite3.Error:
            pass


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
        # QA fix qa-startup-old-backup-flights: a billing charge
        # (student_ledger.flight_id) or logbook entry (pilot_logbook.flight_id)
        # already pointing at a flight makes DROP TABLE flights fail with
        # "FOREIGN KEY constraint failed" on an older backup that already has
        # those tables - same cause, same fix, as qa-project-purge-crash's
        # found_items rebuild: foreign keys off for the swap, ids unchanged
        # so every reference still lands on the right row after.
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
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
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
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
    if "completed_by" not in proj_sect_cols:
        conn.execute("ALTER TABLE project_sections ADD COLUMN completed_by TEXT")
        conn.commit()
    if "confirm_requested_at" not in proj_sect_cols:
        # Checking a sub area off now requests confirmation instead of
        # completing it outright - completed_at/completed_by only get set
        # once an Inspector (or admin) confirms it (see
        # app.project_section_complete/project_section_confirm).
        conn.execute("ALTER TABLE project_sections ADD COLUMN confirm_requested_at TEXT")
        conn.commit()
    if "confirm_requested_by" not in proj_sect_cols:
        conn.execute("ALTER TABLE project_sections ADD COLUMN confirm_requested_by TEXT")
        conn.commit()
    if "sent_back_at" not in proj_sect_cols:
        # Set when an Inspector/admin sends a sub area back instead of
        # confirming it (project_section_confirm) - kept even after it's
        # eventually confirmed, so a later look at it still shows it wasn't
        # approved on the first try.
        conn.execute("ALTER TABLE project_sections ADD COLUMN sent_back_at TEXT")
        conn.execute("ALTER TABLE project_sections ADD COLUMN sent_back_by TEXT")
        conn.commit()
    if "linked_squawk_kind" not in proj_sect_cols:
        # QA finding ux-squawk-on-project: a Sub Area created via "Fix on
        # this job"/"Do on this job" (see app.py) is tied to the plane's own
        # squawk or to-do it stands in for, so checking it off moves that
        # squawk/to-do to Inspection too, and confirming/sending it back
        # does the same - one record, not a copy, wherever it shows.
        conn.execute("ALTER TABLE project_sections ADD COLUMN linked_squawk_kind TEXT")
        conn.execute("ALTER TABLE project_sections ADD COLUMN linked_squawk_id INTEGER")
        conn.execute("ALTER TABLE project_sections ADD COLUMN linked_todo_id INTEGER REFERENCES plane_todos(id)")
        conn.commit()
    if "completed_by" not in [r["name"] for r in conn.execute("PRAGMA table_info(projects)").fetchall()]:
        conn.execute("ALTER TABLE projects ADD COLUMN completed_by TEXT")
        conn.commit()
    if "notes" not in proj_sect_cols:
        # Idea "Discrepancy List": notes is shop-only, never shown on the
        # invoice or in My Aircraft; description is the write-up an owner
        # actually sees there (see customer._project_bill/project_detail()).
        conn.execute("ALTER TABLE project_sections ADD COLUMN notes TEXT")
        conn.execute("ALTER TABLE project_sections ADD COLUMN description TEXT")
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
    if "is_optional" not in [r["name"] for r in conn.execute("PRAGMA table_info(task_template_areas)").fetchall()]:
        # An optional-service area (e.g. "Spark plug replacement" under
        # Annual) only gets added to a new project when its checkbox is
        # ticked on New Project - a required one (the default) is always
        # added the moment its Quick Type is picked, same as before this
        # column existed. See app.project_new()'s optional_area_ids handling.
        conn.execute("ALTER TABLE task_template_areas ADD COLUMN is_optional INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    # Quick Type buttons beyond the 4 built-in ones (Annual/100hr/Oil
    # Change/Maintenance, still hardcoded as app.QUICK_TYPES) - admins add
    # their own from Manage > Task Templates. See app._all_quick_types().
    conn.execute("""CREATE TABLE IF NOT EXISTS task_template_types (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
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
        groundschool_access INTEGER NOT NULL DEFAULT 0,
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
        ("groundschool_access", "ALTER TABLE users ADD COLUMN groundschool_access INTEGER NOT NULL DEFAULT 0"),
        # Every role an account holds, comma-separated (see user_shop_roles).
        # shop_role/flight_role stay as the account's main role per program.
        ("shop_roles", "ALTER TABLE users ADD COLUMN shop_roles TEXT"),
        ("flight_roles", "ALTER TABLE users ADD COLUMN flight_roles TEXT"),
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
        # A separate billed rate for instruction given outside a Flight
        # School plane (e.g. in a student's own aircraft) - NULL means "use
        # rate_per_hour", same as pay_rate_per_hour's own NULL-means-unset
        # convention above. Not wired into any billing flow yet (there's no
        # way to log a flight without picking a real Flight School plane) -
        # just the rate on file for now, for manual reference until that's
        # built.
        ("external_rate", "ALTER TABLE cfis ADD COLUMN external_rate REAL"),
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

    # Per-student notification feed - "your flight was approved/denied",
    # etc. (see flight._notify_student). Distinct from flight_alerts above,
    # which is the instructor-facing review queue; this is student-facing,
    # one row per event, read_at set once they've seen it (my_alerts()/
    # notification_dismiss() in flight.py). Shown as a dashboard banner
    # (unread only) and in full on the student's Alerts tab.
    conn.execute("""CREATE TABLE IF NOT EXISTS student_notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL,
        category TEXT NOT NULL,
        message TEXT NOT NULL,
        link TEXT,
        created_at TEXT NOT NULL,
        read_at TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_student_notifications_lookup ON student_notifications(student_id, read_at, created_at)")
    conn.commit()

    # Ground School: each row is one FAA Airman Certification Standards (ACS)
    # document (Private Pilot, Instrument, etc.) - the uploaded PDF is the
    # source of record; acs_areas/acs_tasks/acs_task_elements are what
    # groundschool._parse_and_store() extracts from it (see acs_parser.py).
    # status: 'empty' (uploaded, not yet parsed/failed) | 'parsed'.
    conn.execute("""CREATE TABLE IF NOT EXISTS acs_ratings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        slug TEXT NOT NULL UNIQUE,
        doc_number TEXT,
        pdf_filename TEXT,
        uploaded_by INTEGER,
        uploaded_at TEXT NOT NULL,
        parsed_at TEXT,
        status TEXT NOT NULL DEFAULT 'empty',
        parse_error TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS acs_areas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rating_id INTEGER NOT NULL REFERENCES acs_ratings(id),
        code TEXT NOT NULL,
        title TEXT NOT NULL,
        order_index INTEGER NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_acs_areas_rating ON acs_areas(rating_id, order_index)")
    conn.execute("""CREATE TABLE IF NOT EXISTS acs_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        area_id INTEGER NOT NULL REFERENCES acs_areas(id),
        code TEXT NOT NULL,
        title TEXT NOT NULL,
        acs_references TEXT,
        objective TEXT,
        order_index INTEGER NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_acs_tasks_area ON acs_tasks(area_id, order_index)")
    # notes: the ACS's own "Note:" callouts for a task (e.g. "If K2 is
    # selected, the evaluator must assess..."), shown verbatim alongside
    # Objective/References - distinct from lesson content below.
    conn.execute("""CREATE TABLE IF NOT EXISTS acs_task_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL REFERENCES acs_tasks(id),
        note TEXT NOT NULL,
        order_index INTEGER NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_acs_task_notes_task ON acs_task_notes(task_id, order_index)")
    # kind: 'knowledge' | 'risk_management' | 'skills'. code is the ACS's own
    # reference code (e.g. "PA.I.A.K1") - kept verbatim so it can be quoted
    # on a checkride prep sheet the same way the ACS itself is.
    conn.execute("""CREATE TABLE IF NOT EXISTS acs_task_elements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL REFERENCES acs_tasks(id),
        kind TEXT NOT NULL,
        code TEXT NOT NULL,
        text TEXT NOT NULL,
        order_index INTEGER NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_acs_task_elements_task ON acs_task_elements(task_id, kind, order_index)")
    # One editable lesson body per task (chief-instructor/admin authored -
    # talking points, materials, links - separate from the verbatim ACS text
    # above). One row per task, created on first save.
    conn.execute("""CREATE TABLE IF NOT EXISTS acs_lesson_content (
        task_id INTEGER PRIMARY KEY REFERENCES acs_tasks(id),
        content TEXT NOT NULL DEFAULT '',
        updated_by INTEGER,
        updated_at TEXT
    )""")
    # Per-student completion/sign-off against a task, one row per
    # (student, task) - re-signing updates it in place (see
    # groundschool.task_signoff()).
    conn.execute("""CREATE TABLE IF NOT EXISTS acs_task_signoff (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL REFERENCES students(id),
        task_id INTEGER NOT NULL REFERENCES acs_tasks(id),
        cfi_id INTEGER REFERENCES cfis(id),
        signed_off_at TEXT NOT NULL,
        notes TEXT,
        UNIQUE(student_id, task_id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_acs_signoff_student ON acs_task_signoff(student_id)")

    conn.execute("""CREATE TABLE IF NOT EXISTS acs_resources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        doc_type TEXT,
        filename TEXT NOT NULL,
        uploaded_by INTEGER,
        uploaded_at TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS acs_resource_pages (
        resource_id INTEGER NOT NULL REFERENCES acs_resources(id),
        page_num INTEGER NOT NULL,
        text TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (resource_id, page_num)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS acs_element_lesson_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        element_id INTEGER NOT NULL REFERENCES acs_task_elements(id),
        order_index INTEGER NOT NULL,
        kind TEXT NOT NULL,
        title TEXT,
        body TEXT,
        url TEXT,
        required INTEGER NOT NULL DEFAULT 1
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_acs_element_items_element ON acs_element_lesson_items(element_id, order_index)")
    conn.execute("""CREATE TABLE IF NOT EXISTS acs_element_item_progress (
        student_id INTEGER NOT NULL REFERENCES students(id),
        item_id INTEGER NOT NULL REFERENCES acs_element_lesson_items(id),
        done_at TEXT NOT NULL,
        PRIMARY KEY (student_id, item_id)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS acs_element_completion (
        student_id INTEGER NOT NULL REFERENCES students(id),
        element_id INTEGER NOT NULL REFERENCES acs_task_elements(id),
        self_completed_at TEXT,
        cfi_id INTEGER REFERENCES cfis(id),
        cfi_verified_at TEXT,
        PRIMARY KEY (student_id, element_id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_acs_element_completion_student ON acs_element_completion(student_id)")
    # A Task's own References (the ACS's citation list, e.g. "14 CFR 61.83,
    # AC 61-65") now link to the complete source document rather than a
    # short excerpt (see linked_references_html) - this tracks, per student
    # per Task, that they actually opened one of those links before letting
    # them mark the reading as done (task_reference_click/task_mark_read in
    # groundschool.py). Separate from acs_element_completion, which tracks
    # a Knowledge/Risk/Skills element's own lesson items, not the Task-level
    # reading assignment.
    conn.execute("""CREATE TABLE IF NOT EXISTS acs_task_reading_progress (
        student_id INTEGER NOT NULL REFERENCES students(id),
        task_id INTEGER NOT NULL REFERENCES acs_tasks(id),
        reference_opened_at TEXT,
        marked_read_at TEXT,
        cfi_id INTEGER REFERENCES cfis(id),
        cfi_verified_at TEXT,
        PRIMARY KEY (student_id, task_id)
    )""")
    conn.commit()
    # cfi_id/cfi_verified_at added later - the CFI's own check-off, once the
    # student's marked_read_at is set, that they reviewed this Task's
    # knowledge area with the student (groundschool.task_verify_reading).
    reading_progress_cols = [r["name"] for r in conn.execute("PRAGMA table_info(acs_task_reading_progress)").fetchall()]
    if "cfi_id" not in reading_progress_cols:
        conn.execute("ALTER TABLE acs_task_reading_progress ADD COLUMN cfi_id INTEGER REFERENCES cfis(id)")
        conn.commit()
    if "cfi_verified_at" not in reading_progress_cols:
        conn.execute("ALTER TABLE acs_task_reading_progress ADD COLUMN cfi_verified_at TEXT")
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
        # A CFI's time off can now repeat weekly (e.g. "every Saturday off")
        # instead of being one specific date - off_date is then the anchor
        # date (any date that falls on the repeated weekday), matched by
        # weekday going forward. See _cfi_time_off_conflict/_cfi_schedule_week.
        ("cfi_time_off", "recurs_weekly", "ALTER TABLE cfi_time_off ADD COLUMN recurs_weekly INTEGER NOT NULL DEFAULT 0"),
        # Links a notification back to the booking it's about (approved/
        # denied - see _notify_student callers), so dismissing the booking
        # from "Your Requests" (schedule_dismiss) can also clear its
        # matching dashboard notification instead of leaving it behind.
        # NULL for older rows and notifications not tied to one booking.
        ("student_notifications", "scheduled_flight_id", "ALTER TABLE student_notifications ADD COLUMN scheduled_flight_id INTEGER"),
        # A plane to-do can be handed to a specific laborer, same idea as a
        # squawk's assigned_to - lets it show up on that tech's own My Tasks
        # page instead of only living on the plane's page. NULL means
        # unassigned, same as before this column existed.
        ("plane_todos", "assigned_to", "ALTER TABLE plane_todos ADD COLUMN assigned_to INTEGER REFERENCES users(id)"),
        # A flight in a student's own plane (see _get_or_create_own_plane_asset
        # in flight.py) has no real Hobbs/Tach to read - there's just one
        # recorded time box for how long the flight actually took, billed at
        # the CFI's Students Aircraft Rate (cfis.external_rate) instead of the
        # usual Hobbs/Tach-derived hours. NULL for every other flight.
        ("flights", "recorded_hours", "ALTER TABLE flights ADD COLUMN recorded_hours REAL"),
        # A plane to-do's checkbox no longer completes it outright - same
        # request/confirm two-step a project sub area or a squawk's repair
        # already requires (see project_section_complete/confirm and
        # squawk_repair/repair_confirm): checking it off asks for an
        # Inspector (or admin) to confirm before "done" is set. NULL for
        # every to-do that isn't currently awaiting confirmation.
        ("plane_todos", "confirm_requested_at", "ALTER TABLE plane_todos ADD COLUMN confirm_requested_at TEXT"),
        ("plane_todos", "confirm_requested_by", "ALTER TABLE plane_todos ADD COLUMN confirm_requested_by TEXT"),
        ("plane_todos", "confirmed_by", "ALTER TABLE plane_todos ADD COLUMN confirmed_by TEXT"),
        ("plane_todos", "sent_back_at", "ALTER TABLE plane_todos ADD COLUMN sent_back_at TEXT"),
        ("plane_todos", "sent_back_by", "ALTER TABLE plane_todos ADD COLUMN sent_back_by TEXT"),
        # Idea "add who updated the tach": who last logged a Hobbs/Tach
        # reading - "Shop - <name>" from asset_update_hours, "Owner - <name>"
        # from the My Aircraft portal's customer_update_hours - next to the
        # existing hobbs_updated_at/tach_updated_at.
        ("assets", "hobbs_updated_by", "ALTER TABLE assets ADD COLUMN hobbs_updated_by TEXT"),
        ("assets", "tach_updated_by", "ALTER TABLE assets ADD COLUMN tach_updated_by TEXT"),
        # Idea "solo flights allowed": a Planes > Edit checkbox that blocks
        # booking a plane solo regardless of the student's own sign-off.
        # Defaults to allowed so no existing plane is suddenly un-bookable.
        ("assets", "solo_allowed", "ALTER TABLE assets ADD COLUMN solo_allowed INTEGER NOT NULL DEFAULT 1"),
        # QA feat-shop-job-payments: whether a job's bill has gone out and
        # been paid, shown on Manage > Billing - 'not_invoiced' (default,
        # NULL reads the same way), 'invoiced' or 'paid'.
        ("projects", "payment_status", "ALTER TABLE projects ADD COLUMN payment_status TEXT"),
        ("projects", "invoiced_at", "ALTER TABLE projects ADD COLUMN invoiced_at TEXT"),
        ("projects", "invoiced_by", "ALTER TABLE projects ADD COLUMN invoiced_by TEXT"),
        ("projects", "paid_at", "ALTER TABLE projects ADD COLUMN paid_at TEXT"),
        ("projects", "paid_by", "ALTER TABLE projects ADD COLUMN paid_by TEXT"),
        ("projects", "paid_method", "ALTER TABLE projects ADD COLUMN paid_method TEXT"),
        # QA feat-owed-balance-on-bookings: when a student's owed balance
        # crosses the school's limit (Settings > Balance Hold), every one of
        # their upcoming bookings flips to status 'balance_hold' -
        # hold_previous_status remembers what to restore it to once paid
        # down (see flight.check_balance_hold). held_release_date is the day
        # (lesson date minus 5 days) the slot opens to everyone else if
        # still unpaid by then; released_from_hold_at is set once that
        # actually happens (see flight.check_balance_hold_releases).
        ("scheduled_flights", "hold_previous_status", "ALTER TABLE scheduled_flights ADD COLUMN hold_previous_status TEXT"),
        ("scheduled_flights", "hold_started_at", "ALTER TABLE scheduled_flights ADD COLUMN hold_started_at TEXT"),
        ("scheduled_flights", "held_release_date", "ALTER TABLE scheduled_flights ADD COLUMN held_release_date TEXT"),
        ("scheduled_flights", "released_from_hold_at", "ALTER TABLE scheduled_flights ADD COLUMN released_from_hold_at TEXT"),
        # Idea "Credit card": a simulated Stripe-style "Pay with Card" option
        # next to Mark Paid on Billing - no real Stripe account or charges,
        # just a fake receipt so Frank can see how the flow would feel.
        # NULL unless paid_method is 'Card' through that button.
        ("projects", "card_last4", "ALTER TABLE projects ADD COLUMN card_last4 TEXT"),
        ("projects", "card_charge_id", "ALTER TABLE projects ADD COLUMN card_charge_id TEXT"),
        # Idea "Credit card" (revision): the same simulated "Pay with Card"
        # also on Fly with Kate's End Flight payment box and a student's Add
        # Funds - NULL unless payment_method/the add-funds note is 'Card'
        # through one of those.
        ("flights", "card_last4", "ALTER TABLE flights ADD COLUMN card_last4 TEXT"),
        ("flights", "card_charge_id", "ALTER TABLE flights ADD COLUMN card_charge_id TEXT"),
        # Idea "oil": who logged an Oil Added reading, next to the existing
        # oil_added_qt - whoever's session set it (End Session, Log Flight,
        # Edit Flight, or the mid-flight progress update), same pattern as
        # assets.hobbs_updated_by/tach_updated_by above.
        ("flights", "oil_added_by", "ALTER TABLE flights ADD COLUMN oil_added_by TEXT"),
        # Wave invoicing: which Wave invoice (wave_invoices.id) a flight was
        # billed on, so Wave marking that invoice paid marks these flights
        # paid - and so the same flight is never put on two invoices.
        ("flights", "wave_invoice_id", "ALTER TABLE flights ADD COLUMN wave_invoice_id INTEGER"),
        # Wave with several accounts: which account (Admin > Wave, 1-3) and
        # which Wave business an invoice was made in, so its payment is
        # always looked up there even after a program swaps accounts.
        ("wave_invoices", "account", "ALTER TABLE wave_invoices ADD COLUMN account INTEGER"),
        ("wave_invoices", "business_id", "ALTER TABLE wave_invoices ADD COLUMN business_id TEXT"),
        # QA finding feat-job-closeout-owner-ready: set when "Tell the owner
        # it's ready" on the Close out this job pop-up actually reached an
        # owner (email or text) - see app._notify_owner_job_ready.
        ("projects", "ready_notified_at", "ALTER TABLE projects ADD COLUMN ready_notified_at TEXT"),
        ("projects", "ready_notified_by", "ALTER TABLE projects ADD COLUMN ready_notified_by TEXT"),
    ):
        if col not in [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]:
            conn.execute(ddl)
            conn.commit()
    # Wave with several accounts (see wave_billing.ACCOUNTS/PROGRAMS): the
    # first version had one Wave connection used by both the Shop and the
    # Flight School. It becomes Account 1, given to (and the default for)
    # both programs; its invoices are marked as made in Account 1, and
    # remembered customers move to per-business keys. The old keys are
    # removed afterwards, so this only ever runs once.
    old = {r["key"]: r["value"] or "" for r in conn.execute(
        """SELECT key, value FROM app_settings WHERE key IN ('wave_access_token', 'wave_business_id',
           'wave_labor_product_id', 'wave_parts_product_id', 'wave_flight_product_id')""").fetchall()}
    if old:
        biz = old.get("wave_business_id", "")
        new = {"wave_acct1_token": old.get("wave_access_token", ""), "wave_acct1_business_id": biz,
               "wave_acct1_labor_product_id": old.get("wave_labor_product_id", ""),
               "wave_acct1_parts_product_id": old.get("wave_parts_product_id", ""),
               "wave_acct1_flight_product_id": old.get("wave_flight_product_id", "")}
        if old.get("wave_access_token"):
            new.update(wave_shop_accounts="1", wave_shop_default="1", wave_flight_accounts="1", wave_flight_default="1")
        for key, value in new.items():
            conn.execute("INSERT INTO app_settings (key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        conn.execute("UPDATE wave_invoices SET account = 1, business_id = COALESCE(business_id, ?) WHERE account IS NULL",
                     (biz,))
        if biz:
            conn.execute("""UPDATE app_settings SET key = 'wavecust:' || ? || ':' || substr(key, 10)
                            WHERE key LIKE 'wavecust:%' AND substr(key, 10) NOT LIKE '%:%'""", (biz,))
        conn.execute("""DELETE FROM app_settings WHERE key IN ('wave_access_token', 'wave_business_id',
                        'wave_labor_product_id', 'wave_parts_product_id', 'wave_flight_product_id')""")
        conn.commit()
    # QA feat-tool-calibration: torque wrenches, gauges and testers that need
    # periodic recalibration - a due date derives from the last calibration
    # plus the tool's own interval, NULL interval meaning "not required".
    # tool_calibrations keeps the full history (each time it went out);
    # shop_tools.last_calibrated_date/next_due_date/cert_file just mirror
    # that history's most recent row for quick list/badge/dashboard reads.
    conn.execute("""CREATE TABLE IF NOT EXISTS shop_tools (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        serial TEXT,
        location TEXT,
        calibration_interval_days INTEGER,
        last_calibrated_date TEXT,
        next_due_date TEXT,
        cert_file TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        deleted_at TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shop_tools_due ON shop_tools(next_due_date)")
    conn.execute("""CREATE TABLE IF NOT EXISTS tool_calibrations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tool_id INTEGER NOT NULL REFERENCES shop_tools(id),
        calibrated_at TEXT NOT NULL,
        cert_file TEXT,
        performed_by TEXT,
        note TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_calibrations_tool ON tool_calibrations(tool_id, calibrated_at)")
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

    # labor_sessions.project_id used to be NOT NULL (every clock-in had to be
    # against a real project). The "General Shop" clock-in code (cleanup,
    # meetings, other non-project time) needs project_id to be allowed NULL
    # instead - SQLite can't drop a NOT NULL in place, so this rebuilds the
    # table (once) when it's still the old, stricter shape.
    ls_cols = conn.execute("PRAGMA table_info(labor_sessions)").fetchall()
    project_id_col = next((c for c in ls_cols if c["name"] == "project_id"), None)
    if project_id_col is not None and project_id_col["notnull"]:
        # Same class of bug as qa-project-purge-crash (found_items) and
        # qa-startup-old-backup-flights: nothing references labor_sessions
        # today, but DROP TABLE labor_sessions still runs with foreign keys
        # on, so the day something does (a future column REFERENCES
        # labor_sessions(id)) this rebuild would crash startup exactly the
        # same way on an old backup. Off for the swap, ids unchanged, back
        # on after - matches the other two rebuilds so predeploy_check's
        # "every DROP TABLE rebuild guards its foreign keys" check passes.
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("DROP TABLE IF EXISTS labor_sessions_new")
            conn.execute("""CREATE TABLE labor_sessions_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                laborer_id INTEGER NOT NULL REFERENCES laborers(id),
                project_id INTEGER REFERENCES projects(id),
                section TEXT,
                started_at TEXT NOT NULL DEFAULT (datetime('now')),
                ended_at TEXT,
                hours REAL,
                rate REAL,
                cost REAL,
                note TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )""")
            conn.execute("""INSERT INTO labor_sessions_new
                (id, laborer_id, project_id, section, started_at, ended_at, hours, rate, cost, note, created_at)
                SELECT id, laborer_id, project_id, section, started_at, ended_at, hours, rate, cost, note, created_at
                FROM labor_sessions""")
            conn.execute("DROP TABLE labor_sessions")
            conn.execute("ALTER TABLE labor_sessions_new RENAME TO labor_sessions")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_labor_sessions_laborer ON labor_sessions(laborer_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_labor_sessions_project ON labor_sessions(project_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_labor_sessions_open ON labor_sessions(laborer_id, ended_at)")
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

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
    # Owner "Book it" requests: the week the owner asked for (Monday, YYYY-MM-DD)
    # and which reminder it came from. A request is waiting while scheduled_date is NULL.
    proj_cols_cust = [r["name"] for r in conn.execute("PRAGMA table_info(projects)").fetchall()]
    for col, ddl in (("customer_requested_week", "TEXT"), ("customer_requested_item_id", "INTEGER")):
        if col not in proj_cols_cust:
            conn.execute(f"ALTER TABLE projects ADD COLUMN {col} {ddl}")
            conn.commit()

    # "Promised back" date on a job (shop-only unless promised_show_owner = 1).
    proj_cols_cust = [r["name"] for r in conn.execute("PRAGMA table_info(projects)").fetchall()]
    for col, ddl in (("promised_date", "TEXT"), ("promised_show_owner", "INTEGER NOT NULL DEFAULT 0")):
        if col not in proj_cols_cust:
            conn.execute(f"ALTER TABLE projects ADD COLUMN {col} {ddl}")
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

    # Manual / Illustrated-Parts-Catalog library (Manage > Manuals) - PDFs
    # tagged to a make/model/year/serial range at upload and auto-indexed
    # page-by-page (see manuals.py) so a plane's detail page can show only
    # the manuals that actually cover it, and a manual can be searched by
    # part or figure number.
    conn.execute("""CREATE TABLE IF NOT EXISTS manuals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        manual_type TEXT NOT NULL DEFAULT 'maintenance',
        filename TEXT NOT NULL,
        make TEXT,
        model TEXT,
        year_start INTEGER,
        year_end INTEGER,
        serial_start TEXT,
        serial_end TEXT,
        page_count INTEGER NOT NULL DEFAULT 0,
        uploaded_by INTEGER,
        created_at TEXT NOT NULL
    )""")
    conn.commit()
    conn.execute("""CREATE TABLE IF NOT EXISTS manual_pages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        manual_id INTEGER NOT NULL,
        page_num INTEGER NOT NULL,
        text_content TEXT,
        figure_refs TEXT,
        part_numbers TEXT
    )""")
    conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_manual_pages_manual ON manual_pages(manual_id, page_num)")
    conn.commit()
    conn.execute("""CREATE TABLE IF NOT EXISTS manual_page_parts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        manual_page_id INTEGER NOT NULL,
        part_number TEXT NOT NULL
    )""")
    conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_manual_page_parts_num ON manual_page_parts(part_number)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_manual_page_parts_page ON manual_page_parts(manual_page_id)")
    conn.commit()

    # A student's own "Request Change" / "Cancel" on their scheduled flight
    # (flight.schedule_request_change / schedule_student_cancel) - both
    # require a comment, kept here (and pushed to the CFI/admin) so the
    # school knows why.
    sched_cols_req = [r["name"] for r in conn.execute("PRAGMA table_info(scheduled_flights)").fetchall()]
    if "change_request_note" not in sched_cols_req:
        conn.execute("ALTER TABLE scheduled_flights ADD COLUMN change_request_note TEXT")
        conn.execute("ALTER TABLE scheduled_flights ADD COLUMN change_requested_at TEXT")
        conn.execute("ALTER TABLE scheduled_flights ADD COLUMN cancel_reason TEXT")
        conn.commit()

    # An instructor's required reason for denying a student's flight request
    # (flight.schedule_deny) - the student sees this reason directly instead
    # of a generic "contact your instructor".
    sched_cols_deny = [r["name"] for r in conn.execute("PRAGMA table_info(scheduled_flights)").fetchall()]
    if "deny_reason" not in sched_cols_deny:
        conn.execute("ALTER TABLE scheduled_flights ADD COLUMN deny_reason TEXT")
        conn.commit()

    # A student dismissing a denied request off their own "Your Requests"
    # list (flight.schedule_dismiss) - just hides it from that list, keeps
    # the row (and its deny_reason) on file same as ever.
    sched_cols_dismiss = [r["name"] for r in conn.execute("PRAGMA table_info(scheduled_flights)").fetchall()]
    if "student_dismissed_at" not in sched_cols_dismiss:
        conn.execute("ALTER TABLE scheduled_flights ADD COLUMN student_dismissed_at TEXT")
        conn.commit()

    # Assigning a squawk to a specific tech (users.shop_role='tech') so it
    # shows on that person's own dashboard as something to do, separate from
    # someone just acknowledging the squawk exists. worker_acknowledged_at
    # is that tech's own "I've seen this, I've got it" - distinct from
    # acknowledged_at/squawk_acknowledged_at above, which just means someone
    # (anyone on the shop side) has seen the squawk at all. Whoever assigned
    # it can see whether the tech has acknowledged the assignment yet - see
    # get_open_squawks()/squawk_assign()/squawk_worker_ack() in app.py.
    flight_cols_assign = [r["name"] for r in conn.execute("PRAGMA table_info(flights)").fetchall()]
    if "squawk_assigned_to" not in flight_cols_assign:
        conn.execute("ALTER TABLE flights ADD COLUMN squawk_assigned_to INTEGER")
        conn.execute("ALTER TABLE flights ADD COLUMN squawk_worker_acknowledged_at TEXT")
        conn.execute("ALTER TABLE flights ADD COLUMN squawk_worker_acknowledged_by TEXT")
        conn.commit()
    quick_cols_assign = [r["name"] for r in conn.execute("PRAGMA table_info(plane_squawks)").fetchall()]
    if "assigned_to" not in quick_cols_assign:
        conn.execute("ALTER TABLE plane_squawks ADD COLUMN assigned_to INTEGER")
        conn.execute("ALTER TABLE plane_squawks ADD COLUMN worker_acknowledged_at TEXT")
        conn.execute("ALTER TABLE plane_squawks ADD COLUMN worker_acknowledged_by TEXT")
        conn.commit()

    # Marking a squawk repaired now goes through the same request/confirm
    # two-step as a project sub area (see project_section_complete/_confirm
    # in app.py): squawk_repair only requests confirmation, and an Inspector
    # or admin has to actually confirm it via squawk_repair_confirm before
    # repaired_at gets set. "Send back" clears the request without repairing.
    flight_cols_repair_confirm = [r["name"] for r in conn.execute("PRAGMA table_info(flights)").fetchall()]
    if "squawk_repair_confirm_requested_at" not in flight_cols_repair_confirm:
        conn.execute("ALTER TABLE flights ADD COLUMN squawk_repair_confirm_requested_at TEXT")
        conn.execute("ALTER TABLE flights ADD COLUMN squawk_repair_confirm_requested_by TEXT")
        conn.commit()
    quick_cols_repair_confirm = [r["name"] for r in conn.execute("PRAGMA table_info(plane_squawks)").fetchall()]
    if "repair_confirm_requested_at" not in quick_cols_repair_confirm:
        conn.execute("ALTER TABLE plane_squawks ADD COLUMN repair_confirm_requested_at TEXT")
        conn.execute("ALTER TABLE plane_squawks ADD COLUMN repair_confirm_requested_by TEXT")
        conn.commit()

    # A student's own plane, for the Schedule a Flight "Student's own plane"
    # toggle (see _get_or_create_own_plane_asset in flight.py) - a real
    # assets row (so everything that already joins on asset_id just works)
    # but flagged so it never shows in the Fleet, Maintenance, or the normal
    # plane picker until someone promotes it via asset_detail.html.
    asset_cols_owner = [r["name"] for r in conn.execute("PRAGMA table_info(assets)").fetchall()]
    if "is_owner_placeholder" not in asset_cols_owner:
        conn.execute("ALTER TABLE assets ADD COLUMN is_owner_placeholder INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE assets ADD COLUMN owner_student_id INTEGER REFERENCES students(id)")
        conn.commit()

    # QA finding qa-part-delete-history: a part with real usage history
    # (transactions on a project) can no longer be deleted outright - see
    # part_delete/part_retire in app.py - retired_at hides it from the parts
    # list and scan lookups while keeping every past transaction intact.
    part_cols_retire = [r["name"] for r in conn.execute("PRAGMA table_info(parts)").fetchall()]
    if "retired_at" not in part_cols_retire:
        conn.execute("ALTER TABLE parts ADD COLUMN retired_at TEXT")
        conn.commit()

    # Pi health alerts (temperature/throttling) - written by pi_health.py, a
    # standalone cron script that runs every 5 minutes independent of this
    # app, so it keeps alerting even if OpsHub itself is stuck. Shown as a
    # dashboard banner to shop admins (see dashboard()) until acknowledged.
    # created_at/resolved_at hold LOCAL time here (db.now_iso()), because the
    # dashboard banner prints created_at straight out with no conversion. Pass
    # them explicitly, as pi_health.py and app.py's Acknowledge both do - the
    # DEFAULT below is datetime('now'), which is UTC, and an existing database
    # keeps that default whatever this CREATE says, so relying on it would
    # store a timestamp hours off on any machine that isn't on UTC.
    conn.execute("""CREATE TABLE IF NOT EXISTS system_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message TEXT NOT NULL,
        level TEXT NOT NULL DEFAULT 'warning',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        resolved_at TEXT
    )""")
    conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_system_alerts_unresolved ON system_alerts(resolved_at)")
    conn.commit()

    # Payroll (see payroll.py): one row per person per week an admin marked
    # paid, with the amount/hours owed at that moment. person_type is
    # 'laborer' (laborers.id) or 'cfi' (cfis.id); week_start is a Monday.
    conn.execute("""CREATE TABLE IF NOT EXISTS payroll_payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        person_type TEXT NOT NULL,
        person_id INTEGER NOT NULL,
        week_start TEXT NOT NULL,
        hours REAL NOT NULL DEFAULT 0,
        amount REAL NOT NULL DEFAULT 0,
        paid_at TEXT NOT NULL,
        paid_by TEXT,
        note TEXT,
        UNIQUE(person_type, person_id, week_start)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_payroll_payments_week ON payroll_payments(week_start)")
    conn.commit()

    # Found items / owner approvals (see found_item_* routes in app.py and
    # customer.customer_found_item_decide): something extra a tech found on
    # a job, priced, sent to the plane's owner on the customer portal to
    # approve or decline. Photos live in the photos table (found_item_id).
    conn.execute("""CREATE TABLE IF NOT EXISTS found_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER REFERENCES projects(id), -- NULL once that project is permanently deleted; asset_id keeps this on the plane's record either way
        asset_id INTEGER REFERENCES assets(id),
        description TEXT NOT NULL,
        est_parts REAL NOT NULL DEFAULT 0,
        est_labor_hours REAL NOT NULL DEFAULT 0,
        est_labor_rate REAL NOT NULL DEFAULT 0,
        est_total REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'waiting',
        created_by TEXT,
        created_at TEXT NOT NULL,
        notified_at TEXT,
        decided_at TEXT,
        decided_by TEXT,
        decision_note TEXT,
        section_name TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_found_items_project ON found_items(project_id)")
    photo_cols_found = [r["name"] for r in conn.execute("PRAGMA table_info(photos)").fetchall()]
    if "found_item_id" not in photo_cols_found:
        conn.execute("ALTER TABLE photos ADD COLUMN found_item_id INTEGER REFERENCES found_items(id)")
    conn.commit()

    # QA fix qa-project-purge-crash: found_items.project_id used to be
    # NOT NULL, so permanently deleting a job with an extra-repair item on
    # it hit a FOREIGN KEY constraint (a 500 page) instead of deleting the
    # job - and stopped Empty Trash from finishing too. SQLite can't drop a
    # NOT NULL in place, so this rebuilds the table (once) when it's still
    # the old, stricter shape, adding asset_id at the same time so an item
    # stays on the plane's record (found by asset_id) after its job is gone.
    fi_cols = conn.execute("PRAGMA table_info(found_items)").fetchall()
    fi_project_id_col = next((c for c in fi_cols if c["name"] == "project_id"), None)
    fi_has_asset_id = any(c["name"] == "asset_id" for c in fi_cols)
    if (fi_project_id_col is not None and fi_project_id_col["notnull"]) or not fi_has_asset_id:
        # photos.found_item_id (and found_item_messages) reference
        # found_items, so DROP TABLE found_items - an implicit delete of
        # every row - fails with "FOREIGN KEY constraint failed" once any
        # photo is attached to a found item, and the app won't start.
        # Foreign keys have to be off for the swap (SQLite's documented
        # table-rebuild recipe), and PRAGMA foreign_keys is silently ignored
        # inside an open transaction, so commit first. The ids are copied
        # unchanged, so every reference still points at the right row after.
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("DROP TABLE IF EXISTS found_items_new")
            conn.execute("""CREATE TABLE found_items_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER REFERENCES projects(id),
                asset_id INTEGER REFERENCES assets(id),
                description TEXT NOT NULL,
                est_parts REAL NOT NULL DEFAULT 0,
                est_labor_hours REAL NOT NULL DEFAULT 0,
                est_labor_rate REAL NOT NULL DEFAULT 0,
                est_total REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'waiting',
                created_by TEXT,
                created_at TEXT NOT NULL,
                notified_at TEXT,
                decided_at TEXT,
                decided_by TEXT,
                decision_note TEXT,
                section_name TEXT
            )""")
            old_cols = [c["name"] for c in fi_cols]
            asset_id_expr = "asset_id" if "asset_id" in old_cols else "(SELECT asset_id FROM projects WHERE projects.id = found_items.project_id)"
            conn.execute(f"""INSERT INTO found_items_new
                (id, project_id, asset_id, description, est_parts, est_labor_hours, est_labor_rate,
                 est_total, status, created_by, created_at, notified_at, decided_at, decided_by, decision_note, section_name)
                SELECT id, project_id, {asset_id_expr}, description, est_parts, est_labor_hours, est_labor_rate,
                 est_total, status, created_by, created_at, notified_at, decided_at, decided_by, decision_note, section_name
                FROM found_items""")
            conn.execute("DROP TABLE found_items")
            conn.execute("ALTER TABLE found_items_new RENAME TO found_items")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_found_items_project ON found_items(project_id)")
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

    # Detach (never delete) a purged job's logbook entries and found items
    # from it so a job purged before this fix - already permanently deleted
    # but left with dangling references - doesn't leave orphaned rows
    # pointing at a project id that no longer exists.
    conn.execute("UPDATE found_items SET project_id = NULL WHERE project_id IS NOT NULL "
                 "AND project_id NOT IN (SELECT id FROM projects)")
    conn.execute("UPDATE logbook_entries SET project_id = NULL WHERE project_id IS NOT NULL "
                 "AND project_id NOT IN (SELECT id FROM projects)")
    conn.commit()

    # Found item conversation (idea "New feature: Owners approve extra
    # repairs" revision 2): a back-and-forth note thread on one found item,
    # shop and owner both post to it. author_type is 'shop' or 'owner'.
    # Photos on a message live in the photos table (found_item_message_id),
    # same pattern as a found item's own photos.
    conn.execute("""CREATE TABLE IF NOT EXISTS found_item_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        found_item_id INTEGER NOT NULL REFERENCES found_items(id),
        author_type TEXT NOT NULL,
        author_name TEXT,
        body TEXT,
        created_at TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_found_item_messages_item ON found_item_messages(found_item_id)")
    photo_cols_found2 = [r["name"] for r in conn.execute("PRAGMA table_info(photos)").fetchall()]
    if "found_item_message_id" not in photo_cols_found2:
        conn.execute("ALTER TABLE photos ADD COLUMN found_item_message_id INTEGER REFERENCES found_item_messages(id)")
    conn.commit()

    # Cancellation waitlist (see waitlist_* in flight.py). days: comma list
    # of weekday numbers (0 = Monday); periods: morning/afternoon/evening.
    conn.execute("""CREATE TABLE IF NOT EXISTS flight_waitlist (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL REFERENCES students(id),
        days TEXT NOT NULL,
        periods TEXT NOT NULL,
        asset_id INTEGER REFERENCES assets(id),
        cfi_id INTEGER REFERENCES cfis(id),
        until_date TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        created_by TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS waitlist_offers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        waitlist_id INTEGER REFERENCES flight_waitlist(id),
        student_id INTEGER NOT NULL REFERENCES students(id),
        cancelled_flight_id INTEGER REFERENCES scheduled_flights(id),
        asset_id INTEGER NOT NULL REFERENCES assets(id),
        cfi_id INTEGER REFERENCES cfis(id),
        scheduled_date TEXT NOT NULL,
        scheduled_time TEXT NOT NULL,
        duration_hours REAL,
        created_at TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_waitlist_offers_student ON waitlist_offers(student_id)")
    # Weather cancellation: the 3 new times offered to a student whose lesson was
    # called off for weather (flight.weather_cancel). One tap books one of them.
    conn.execute("""CREATE TABLE IF NOT EXISTS weather_offers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cancelled_flight_id INTEGER NOT NULL REFERENCES scheduled_flights(id),
        student_id INTEGER NOT NULL REFERENCES students(id),
        scheduled_date TEXT NOT NULL,
        scheduled_time TEXT NOT NULL,
        taken_at TEXT,
        created_at TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_weather_offers_flight ON weather_offers(cancelled_flight_id)")
    conn.commit()

    # Exchange cores (QA feat-core-return-tracker): an order for an exchange
    # unit owes the old part back to the supplier by a deadline, or a core
    # charge is billed. core_due_date is set when the order is received.
    order_cols_core = [r["name"] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
    for col, decl in (("is_exchange", "INTEGER NOT NULL DEFAULT 0"), ("core_charge", "REAL"),
                      ("core_days", "INTEGER"), ("core_due_date", "TEXT"), ("core_shipped_at", "TEXT"),
                      ("core_tracking", "TEXT"), ("core_credited_at", "TEXT")):
        if col not in order_cols_core:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {decl}")
    conn.commit()

    # AD compliance (see ads.py). A recurring AD links to the maintenance
    # item that tracks when it's next due.
    conn.execute("""CREATE TABLE IF NOT EXISTS ads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset_id INTEGER NOT NULL REFERENCES assets(id),
        ad_number TEXT NOT NULL,
        subject TEXT,
        kind TEXT NOT NULL,
        interval_hours REAL,
        interval_days INTEGER,
        na_reason TEXT,
        maintenance_item_id INTEGER REFERENCES maintenance_items(id),
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        created_by TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ads_asset ON ads(asset_id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS ad_compliance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ad_id INTEGER NOT NULL REFERENCES ads(id),
        complied_date TEXT NOT NULL,
        tach_hours REAL,
        method TEXT NOT NULL,
        signed_by TEXT,
        note TEXT,
        photo TEXT,
        created_at TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ad_compliance_ad ON ad_compliance(ad_id)")
    conn.commit()

    # Grounding a plane for maintenance (flight.plane_ground): blocks new
    # bookings on it until it's returned to service.
    asset_cols_ground = [r["name"] for r in conn.execute("PRAGMA table_info(assets)").fetchall()]
    for col in ("grounded_at", "grounded_by", "grounded_reason"):
        if col not in asset_cols_ground:
            conn.execute(f"ALTER TABLE assets ADD COLUMN {col} TEXT")
    conn.commit()

    # Optional shelf-life expiration date per part (QA feat-shelf-life-expiry).
    part_cols_exp = [r["name"] for r in conn.execute("PRAGMA table_info(parts)").fetchall()]
    if "expiration_date" not in part_cols_exp:
        conn.execute("ALTER TABLE parts ADD COLUMN expiration_date TEXT")
    conn.commit()

    # Shipment tracking on orders (see tracking.py): the number/carrier typed
    # on New/Edit Order, plus the last status fetched for it, cached so the
    # Orders page doesn't ask the carrier on every load.
    order_cols_tracking = [r["name"] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
    for col in ("tracking_number", "tracking_carrier", "tracking_status", "tracking_detail",
                "tracking_location", "tracking_eta", "tracking_events", "tracking_checked_at",
                "tracking_delivered_at"):
        if col not in order_cols_tracking:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} TEXT")
    conn.commit()

    # One person, several roles (idea "Admin"): a shop worker badge can be
    # linked to a login account, the same way a CFI profile already is
    # (cfis.user_id), so Payroll can show one person's pay across roles.
    # One order, several items and several packages (idea "new order"):
    # every order line carries a batch_id shared by the lines entered
    # together on one New Order, and tracking numbers live per batch in
    # order_shipments (each with its own cached carrier status) instead of
    # one number per line. Existing orders each become their own batch and
    # keep their one tracking number (and its cached status).
    order_cols_batch = [r["name"] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
    if "batch_id" not in order_cols_batch:
        conn.execute("ALTER TABLE orders ADD COLUMN batch_id TEXT")
    conn.execute("UPDATE orders SET batch_id = 'o' || id WHERE batch_id IS NULL OR batch_id = ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_batch ON orders(batch_id)")

    # Part requests from a job (idea "Techs ask for a part right from the
    # job"): a "things to order" entry can say which job it is for and who
    # asked, and is stamped when the part arrives so the asker is told. An
    # order line made from the entry remembers it (orders.wishlist_id) so
    # receiving the order can close the loop.
    wish_cols = [r["name"] for r in conn.execute("PRAGMA table_info(order_wishlist)").fetchall()]
    for col, decl in (("project_id", "INTEGER"), ("requested_by_id", "INTEGER"),
                      ("arrived_at", "TEXT"), ("arrival_seen_at", "TEXT")):
        if col not in wish_cols:
            conn.execute(f"ALTER TABLE order_wishlist ADD COLUMN {col} {decl}")
    order_cols_wish = [r["name"] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
    if "wishlist_id" not in order_cols_wish:
        conn.execute("ALTER TABLE orders ADD COLUMN wishlist_id INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_order_wishlist_project ON order_wishlist(project_id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS order_shipments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT NOT NULL,
        tracking_number TEXT NOT NULL,
        tracking_carrier TEXT,
        tracking_status TEXT,
        tracking_detail TEXT,
        tracking_location TEXT,
        tracking_eta TEXT,
        tracking_events TEXT,
        tracking_checked_at TEXT,
        tracking_delivered_at TEXT,
        created_at TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_order_shipments_batch ON order_shipments(batch_id)")
    # Login lockout: one row per account name (lowercased username/email) that has
    # wrong passwords on record, plus a log of failed attempts and lockouts.
    conn.execute("""CREATE TABLE IF NOT EXISTS login_lockouts (
        account TEXT PRIMARY KEY,
        fails INTEGER NOT NULL DEFAULT 0,
        lockouts INTEGER NOT NULL DEFAULT 0,
        locked_until TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS login_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        username TEXT,
        source TEXT,
        kind TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_source ON login_attempts(source, at)")
    # Each package can be marked received or cancelled on its own (NULL = still open).
    ship_cols = [r["name"] for r in conn.execute("PRAGMA table_info(order_shipments)").fetchall()]
    if "status" not in ship_cols:
        conn.execute("ALTER TABLE order_shipments ADD COLUMN status TEXT")
    conn.execute("""INSERT INTO order_shipments (batch_id, tracking_number, tracking_carrier, tracking_status,
                        tracking_detail, tracking_location, tracking_eta, tracking_events, tracking_checked_at,
                        tracking_delivered_at, created_at)
                    SELECT o.batch_id, o.tracking_number, o.tracking_carrier, o.tracking_status, o.tracking_detail,
                           o.tracking_location, o.tracking_eta, o.tracking_events, o.tracking_checked_at,
                           o.tracking_delivered_at, COALESCE(o.created_at, datetime('now'))
                    FROM orders o
                    WHERE o.tracking_number IS NOT NULL AND TRIM(o.tracking_number) != ''
                      AND NOT EXISTS (SELECT 1 FROM order_shipments s
                                      WHERE s.batch_id = o.batch_id AND s.tracking_number = o.tracking_number)""")
    conn.commit()

    laborer_cols_user = [r["name"] for r in conn.execute("PRAGMA table_info(laborers)").fetchall()]
    if "user_id" not in laborer_cols_user:
        conn.execute("ALTER TABLE laborers ADD COLUMN user_id INTEGER REFERENCES users(id)")
    conn.commit()

    # Manual schedule display order for planes/sims (idea "Move AATD Redbird"):
    # ORDER BY tag alone can't put a plane in an arbitrary spot, so give every
    # flight asset an explicit rank and use it (falling back to tag) everywhere
    # the Schedule/Planes legend orders planes.
    asset_cols_sched_order = [r["name"] for r in conn.execute("PRAGMA table_info(assets)").fetchall()]
    if "schedule_order" not in asset_cols_sched_order:
        conn.execute("ALTER TABLE assets ADD COLUMN schedule_order INTEGER")
        conn.commit()
        flight_assets = conn.execute(
            "SELECT id, tag FROM assets WHERE deleted_at IS NULL AND is_flight_asset = 1 ORDER BY tag").fetchall()
        for i, row in enumerate(flight_assets):
            conn.execute("UPDATE assets SET schedule_order = ? WHERE id = ?", (i * 10, row["id"]))
        conn.commit()
        # Frank's specific request: put the AATD Redbird simulator between
        # N22689 and N5569P on every schedule view.
        n22689 = conn.execute("SELECT id, schedule_order FROM assets WHERE tag = 'N22689'").fetchone()
        n5569p = conn.execute("SELECT id, schedule_order FROM assets WHERE tag = 'N5569P'").fetchone()
        redbird = conn.execute(
            "SELECT id FROM assets WHERE is_simulator = 1 AND (tag LIKE '%Redbird%' OR name LIKE '%Redbird%')").fetchone()
        if redbird and n22689 and n5569p:
            lo, hi = sorted((n22689["schedule_order"], n5569p["schedule_order"]))
            if hi - lo >= 2:
                new_order = (lo + hi) // 2
            else:
                conn.execute("UPDATE assets SET schedule_order = schedule_order + 10 WHERE schedule_order > ?", (lo,))
                new_order = lo + 5
            conn.execute("UPDATE assets SET schedule_order = ? WHERE id = ?", (new_order, redbird["id"]))
            conn.commit()

    # Idea "Schedule conflict": the times a CFI/admin proposed instead, when
    # denying a conflicting student request (see schedule_deny in flight.py) -
    # a JSON list of display strings like ["8:00a", "9:30a"], so the
    # student's "Your Requests" card can offer them as one-click buttons
    # instead of the student having to retype a new request by hand.
    sched_cols_proposed = [r["name"] for r in conn.execute("PRAGMA table_info(scheduled_flights)").fetchall()]
    if "proposed_times" not in sched_cols_proposed:
        conn.execute("ALTER TABLE scheduled_flights ADD COLUMN proposed_times TEXT")
        conn.commit()

    # Idea "remove student from maintenance role, add apprentice": shop_role
    # 'student' renamed to 'apprentice' (Maintenance Role dropdown only -
    # flight_role's own 'student' is a separate column, untouched).
    conn.execute("UPDATE users SET shop_role = 'apprentice' WHERE shop_role = 'student'")
    conn.commit()

    # QA fix qa-labor-double-clock-in: a worker can never have two open
    # timers - before the unique index below can be added, close out any
    # duplicate open sessions a past double-scan already left running (keep
    # the newest, end each older one right when the next one started, same
    # math as a normal clock-out).
    dupe_laborers = conn.execute(
        "SELECT laborer_id FROM labor_sessions WHERE ended_at IS NULL GROUP BY laborer_id HAVING COUNT(*) > 1"
    ).fetchall()
    for row in dupe_laborers:
        open_sessions = conn.execute(
            "SELECT * FROM labor_sessions WHERE laborer_id = ? AND ended_at IS NULL ORDER BY started_at",
            (row["laborer_id"],)).fetchall()
        for i, sess in enumerate(open_sessions[:-1]):
            close_at = open_sessions[i + 1]["started_at"]
            started = datetime.strptime(sess["started_at"], "%Y-%m-%d %H:%M:%S")
            ended = datetime.strptime(close_at, "%Y-%m-%d %H:%M:%S")
            hours = max((ended - started).total_seconds() / 3600.0, 0)
            conn.execute("UPDATE labor_sessions SET ended_at = ?, hours = ?, cost = ? WHERE id = ?",
                         (close_at, hours, hours * (sess["rate"] or 0), sess["id"]))
    conn.commit()
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_labor_sessions_one_open "
                 "ON labor_sessions(laborer_id) WHERE ended_at IS NULL")
    conn.commit()

    # QA finding ux-squawk-my-list-dashboard: an Inspector's "Send Back" on a
    # squawk repair now carries an optional note, so the tech sees why it
    # came back instead of just watching the button revert (see
    # squawk_repair_confirm in app.py). Cleared the next time the tech sends
    # it back to the Inspector, so it never shows stale.
    flight_cols_sent_back = [r["name"] for r in conn.execute("PRAGMA table_info(flights)").fetchall()]
    if "squawk_sent_back_note" not in flight_cols_sent_back:
        conn.execute("ALTER TABLE flights ADD COLUMN squawk_sent_back_note TEXT")
        conn.commit()
    quick_cols_sent_back = [r["name"] for r in conn.execute("PRAGMA table_info(plane_squawks)").fetchall()]
    if "sent_back_note" not in quick_cols_sent_back:
        conn.execute("ALTER TABLE plane_squawks ADD COLUMN sent_back_note TEXT")
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


# ---------------------------------------------------------------------------
# One account, several roles. Admin > Accounts ticks each role separately, so
# someone can be e.g. a shop Admin AND an Inspector, or a CFI AND a Student.
# users.shop_roles/flight_roles hold every role ticked (comma-separated);
# users.shop_role/flight_role hold the main one - the first of the ticked
# roles in the orders below - which is what every existing permission check
# reads. The other roles are used through the "View as" chips, which switch
# the session to that role for real (not read-only).
# ---------------------------------------------------------------------------
SHOP_ROLE_ORDER = ("admin", "tech", "inspector", "apprentice")
FLIGHT_ROLE_ORDER = ("cfi", "student")


def _row_get(row, key):
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _roles_of(row, list_key, main_key, order):
    have = {r.strip() for r in (_row_get(row, list_key) or "").split(",") if r.strip()}
    if _row_get(row, main_key):
        have.add(_row_get(row, main_key))
    return [r for r in order if r in have]


def user_shop_roles(user_row):
    """Every Maintenance role this account holds, main role first."""
    return _roles_of(user_row, "shop_roles", "shop_role", SHOP_ROLE_ORDER)


def user_flight_roles(user_row):
    """Every Flight School role this account holds, main role first."""
    return _roles_of(user_row, "flight_roles", "flight_role", FLIGHT_ROLE_ORDER)


def clean_roles(values, order):
    """Ticked role checkboxes -> (comma list or None, main role or None)."""
    picked = [r for r in order if r in set(values or [])]
    return (",".join(picked) or None), (picked[0] if picked else None)


def ensure_flight_profile(conn, user_row):
    """Make sure a user with flight_role set has the matching cfis/students
    profile row to hold their rate info, creating an empty one if needed
    (e.g. an admin just granted someone CFI access from the accounts page).

    Idea "shop admin skip launcher": a shop admin (not master) with no
    Flight School Role of their own also gets a cfis row - is_station=1
    (never offered as a bookable instructor, no pay-rate or listing
    anywhere real CFIs show up) and no pay rate, the same "instructor
    without billing" view any unbilled CFI gets. Setting a real Flight
    School Role for them on the accounts page overrides this, same as
    for anyone else."""
    flight_roles = user_flight_roles(user_row)
    if "cfi" in flight_roles:
        row = conn.execute("SELECT id FROM cfis WHERE user_id = ?", (user_row["id"],)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO cfis (name, username, password_hash, rate_per_hour, active, user_id, created_at) "
                "VALUES (?, ?, ?, 0, ?, ?, ?)",
                (user_row["name"], user_row["username"], user_row["password_hash"], user_row["active"],
                 user_row["id"], now_iso()))
            conn.commit()
    if "student" in flight_roles:
        row = conn.execute("SELECT id FROM students WHERE user_id = ?", (user_row["id"],)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO students (name, username, password_hash, active, user_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_row["name"], user_row["username"], user_row["password_hash"], user_row["active"],
                 user_row["id"], now_iso()))
            conn.commit()
    if not flight_roles and user_row["shop_role"] == "admin" and not user_row["is_master_admin"]:
        row = conn.execute("SELECT id FROM cfis WHERE user_id = ?", (user_row["id"],)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO cfis (name, username, password_hash, rate_per_hour, active, is_station, user_id, created_at) "
                "VALUES (?, ?, ?, 0, ?, 1, ?, ?)",
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


def found_item_messages(conn, found_item_id):
    """The back-and-forth note thread on one found item (shop and owner
    both post here - see found_item_message_new in app.py and
    customer_found_item_message_new in customer.py), oldest first, each
    with its own photos."""
    rows = conn.execute(
        "SELECT * FROM found_item_messages WHERE found_item_id = ? ORDER BY created_at, id",
        (found_item_id,)).fetchall()
    out = []
    for r in rows:
        m = dict(r)
        m["photos"] = [p["filename"] for p in conn.execute(
            "SELECT filename FROM photos WHERE found_item_message_id = ? ORDER BY id", (m["id"],)).fetchall()]
        out.append(m)
    return out
