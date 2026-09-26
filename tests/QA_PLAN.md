# OpsHub QA plan

This is the working brief for the nightly QA agent and the log of what it has
found. It is read at the start of every nightly run and updated at the end.

## How to run the tests

```bash
python3 -m unittest discover -s tests -v    # everything, ~15 seconds
python3 -m unittest tests.test_inventory_flows -v
```

Tests use a throwaway database in a temp folder, never `instance/shopinv.db`.
Internet, email, SMS, push, the label printer, and reboot/restart are all faked.
They're safe to run on the Mac, the Pi (even while the live app is running), or in
a cloud session. They need only the standard library plus Flask.

## Nightly QA agent: rules

1. **Never push to `main` or merge anything.** Work on a branch named
   `qa/YYYY-MM-DD` and open a pull request. Frank reviews and merges.
2. **Never touch the Pi, the live database, or secrets.** The tests are the
   whole world.
3. Start every run by running the full suite on a fresh `main`. If `main` is
   red, the PR's first job is to explain why (usually another session's change).
4. Pick the **next unchecked area** from the backlog below. Write flow tests
   for it the way `test_inventory_flows.py` does: drive the real routes, then
   check the database. Cover the happy path, every rejection, permissions by
   role, and double-submits.
5. When a test exposes a bug:
   - **Clear-cut bug** (crash, data corruption, double-counting, a permission
     hole, a missing 404): fix it in the same PR, keeping the change as small as
     possible, with a comment explaining why.
   - **Judgment call** (business rules, who should see what, whether something
     should be allowed at all): don't change app code. Add the test with
     `@unittest.expectedFailure` and a `KNOWN BUG - NEEDS YOUR DECISION` comment,
     and list it under "Waiting on Frank" below.
6. Keep each PR reviewable: one area per night, around 400 changed lines of app
   code at most. Tests can be longer.
7. Update this file: tick the area, add findings, and add any new areas you
   noticed.
8. The PR description is written for Frank, not a developer: say what was
   tested, what broke, what got fixed, and what needs his decision, in plain
   language.

## Backlog: flows to cover (in priority order)

Shop / inventory
- [x] Scan in/out (`/api/scan`): validation, stock limits, projects, permissions
- [x] Parts: create, edit, recount, delete
- [x] Project page "Add Part"
- [x] Orders: receive, cancel, double-receive
- [x] Ledger invariant: on-hand always matches transaction history (randomized)
- [ ] Orders: new, edit, wishlist, export CSV (tech sees costs?)
- [ ] Labor tracking: `/api/labor/scan` start/stop, double-start, stop someone else's session, pay totals
- [ ] Project lifecycle: new, intake, edit, status, trash/restore/purge, renumber after delete
- [ ] Project sub-areas: add, rename (history follows), complete, then inspector confirm or send back
- [ ] Squawks: new, acknowledge, assign, repair, worker acknowledge (both `kind`s)
- [ ] Assets: new, quick new, edit, hours update (can hours go backwards?), trash/restore/purge
- [ ] Maintenance items: new, edit, complete, due/overdue math around hours and dates
- [ ] Logbook entries: create, edit, print, starter templates
- [ ] Photos: upload (bad file types, huge files), set cover, delete
- [ ] Trash page: empty trash, restore conflicts
- [ ] Shop billing, shop pay, and stats totals match the underlying transactions and labor

Accounts / admin
- [x] Login form, wrong password, deactivated account
- [x] Customer portal only shows the customer's own aircraft
- [ ] Admin users: create, edit, deactivate, role changes take effect on next request
- [ ] View As (master admin impersonation): enter, exit, can't escalate
- [ ] Admin reset pages: each reset touches only what it says

Flight school
- [ ] Scheduling: new, edit, conflicts, recurring, CFI time off
- [ ] Flight log: new, early start, active flight, complete, Hobbs/tach gaps
- [ ] Billing: student ledger balances, CFI pay, plane rates
- [ ] Ground school / ACS: sign-offs, progress (the crawler needs seed data for these, they currently 404)
- [ ] Pilot logbook, Academy
- [ ] Notifications and push alerts: correct recipients, no duplicate sends

## Findings

### Fixed (PR #1, 2026-09-25)
- A flight student, a CFI, or any account with no Shop role could add or remove stock through the Scan API.
- A quantity of `nan` or `inf` on scan, Add Part, New Part, or Recount was accepted. It either crashed or wrote NaN into the part's count.
- Receiving the same order twice (a double-click or a resubmit) added the stock twice. A cancelled order could also be received, and a received order could be cancelled.
- A recount could be set to a negative number.
- Editing a part could blank out its name.
- The project page's Add Part could charge parts to a project sitting in Recently Deleted.
- Changing a missing project's status silently did nothing instead of returning a 404.
- A malformed request to the Scan API crashed it instead of returning a 400.
- Any crash in the middle of a save left the database locked, so every other save in the app stalled for up to 30 seconds. Leftover connections are now closed at the end of each request.

### Waiting on Frank
- **Deleting a part erases its history on every project.** Delete Part removes all of that part's transactions, so completed and billed jobs lose those parts and their costs. Suggested fix: block delete once the part has been used on a project, and offer "hide/retire" instead. The test is `test_deleting_a_part_keeps_project_parts_history`.
- **Costs visible to techs outside the UI.** Techs and students don't see prices on screen, but:
  - `/api/lookup/<barcode>` and the Scan API response return the full part, including `unit_cost` and `sell_price`, to any logged-in user.
  - `/projects/parts-used` puts `data-cost` in the page source for every role.
  - `/orders/export` gives techs a CSV with unit cost and total.

  Should techs be able to see costs, or should these be stripped?
- **Accounts with no Shop role can still open some shop pages.** A flight student can load `/shop`, `/scan`, `/projects`, a project's detail page, `/projects/parts-used`, `/calendar`, and `/shop/pay`. Is that intended, for example so students see the shop calendar?
- **Scanning parts out to a completed or archived project is allowed.** Is that intentional, for example to add a part after the fact?
