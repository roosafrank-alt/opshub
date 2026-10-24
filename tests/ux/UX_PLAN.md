# OpsHub daily design review: plan

The daily design review (a scheduled Claude task) reads this file first. It
covers how OpsHub looks and feels on **phones** (techs at the parts counter,
students and CFIs on the flight line, aircraft owners) and on **desktop** (the
office and the shop PC), and turns what it finds into cards in the Idea
Queue's QA findings for Frank to approve.

## Tool

```bash
python3 tests/ux/ux_audit.py --out /tmp/ux             # every page, every role, ~4 min
python3 tests/ux/ux_audit.py --pages /scan --roles tech # focus on one area
```

It runs the real app on a throwaway database with realistic sample data, at
phone size (390px wide, iPhone) and desktop size (1366px), logged in as each
account type. It writes `summary.md`, `report.json`, and full-page
screenshots. The app's CDN libraries (Bootstrap and others) are served from
`tests/ux/cdn-cache/` because CDNs are blocked in the cloud sandbox. If
`ux_audit.py` prints "these CDN files aren't in tests/ux/cdn-cache", add the
missing file from the library's GitHub release, or the pages won't look real.

## Navigation and flow tool

```bash
python3 tests/ux/flow_audit.py --out /tmp/flow      # about 1 minute
```

- `map.md`: taps from the home screen to every page for each account type,
  pages that take 4+ taps, pages nothing links to, and dead ends.
- `clickable.md`: records (planes, projects, parts) shown as plain text where a
  link to their page would help, plus rows of look-alike boxes where only some
  can be tapped (e.g. the shop dashboard's stat boxes).
- `journeys.md` + `journeys/*.png`: real daily jobs from `tests/ux/journeys.py`,
  done in a browser the way a person would, counting taps, page loads, typing
  and scrolling, with a screenshot after every step. "STUCK" means the next step
  couldn't be found, which is usually a flow problem worth a card.

Frank wants the app streamlined: everything in a logical order, and the
shortest, most obvious path to every daily job. Judge flows by:
- Taps vs `target_taps` for each journey. How could it take fewer? Consider a
  shortcut on the dashboard, a direct link from the page people are already on,
  a sensible default (the logged-in person, today's date, the current plane or
  project), or putting the action at the top of the page instead of the bottom.
- Things that look tappable but aren't, or that should be (a tail number, part
  or project shown as plain text, a stat box that goes nowhere).
- Related things kept apart. Would a person on page A usually need B next, and
  is B one tap away?
- Order on the page: the most common action first, especially on a phone.
- Consistency: the same thing is called the same and found in the same place
  on every page.

Grow `journeys.py`: add 1-2 new real daily jobs each day, the most common
first, until every account type's top jobs are covered. Keep the old ones:
they catch a flow that gets worse after a new feature lands.

## Each day

1. **Automatic checks, all pages.** Look for anything serious: a page that
   scrolls sideways, things off the edge of the screen, JavaScript errors,
   broken images. Always confirm on the screenshot before filing. Some flags
   are false alarms, such as hidden inputs behind a custom dropdown.
2. **Deep review of the next focus area** below, 1-2 areas a day. Look at the
   screenshots yourself, phone and desktop, and judge them as the person who
   uses that page. Is the main action obvious and reachable with a thumb? Is
   anything cramped, hard to read in sunlight, or needing too much scrolling
   or typing? Are there too many taps for a daily task? Is it consistent with
   the rest of the app? Does the page look finished with real-length data?
3. File 2-5 of the most valuable suggestions per area as cards, not every
   nitpick. Frank is in active development and wants improvements now, but
   quality beats volume.

## Focus areas (rotation, in priority order)

Tick an area when reviewed and add the date. After the list is done, start
over from the top, and put areas that changed a lot since their last review
(check `git log` on origin/main and idea-queue) first.

- [x] Daily-job flows (flow_audit journeys), reviewed 2026-09-26: filed ux-assign-part-flow (12 taps + 4 fields vs ~5), ux-log-flight-scroll (7 taps + 1,546px scroll). Added journeys scan-out and log-flight.
- [x] Scan page, phone (tech), reviewed 2026-09-26: the page itself is quick (operator pre-filled, project then part scans need no extra taps); filed ux-scan-button-shop-dashboard (Scan only in the ☰ menu, 5 taps vs 3).
- [ ] Flight School dashboard, phone (CFI and student) (clock/sun-times wrap filed 2026-09-26 as ux-flight-dash-sun-times; still needs a full review)
- [ ] Shop dashboard, phone and desktop
- [ ] Top menu / navigation on every screen size (shop admin menu overflow at 1366px FIXED on idea-queue 2026-09-26; two home tiles both titled "Winds Aloft"; flight-only accounts hit a redirect loop on shop pages, filed ux-shop-link-redirect-loop 2026-09-26)
- [ ] Shop dashboard stat boxes: Active Projects and Low Stock Items look tappable but aren't (found 2026-09-25)
- [ ] Log a flight / active flight, phone (CFI)
- [ ] Schedule and calendar, phone and desktop
- [ ] Projects list and project detail (desktop, office)
- [ ] Parts list and part detail (phone and desktop)
- [ ] Orders (desktop)
- [ ] Aircraft pages, squawks, maintenance (phone and desktop)
- [ ] Customer portal (aircraft owner, phone)
- [ ] Login page and home launcher (phone)
- [ ] Ground school / Academy (student, phone)
- [ ] Billing and pay pages (desktop)
- [ ] Admin pages: users, notifications, system (desktop)

## Tool notes

- 2026-09-26: `scan("CODE")` walker step added (a USB scanner: types into the
  scan box and presses Enter, counted as 1 tap). The sample data now marks
  N81PA and N2231Q as Flight School planes (they show on Schedule / Log a
  Flight); N4729K stays a customer's plane. `summary.md` now lists pages that
  didn't load first, and calls out redirect loops.
- tests/harness.py's fake command runner now returns text (not bytes) when the
  app asks for text, so /admin/system no longer crashes in tests (it was a test
  artifact; the real Pi is fine).

## Card rules (qa_findings)

- kind `layout`: something broken on screen, such as sideways scrolling,
  cut-off controls, overlap, or unreadable text. Severity high if it blocks a
  daily task, otherwise medium.
- Flow findings (too many taps, missing link, wrong order, dead end) are kind
  `improvement` with area "Navigation & flow". Put the before/after tap count
  in `problem`, for example "12 taps + 4 fields today; about 5 with this
  change", and attach the journey's step screenshots that show the detour.
- kind `improvement`: a suggestion that would make a page easier or faster to
  use. Severity by how often the page is used: high for Scan, Flight
  dashboard, and Log flight; medium for other daily pages; low for admin.
- Always attach 1-2 screenshots cropped to the relevant part, and set `page`
  (the URL path) and `screen` ("phone", "desktop", or "both").
- `suggestedFix` describes the change in plain words: what the page will look
  like and do afterwards.
- Never re-file a dismissed card. Update an existing "new" card instead of
  duplicating it.
