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

JOURNEYS = [
    {
        "id": "squawks",
        "task": "Tech checks the open squawks on plane N4729K",
        "role": "tech", "screen": "phone", "target_taps": 3,
        "steps": lambda w, ids: (
            w.start("/"),
            w.tap("Parts, projects, labor"),  # the shop tile (a second tile is also titled "Winds Aloft")
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
            w.tap("Parts, projects, labor"),  # the shop tile (a second tile is also titled "Winds Aloft")
            w.tap("Projects"),
            w.tap("Annual Inspection - N4729K"),
            w.fill("part_id", "Oil Filter CH48110-1"),
            w.fill("qty", "1"),
            # "Scanning as" isn't filled in for the logged-in tech: pick "+ Add new name", type, tap Add.
            w.fill("#assign-operator-select", "Add new name"),
            w.fill("#assign-operator-new-input", "Tech"),
            w.tap("#assign-operator-new-add"),
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
            w.tap("Fly with Kate!"),
            w.see("Today's Schedule"),
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
        "role": "shop_admin", "screen": "desktop", "target_taps": 2,
        "steps": lambda w, ids: (
            w.start("/"),
            w.tap("Parts, projects, labor"),  # the shop tile (a second tile is also titled "Winds Aloft")
            w.see("Low"),
        ),
    },
    # Added 2026-09-26: the Scan page is the most-used screen in the shop.
    {
        "id": "scan-out",
        "task": "Tech scans an oil filter out to the N4729K annual on the Scan page",
        "role": "tech", "screen": "phone", "target_taps": 3,
        "steps": lambda w, ids: (
            w.start("/"),
            w.tap("Parts, projects, labor"),
            w.tap("Scan"),
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
            w.tap("Fly with Kate!"),
            w.tap("Log a Past Session"),
            # "Log a Past Session" opens Schedule a Flight with "Log a past session instead of booking" switched on.
            w.tap("N81PA"),                          # one-tap plane buttons
            w.fill("#student-combo-input", "Student"),
            w.tap("#student-combo-list .list-group-item"),
            w.fill("hobbs_end", "1001.3"),           # Hobbs start fills in from the plane
            w.tap("Log Session", role="button"),
            w.see("N81PA"),
        ),
    },
]
