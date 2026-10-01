"""Daily jobs for the flow review to walk through (tests/ux/flow_audit.py).

Each journey is one real task a real person does, starting from the home
screen right after login, done by tapping visible labels the way a person
would. flow_audit counts the taps, page loads, typing and scrolling, and saves
a screenshot after every step.

The daily design reviewer ADDS journeys here as it learns the app. Aim for the
jobs people do most: every day at the parts counter, on the flight line, in
the office. Keep each one to a single goal.

Step helpers (w = the walker):
  w.start("/")            start on a page (after login)
  w.tap("Label")          tap a link or button by its visible text; opens the
                          phone menu or a dropdown first if needed (counts taps)
  w.fill("Label", "val")  type into (or pick from) a field found by its label,
                          placeholder or name
  w.see("Some text")      confirm the goal is on screen (scrolls to it)
  A label starting with # . or [ is a CSS selector, for controls with no
  visible label (e.g. w.tap("#assign-operator-new-add")).

`ids` holds the sample records' ids (see seed_everything in tests/harness.py
and seed_realistic in ux_audit.py). The sample data includes planes N4729K,
N81PA and N2231Q, the projects "Annual Inspection - N4729K" and "Annual -
N12345", and the parts "Oil Filter CH48110-1" and "Oil Filter" (barcode
PART-001).

target_taps: what a streamlined version of this task should take. The
reviewer suggests changes when the real count is well above it.
"""


def _tap_tomorrow(w):
    """Tap tomorrow on the schedule form's month calendar, paging to next month
    first when tomorrow is the 1st (a person has to do that too)."""
    import datetime
    t = datetime.date.today() + datetime.timedelta(days=1)
    if t.day == 1:
        w.tap("#cal-next")
    w.tap(f"#date-calendar [data-date='{t.isoformat()}']")


def _tomorrow():
    import datetime
    return (datetime.date.today() + datetime.timedelta(days=1)).isoformat()


def _sample(sql):
    """Adds one sample record for a journey that needs it (e.g. a lesson
    already in the air). Runs in the same process as the throwaway app, so
    it writes to the test database, never a real one."""
    import db
    conn = db.get_db()
    conn.execute(sql)
    conn.commit()
    conn.close()


JOURNEYS = [
    {
        "id": "squawks",
        "task": "Tech checks the open squawks on plane N4729K",
        "role": "tech", "screen": "phone", "target_taps": 3,
        "steps": lambda w, ids: (
            w.start("/"),
            # Techs land straight on the shop home (single-program launcher skip, 2026-09-28).
            w.tap("Aircraft"),
            w.tap("N4729K"),
            w.see("Left main tire"),
        ),
    },
    {
        "id": "assign-part",
        "task": "Tech charges 1 oil filter to the N4729K annual",
        "role": "tech", "screen": "phone", "target_taps": 5,
        "steps": lambda w, ids: (
            w.start("/"),
            w.tap("Projects"),
            w.tap("Annual Inspection - N4729K"),
            # Assign Parts is now the wrench next to "Discrepancy List" and opens a pop-up (2026-09-28);
            # qty defaults to 1 and "Scanning as" is filled in for the logged-in tech.
            w.tap("#assign-parts-btn"),
            w.fill("part_id", "Oil Filter CH48110-1"),
            w.tap("Assign to Project"),
            w.see("Assigned 1"),
        ),
    },
    {
        "id": "todays-flights",
        "task": "CFI checks today's flight schedule",
        "role": "cfi", "screen": "phone", "target_taps": 1,
        "steps": lambda w, ids: (
            w.start("/"),
            # CFIs land straight on the flight dashboard; today's list is headed "Today <date>".
            # 2 since 2026-10-01: the sample data also has a lesson booked 20 minutes ago (start-late-lesson).
            w.see("0 of 2 done"),
        ),
    },
    {
        "id": "owner-plane-status",
        "task": "Aircraft owner checks what's due on their plane",
        "role": "customer", "screen": "phone", "target_taps": 2,
        "steps": lambda w, ids: (
            w.start("/"),
            w.tap("My Aircraft"),
            w.tap("N4729K"),
            w.see("100-hour inspection"),
        ),
    },
    {
        "id": "low-stock",
        "task": "Shop admin finds which parts need reordering",
        "role": "shop_admin", "screen": "desktop", "target_taps": 1,
        "steps": lambda w, ids: (
            # Since ux-shop-admin-skip-launcher went live (2026-09-29) a shop admin's login lands
            # straight on the shop home; "/" is now only the program picker behind the grid button.
            w.start("/shop"),
            w.see("Low"),
        ),
    },
    # Added 2026-09-26: the Scan page is the most-used screen in the shop.
    {
        "id": "scan-out",
        "task": "Tech scans an oil filter out to the N4729K annual on the Scan page",
        "role": "tech", "screen": "phone", "target_taps": 4,  # 3 + the USB Scanner tab (test browser has no camera)
        "steps": lambda w, ids: (
            w.start("/"),
            w.tap(".mobile-scan-fab"),  # phones: the round yellow Scan button, bottom right
            # It opens the camera straight away (?cam=1). The test browser has no camera, so switch to
            # the typed box (what a Bluetooth scanner uses); that tap is not counted against target_taps.
            w.tap("USB Scanner"),
            w.scan("26-002"),        # the project's printed code
            w.scan("SHOP-UX0001"),   # Oil Filter CH48110-1
            w.see("Oil Filter CH48110-1"),
        ),
    },
    {
        "id": "log-flight",
        "task": "CFI logs a finished lesson (plane, student, Hobbs) on a phone",
        "role": "cfi", "screen": "phone", "target_taps": 6,
        "steps": lambda w, ids: (
            w.start("/"),
            w.tap("Log a Flight"),
            # "Log a Flight" opens Schedule a Flight with "Flight Already Complete" switched on.
            w.tap("N81PA"),                          # one-tap plane buttons
            w.fill("#student-combo-input", "Student"),
            w.tap("#student-combo-list .list-group-item"),
            w.fill("hobbs_end", "1001.3"),           # Hobbs start fills in from the plane
            w.tap("Session Complete", role="button"),  # renamed from "Flight Complete" (2026-09-28)
            w.see("N81PA"),
        ),
    },
    # Added 2026-09-29: the two flight-line and shop-floor jobs done most often.
    {
        "id": "start-next-lesson",
        "task": "CFI starts the next lesson's session from the dashboard",
        "role": "cfi", "screen": "phone", "target_taps": 1,
        "steps": lambda w, ids: (
            w.start("/"),
            w.tap("Start Session", role="button"),
            w.see("Hobbs"),
        ),
    },
    {
        "id": "report-squawk",
        "task": "Tech reports a squawk on N81PA from a phone",
        "role": "tech", "screen": "phone", "target_taps": 4,
        "steps": lambda w, ids: (
            w.start("/"),
            w.tap("Report a squawk"),
            w.fill("asset_id", "N81PA"),
            w.fill("notes", "Nav light out on the left wing"),
            w.tap("Report", role="button"),
            w.see("Nav light out on the left wing"),
        ),
    },
    # Added 2026-09-30: Frank's first goal is "scheduling in less than a minute", from both sides.
    {
        "id": "request-lesson",
        "task": "Student asks for a lesson in N81PA tomorrow at 2 pm from a phone",
        "role": "flight_student", "screen": "phone", "target_taps": 5,
        "steps": lambda w, ids: (
            w.start("/"),
            w.tap("Request a Flight"),
            w.tap("N81PA"),                       # one-tap plane buttons
            _tap_tomorrow(w),                     # the always-open month calendar under Date
            w.tap("2:00p"),                       # quick-pick time (1.5-hr lesson blocks)
            w.tap("Request Flight", role="button"),
            w.see("N81PA"),
        ),
    },
    {
        "id": "cfi-book-lesson",
        "task": "CFI books a student's next lesson in N81PA for tomorrow at 9:30 am",
        "role": "cfi", "screen": "phone", "target_taps": 6,
        "steps": lambda w, ids: (
            w.start("/"),
            w.tap("Schedule"),
            w.tap("Schedule Flight"),
            w.tap("N81PA"),
            w.fill("#student-combo-input", "Student"),
            w.tap("#student-combo-list .list-group-item"),
            _tap_tomorrow(w),                     # the always-open month calendar under Date
            w.tap("9:30a"),
            w.tap("Schedule Flight", role="button"),
            w.see("Your new booking"),            # lands on the month view with the new booking lit up
        ),
    },
    # Added 2026-10-01 (focus: Log a flight / active flight, Schedule and calendar on phones).
    {
        "id": "end-lesson",
        "task": "CFI ends the lesson that's in the air and logs it as paid (phone)",
        "role": "cfi", "screen": "phone", "target_taps": 6,
        "steps": lambda w, ids: (
            _sample("INSERT INTO flights (cfi_id, student_id, asset_id, flight_date, solo, hobbs_start, tach_start, "
                    "started_at, created_at) VALUES (%d, %d, (SELECT id FROM assets WHERE tag='N2231Q'), date('now'), 0, "
                    "1000.0, 850.0, datetime('now', '-80 minutes'), datetime('now'))" % (ids["cfi"], ids["student"])),
            w.start("/"),
            w.tap("View / End Flights"),          # the yellow 'flight in progress' box on the dashboard
            w.tap("End Session"),
            w.fill("hobbs_end", "1001.2"),        # 1.2 hr on the meter, inside the 1.3 hr on the clock
            w.fill("tach_end", "851.0"),
            w.tap("Paid"),                        # amount fills itself in from the flight total
            w.tap("Session Complete", role="button"),
            w.tap("Yes, log it"),                 # 'Log this session with Hobbs end ...?' pop-up
            w.see("logged"),
        ),
    },
    {
        "id": "cfi-tomorrow",
        "task": "CFI checks who is flying tomorrow on the Schedule (phone)",
        "role": "cfi", "screen": "phone", "target_taps": 2,
        "steps": lambda w, ids: (
            _sample("INSERT INTO scheduled_flights (asset_id, student_id, cfi_id, scheduled_date, scheduled_time, "
                    "duration_hours) VALUES ((SELECT id FROM assets WHERE tag='N2231Q'), %d, %d, date('now', '+1 day'), "
                    "'10:30', 1.5)" % (ids["student"], ids["cfi"])),
            w.start("/"),
            w.tap("Schedule"),                    # opens on Month: 7 thin columns of hour lines on a phone
            w.tap("Day"),                         # Day view, today
            w.tap("[href*='view=day&date=%s'].btn-sm" % _tomorrow()),   # the › next-day arrow
            w.see("10:30a N2231Q"),
        ),
    },
    # Added 2026-10-01 by a second review run (same focus areas): a late start and approving a request.
    {
        "id": "start-late-lesson",
        "task": "CFI starts a lesson booked 20 minutes ago (the student ran late) from the dashboard",
        "role": "cfi", "screen": "phone", "target_taps": 1,
        "steps": lambda w, ids: (
            w.start("/"),
            # Once the booked time has passed, the Next Lesson card only offers "Log this lesson" (the
            # manual log form, no clock). Starting the clock means finding the grey block in Today's
            # list, tapping it open, then its small Start button.
            w.tap(f".flight-chip[data-chip-id='{ids['late_lesson']}']"),
            w.tap("Start", role="button"),
            w.see("Hobbs"),
        ),
    },
    {
        "id": "cfi-approve-request",
        "task": "CFI approves a student's lesson request from the dashboard",
        "role": "cfi", "screen": "phone", "target_taps": 2,
        "steps": lambda w, ids: (
            w.start("/"),
            w.tap("Approve", role="button"),       # Pending Approval card, top of the dashboard
            w.tap("Yes, approve", role="button"),  # confirm pop-up
            w.see("pproved"),
        ),
    },
]
