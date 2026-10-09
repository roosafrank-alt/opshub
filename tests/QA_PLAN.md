# OpsHub QA plan

This is the working brief for the nightly QA agent and the log of what it has
found. It is read at the start of every nightly run and updated at the end.

## How to run the tests

```bash
python3 -m unittest discover -s tests -v    # everything, ~15 seconds
python3 -m unittest tests.test_inventory_flows -v
```

Tests use a throwaway database in a temp folder, never `instance/shopinv.db`.
The faked shell commands return empty output as text or bytes, whichever the
caller asked for (`text=True` etc.), exactly like the real `subprocess`.
Internet, email, SMS, push, the label printer, and reboot/restart are all faked.
They're safe to run on the Mac, the Pi (even while the live app is running), or in
a cloud session. They need only the standard library plus Flask.

## How findings get to Frank (approval first)

Nothing in the app gets fixed until Frank approves it.

1. The nightly QA agent writes tests and runs them. It **never changes app code**.
2. Each problem it finds becomes a card under **QA findings** at the top of the
   Idea Queue page (collection `qa_findings` in that page's database). Each card
   has the problem, why it matters, how it was found, and the suggested fix, or
   the options when it's a judgment call.
3. Frank presses **Approve** (with an optional note or chosen option) or **Dismiss**.
4. Approve turns the card into a normal queued idea. The Idea Queue runner makes
   the fix on `idea-queue`. It removes that finding's `@open_finding(...)`
   decorator in the same change and runs the tests. Then the idea goes through
   the usual Not deployed → Deploy to Pi → Verify steps.

The tests themselves live on branch **`qa-tests`**, which holds only files under
`tests/` and never app code. Tests for reported, unfixed bugs are marked
`@open_finding("qa-...")`, so the suite stays green on today's code and each one
points to its card.

## Nightly QA agent: rules

1. Push **only** to `qa-tests`, and only files under `tests/`. Never push to
   `main` or `idea-queue`, never merge, never open PRs, never change app code.
2. Never touch the Pi, the live database, or secrets.
3. Health check: run the suite against `origin/main` and against
   `origin/idea-queue`, using this branch's `tests/` folder copied over each one.
   - If idea-queue breaks a test that passes on main, file a high-severity
     finding (kind `regression`) naming the queued idea or commit that caused it.
   - An "unexpected success" on main means a fix went live: remove that
     `@open_finding` decorator on `qa-tests` and set the card's status to `fixed`.
4. Cover the next unchecked area in the backlog below. Cover the happy path,
   every rejection, each role, double-submits, missing records (404 not 500),
   NaN/negative/blank input, and totals that must match their records.
5. For every problem found: write the test, mark it `@open_finding("qa-<slug>")`,
   and file one card. Never re-file a finding whose card was dismissed. Update
   the existing card instead of duplicating it.
6. Cards are written for Frank, not a developer: plain language, no code.
7. Tick the area in the backlog and commit to `qa-tests`.

## Backlog: flows to cover (in priority order)

Shop / inventory
- [x] Scan in/out (`/api/scan`): validation, stock limits, projects, permissions
- [x] Parts: create, edit, recount, delete
- [x] Project page "Add Part"
- [x] Orders: receive, cancel, double-receive
- [x] Ledger invariant: on-hand always matches transaction history (randomized)
- [x] Orders: new, edit, wishlist, export CSV (tech sees costs?)
- [x] Labor tracking: `/api/labor/scan` start/stop, double-start, stop someone else's session, pay totals
- [ ] Labor: two scans of the same badge at the same instant (needs a threaded test; today's check-then-insert isn't atomic)
- [ ] Pi health / System page (`/admin/system`, `pi_health.py`) once it's live: temperature parsing, alert thresholds, no duplicate alerts
- [x] Project lifecycle: status, trash/restore/purge, renumber after delete (new/intake/edit still to do)
- [x] Project sub-areas: add, rename (history follows), complete, then inspector confirm or send back
- [x] Squawks: new, acknowledge, assign, repair, worker acknowledge (both `kind`s), inspector confirm / send back, fix on job
- [x] Assets: new, quick new, edit, hours update, trash/restore/purge (open: qa-asset-new-crash, qa-asset-purge-crash, qa-asset-hours-bad-values; still undecided: should a lower Hobbs/Tach reading than the last one be allowed?)
- [x] Maintenance items: new, edit, complete, due/overdue math around hours and dates (open: qa-maint-bad-numbers, qa-maint-deleted-item-still-editable)
- [x] Logbook entries: create, edit, print, starter templates (open: qa-logbook-bad-hours; Photos and the rest still to do)
- [x] Photos: upload (bad file types), set cover, delete (all fine; no size limit on uploads, not reported: nothing to compare against)
- [ ] Trash page: empty trash, restore conflicts
- [ ] Shop billing, shop pay, and stats totals match the underlying transactions and labor

Accounts / admin
- [x] Login form, wrong password, deactivated account
- [x] Customer portal only shows the customer's own aircraft
- [x] Admin users: create, edit, deactivate, role changes take effect on next request (all fine except open: qa-user-password-stored-readable; plan said "next request" - verified for role, admin rights and deactivation)
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

Live list: the **QA findings** section of the Idea Queue page. Finding IDs in
the tests (for example `qa-nan-quantities`) match the card IDs there. A tested
fix for each of the first night's bugs was already written on branch
`qa/2026-09-25` (closed PR #1), and the runner may reuse it once a card is
approved.
