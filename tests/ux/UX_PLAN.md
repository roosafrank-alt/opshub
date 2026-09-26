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

- [ ] Scan page, phone (tech): the most-used screen in the shop
- [ ] Flight School dashboard, phone (CFI and student)
- [ ] Shop dashboard, phone and desktop
- [ ] Top menu / navigation on every screen size (shop admin menu overflows at 1366px, found 2026-09-25)
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

## Card rules (qa_findings)

- kind `layout`: something broken on screen, such as sideways scrolling,
  cut-off controls, overlap, or unreadable text. Severity high if it blocks a
  daily task, otherwise medium.
- kind `improvement`: a suggestion that would make a page easier or faster to
  use. Severity by how often the page is used: high for Scan, Flight
  dashboard, and Log flight; medium for other daily pages; low for admin.
- Always attach 1-2 screenshots cropped to the relevant part, and set `page`
  (the URL path) and `screen` ("phone", "desktop", or "both").
- `suggestedFix` describes the change in plain words: what the page will look
  like and do afterwards.
- Never re-file a dismissed card. Update an existing "new" card instead of
  duplicating it.
