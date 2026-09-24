# Queued OpsHub changes (base: shopinv_v10)

Each section is one Idea Queue item. Patches are in _patches/.

## 01 - Should not be able to start future flights
**What changed:** A booked flight can now only be started from 30 minutes before its scheduled time onward. Earlier than that, the Start button is greyed out (locked) and the schedule modal shows when it opens ("Start opens today at 9:00 AM") with a hint to reschedule to now if it's actually leaving early. The server also refuses an early start (flash warning, back to the previous page), so an old open page or a double-tap can't get around it. Bookings with no time set can be started any time on their scheduled day, not before. The 30-min buffer is `START_EARLY_BUFFER_MIN` in flight.py.
**Files:** flight.py (new `_start_window` / `_opens_label` helpers, check in `schedule_start`, `can_start` on the CFI dashboard's upcoming list), templates/flight/schedule.html (locked Start button + note in the details modal), templates/flight/_dashboard_live.html (locked Start button in Upcoming).
**Test:** Book a flight 2 hours out. On the Schedule page click it: Start Flight is grey with the "Start opens…" note; on the dashboard Upcoming list the Start button shows a lock. Reschedule it to 20 minutes from now: Start is enabled and works. A flight booked for tomorrow with no time is locked until midnight.
**Patch:** _patches/01-block-early-flight-start.patch

## 02 - Schedule view update (list view day lines + grey past)
**What changed:** In the Schedule List and Custom Range tables, the first row of each new day now gets a heavy dark divider line above it and a bold date, so days are easy to tell apart (on phones, where rows stack as cards, there's a gap and a thick bar above each new day). Flights on past days are greyed out (grey background, muted text, lighter), and today's rows get a light yellow tint with a "Today" badge on the first row. The Day view also greys out if you're looking at a past day. Conflict-highlighted rows are never greyed.
**Files:** templates/flight/_schedule_live.html (schedule_row macro takes a new_day flag; list/custom loops pass it), templates/flight/schedule.html (CSS).
**Test:** Schedule > List (and Custom Range spanning a few days that include past and future flights): heavy line between days, past days grey, today tinted with a Today badge. Check on a phone width too. Live refresh (15s) keeps the styling.
**Patch:** _patches/02-schedule-list-day-lines-grey-past.patch (apply after 01; touches schedule.html too)

## 03 - Schedule view (sticky toolbar)
**What changed:** On the Schedule page the top toolbar - the date/range title, Day/Month/Quarter/Year/List/Custom Range buttons, prev/Today/next, Schedule Flight, and the From/To + Plane/Instructor filters - is now pinned right under the navbar and stays visible while the calendar or list scrolls underneath. It gets a soft shadow once it's floating. The navbar height is measured live, so it still lines up when the navbar wraps or the phone menu opens. On phones it's compacted (smaller title, view buttons on one scrollable line). The red "Showing what's in conflict" banner moved just below the toolbar so it scrolls away normally.
**Files:** templates/flight/_schedule_live.html (toolbar wrapper; conflict alert moved below it), templates/flight/schedule.html (CSS + small navbar-height script).
**Test:** Open Schedule in Month and List views with lots of flights and scroll: the toolbar stays under the navbar, and the filters and Schedule Flight keep working while scrolled. Wait 15s for a live refresh and confirm it's still pinned. Check at phone width, including with the navbar menu open. Clicking a flight still opens the details modal on top.
**Patch:** _patches/03-schedule-sticky-toolbar.patch (apply after 01 and 02)


## 04 - Photos for ID (aircraft photo defaults to project photo)
**What changed:** Aircraft that have no photo of their own now show their newest project's photo (the project's cover photo, or else its newest one) on the Aircraft list and at the top of the aircraft page. The aircraft's Photos card says where it's borrowed from and links to that project. When OpsHub first starts after this deploy, a one-time step also carries over existing photos: every aircraft with no photo gets its own copy of that project photo (a separate file, set as the aircraft's cover). From then on the aircraft photo and the project photo are independent: changing, re-covering or deleting either one doesn't touch the other. The one-time step is recorded in app_settings (`asset_photos_carried_over`, with how many it copied), so deleting an aircraft photo later won't bring it back on restart.
**Files:** db.py (`_carry_over_project_photos_to_assets`, called at the end of `_migrate`), app.py (`_asset_project_cover` fallback used by the Aircraft list and aircraft detail), templates/asset_detail.html (fallback hero image + note).
**Test:** Before deploying, note an aircraft with a photo only on its project. After restart it has its own cover photo on the Aircraft list and detail page. Delete that aircraft photo: the project's photo is still there, and the aircraft falls back to showing it with the "No aircraft photo yet" note. Add a new aircraft photo: it replaces the fallback, and the project photo stays the same. Check `SELECT * FROM app_settings WHERE key='asset_photos_carried_over'` for the copy count.
**Patch:** _patches/04-aircraft-photo-from-project.patch

## 05 - Mobile APP phone alerts (separate checkboxes)
**What changed:** On My Account, the single "Flying session time" line is replaced by a boxed "Flying session phone alerts" group with three separate checkboxes: 30 minutes left, Time's up, and Running late. Each alert is only sent to people who ticked that particular box. Existing users who had the old combined setting on start with all three ticked. The Phone Alerts (this device) section now explains why it can't turn on (not on the https address, or on iPhone not opened from the home-screen icon) instead of sitting on "Checking...", and shows the Enable button after 5 seconds if the phone never answers.
**Files:** db.py (migration adds users.notify_push_30min / notify_push_timeup / notify_push_late, copied from notify_push_session_alerts), schema.sql, app.py (account_page saves the three), flight.py (check_session_alerts uses per-alert opt-ins), templates/account.html.
**Migration:** automatic on restart (new columns added by _migrate). No new packages.
**Test:** My Account: three checkboxes under Reminders. Untick "30 minutes left", Save, reload - it stays unticked, the other two stay ticked. On a phone opened from the home-screen icon, Enable on this device works.
**Patch:** _patches/05-separate-session-alert-checkboxes.patch (apply after 01-04)

## 06 - Calendar View (tidier month/quarter/year blocks)
**What changed:** In the Month, Quarter and Year grids the day number now sits in its own header strip with a thin line under it, and lesson blocks start below it so they never cover the date or the + button. Each block is one tidy line (short time like "9:30a" + plane, with a small icon for in-progress/completed/needs-review) plus a smaller student / instructor line; on phones only the first line shows. The in-progress glow is drawn inside the block instead of spilling over its neighbours. Busy days show the first few lessons (4 in Month, 3 in Quarter, 2 in Year) and a "+N more" link that opens that day's Day view. When the page is showing a conflict, nothing is collapsed.
**Files:** flight.py (`short_time` added in `_decorate_schedule_row`), templates/flight/_schedule_live.html (block layout, day header strip, "+N more"), templates/flight/schedule.html (CSS).
**Test:** Schedule > Month on a day with 5+ lessons: the date header is clear, 4 blocks show plus "+N more", which opens the Day view. Check at phone width. Start a flight: its block glows yellow inside its own border.
**Patch:** _patches/06-calendar-tidy-blocks.patch (apply after 01-05)

## 07 - Back button to return to the previous page
**What changed:** Every OpsHub screen that has a top bar (Winds Aloft shop pages, Fly with Kate!, Admin, Academy) now has a "< Back" button at the far left of the navbar, in the same outlined-box style as the flight nav buttons and at least 40px tall for easy tapping (just the arrow on phones). It goes to the previous page. If there's no previous OpsHub page (opened from the home-screen icon, a notification, or a fresh tab) it goes to that module's home (shop dashboard, flight dashboard, Admin overview; Academy goes to the program picker). On a module's home page with nothing to go back to it hides itself. The launcher has no top bar, so no Back there. The navbar also keeps Back / programs / brand packed on the left with the menu button on the right on phones.
**Files:** templates/_back_button.html (new), templates/base.html, templates/flight/base_flight.html, templates/admin_base.html, templates/academy.html.
**Test:** Launcher > Fly with Kate! > Schedule > tap Back: you're on the flight dashboard. Open the flight dashboard straight from the home-screen icon: no Back button. Open a Parts page from a fresh tab: Back goes to the shop dashboard. Check at phone width.
**Patch:** _patches/07-back-button.patch (apply after 01-06)

## 08 - Logging student landings (last lesson / last landing on the Students tab)
**What changed:** The Students list has three new columns: Last Lesson (date, plane, instructor or solo, how many days ago, and a link to that flight), Last Landing (date of the latest flight with landings entered, how many and how many at night, plus total landings in the last 90 days), and Solo (Current with days left, Checkout due, or No dual yet, using the same currency rule as scheduling). Opening a student (Edit) now starts with a "Lessons & Landings" card: last lesson, last landing, 90-day landings and night full-stops, solo currency, and their last 10 flights with day/night full-stop and touch-and-go counts (tap a row to open the flight). Flights still in the air are not counted until they're ended.
**Files:** flight.py (`_student_activity`, `_landing_total`; students_list and student_edit pass the data), templates/flight/students.html, templates/flight/student_form.html.
**Test:** Log or end a flight for a student with some landings. Students tab: that date shows under Last Lesson and Last Landing with the count. Tap Edit on the student: the Lessons & Landings card lists the flight with its FS/TG counts. Check at phone width.
**Patch:** _patches/08-student-last-lesson-landing.patch (apply after 01-07)

## 09 - Mobile dash view (collapsible sections on phones)
**What changed:** On phone-width screens the Fly with Kate! dashboard's Today's Schedule, Upcoming and Recent Flights cards start collapsed to just their header line: a chevron, the title, and a count (Today keeps its "X of Y complete" badge). Tap the header to open or close it. What you opened stays open through the 15-second live refresh and when you come back to the dashboard in the same tab. The Full Calendar button in the Upcoming header still works as a button. Tablets and computers look the same as before (always open).
**Files:** templates/flight/_dashboard_live.html (header/body wrappers + counts), templates/flight/dashboard.html (CSS + toggle script, re-applied after each live refresh).
**Test:** Open the flight dashboard on a phone: the three sections show only headers. Tap Upcoming: it opens. Wait 20 seconds: it stays open. Rotate or open on a laptop: everything shows as before.
**Patch:** _patches/09-mobile-dashboard-collapse.patch (apply after 01-08)

## 10 - Flight schedule viewer (plane color + instructor color, neon for solo)
**What changed:** Each booking on the Schedule now shows two colors: the block is the plane's color and a thick stripe on its left is the instructor's color. Solo bookings use a neon (bright, fully saturated) version of the plane's color with a soft glow and no instructor stripe. Admins pick a plane's color in Manage > Planes > "Rate & Color" (same swatch picker as the CFI form, with a live preview of the normal and solo look). Instructors and planes share one 18-color palette, and any color already used by an active instructor or another plane is greyed out on both forms (and refused by the server), so a CFI and a plane can never have the same color. Planes with no color yet show grey and are marked "(no color set)" in the Key. The Key now lists planes (normal + neon swatch) and instructors separately; the List/Day tables show the plane swatch with the instructor stripe. Text on each block switches to dark on bright colors so it stays readable.
**Files:** db.py (migration adds assets.schedule_color), schema.sql, flight.py (SCHEDULE_COLORS palette, `_neon_color`, `_text_on`, `_used_plane_colors`, `_used_colors_for_plane`; CFI color checks include plane colors; plane_rate_edit saves the color; `_decorate_schedule_row` sets plane/instructor colors; `plane_legend` for the Key), templates/flight/_schedule_live.html, templates/flight/schedule.html, templates/flight/plane_rate_form.html, templates/flight/planes.html, templates/flight/cfi_form.html.
**Migration:** automatic on restart (new column). No new packages.
**Test:** Manage > Planes > Rate & Color on each plane: the instructors' colors are greyed out; pick one and Save. Schedule > Month: a dual lesson is the plane color with the instructor's stripe; a solo booking is the same plane in neon. Try picking a plane's color on a CFI: it's greyed out.
**Patch:** _patches/10-plane-and-cfi-schedule-colors.patch (apply after 01-09)

## 11 - flight School dashboard (student full name, instructor first name)
**What changed:** On the Fly with Kate! dashboard (Next Lesson, Today's Schedule, Pending Approval, Upcoming, Recent Flights) instructors now show by first name only ("Kate" instead of "Kate Smith"), and students keep their full name. The same rule applies on the Schedule's calendar blocks and List/Day tables. In the calendar blocks the student / instructor line now wraps onto a second line instead of being cut off with "...", so the full student name is always readable. Instructor names in dropdowns, the Key and forms are unchanged.
**Files:** app.py (new `first_name` template filter), templates/flight/_dashboard_live.html, templates/flight/_schedule_live.html, templates/flight/schedule.html (line wrap CSS).
**Test:** Flight dashboard: Today's Schedule shows e.g. "Samantha Studentsworth" and "Kate". Schedule > Month: a block reads "Samantha Studentsworth / Kate", wrapping if it's long.
**Patch:** _patches/11-dashboard-names.patch (apply after 01-10)

## 12 - Should not be able to start future flights (revision: ask to move it to now)
**Feedback:** "doesn't work. can schedule start future flights without warning or prompt to change time"
**What changed:** Instead of a greyed-out button that was easy to miss, a Start on a flight that isn't due yet (more than 30 min before its slot) is now amber ("Start" with a clock on the dashboard, "Start Early..." in the Schedule's flight details). Pressing it pops up "This flight isn't due yet - it's booked for Thu 9/24 at 2:00 PM. Is it actually leaving now?" with **Keep booking** or **Move to now & start**. Choosing Move to now re-checks conflicts at the current time, moves the booking to now, then starts it (a flash says "Booking moved from ... to now"). If moving it would clash with another booking it isn't started and the clash is shown. Anything that posts a start without going through the prompt is refused with an explanation.
**Files:** flight.py (`_slot_label` helper, `slot_label` on dashboard Upcoming, `schedule_start` handles `move_to_now`), templates/flight/_early_start_modal.html (new prompt + script), templates/flight/base_flight.html (includes it for CFIs), templates/flight/_dashboard_live.html, templates/flight/schedule.html.
**Test:** Book a flight 2 hours out. Dashboard > Upcoming: its Start is amber; press it and the prompt appears; Keep booking does nothing. Press again > Move to now & start: you land on Active Flight and the booking now shows the current time. Same from Schedule > click the flight > Start Early...
**Patch:** _patches/12-early-start-prompt.patch (apply after 01-11)

## 13 - Mobile dash view (revision: phone alerts show ON + admin test alerts)
**Feedback:** "I clicked enable on this device and notifications icon came up but app does not show that they are enabled. also add a phone alerts test on the admin side where I can select specific people or all to test"
**What changed:** Found why Enable never finished: the phone-alert helper (service worker) was served from /static/, so it only "covered" /static/ pages and the My Account page waited forever for it after the phone granted permission. It's now served from the site root (/push-sw.js) and My Account uses it directly, clears out the old one, and shows a green **ON** or grey **OFF** badge (or says notifications are blocked in the phone's settings). Admin > Notifications has a new **Test Phone Alerts** card: tick specific people (each shows how many devices they've turned on) or choose Everyone, optionally type a message, and Send Test Alert. The result banner says per person how many devices it reached, which failed (with the push service's error) and who has no devices turned on. The mobile dashboard collapse from the first attempt is unchanged.
**Files:** app.py (`/push-sw.js` route, push people list, `admin_notifications_test_push`), push.py (`queue_and_push` returns per-device results), templates/account.html (status/enable/disable script), templates/admin_notifications.html (test card), templates/flight/base_flight.html (registers the root worker).
**Test:** After deploying, on the phone open My Account (from the home-screen icon on iPhone), tap Enable: it should switch to a green ON badge. Then Admin > Notifications > Test Phone Alerts, tick yourself, Send: the phone buzzes and the banner says "sent to 1 device".
**Note:** anyone who tapped Enable before this fix needs to tap it once more.
**Patch:** _patches/13-phone-alerts-status-and-admin-test.patch (apply after 01-12)

## 14 - Button confirmations
**What changed:** Important buttons now ask "are you sure?" in a clean pop-up (title, the question, Cancel and a clearly labeled go-ahead button) before doing anything, on the shop, admin and flight school sides alike. New confirmations: Mark Paid / Mark Unpaid (flight detail, Billing, Flight History), End Flight, Approve a student's flight request, Add Funds (shows the amount, e.g. "Add $250 to Sam's balance?"), Acknowledge squawk (shop dashboard, Squawks, asset page), Complete maintenance, Order Received, and Mark Project Completed. All the existing browser "OK/Cancel" boxes (Mark all paid, deny request, cancel booking, delete/archive/purge, cancel order, remove to-do/maintenance/photo, admin reset/restart/reboot, clear log) now use the same pop-up, with the go-ahead button in red for anything destructive. Log Out keeps its simple browser prompt. If the page's Bootstrap script ever fails to load, it falls back to the browser's own OK/Cancel box, so nothing goes through unasked.
**How to add more later:** put `data-confirm="Question?"` on any form (optional `data-confirm-title`, `data-confirm-ok`, `data-confirm-style="danger|success|warning"`); `[[field]]` in the question is replaced with that field's value.
**Files:** templates/_confirm_modal.html (new), templates/base.html, templates/admin_base.html, templates/flight/base_flight.html (include it), plus data-confirm on the buttons in: templates/flight/log_detail.html, templates/flight/billing.html, templates/flight/log_history.html, templates/flight/log_active.html, templates/flight/_dashboard_live.html, templates/flight/student_form.html, templates/flight/schedule.html, templates/dashboard.html, templates/squawks.html, templates/asset_detail.html, templates/orders.html, templates/project_detail.html, templates/admin_system_log.html, templates/admin_system.html, templates/trash.html, templates/admin_reset.html, templates/part_detail.html.
**Test:** Flight History > Mark paid: a "Mark Paid" pop-up appears; Cancel leaves it unpaid, "Yes, mark paid" marks it. Students > edit a student > Add Funds 250: the pop-up reads "Add $250 to <name>'s balance?". Schedule > click a flight > Cancel: the details close and "Cancel this scheduled flight?" asks first.
**Patch:** _patches/14-button-confirmations.patch (apply after 01-13)

## 15 - flight school dashboard (collapse works everywhere)
**What changed:** Today's Schedule, Upcoming and Recent Flights can now be folded and unfolded on any screen - phone, tablet or computer - by tapping the section title (the > turns down when open) or anywhere else on its header row. Before, the fold only worked below phone width, and the tap target was a plain header, which iPhone Safari can ignore. Each title is now a real button so taps always register. Sections start folded on a phone and open on a wider screen, and whatever you choose is now remembered on that device across visits (it used to reset when the tab closed). "Full Calendar" and the other header buttons still work normally, and the 15-second live refresh keeps your choice.
**Files:** templates/flight/dashboard.html (CSS + toggle script), templates/flight/_dashboard_live.html (section titles are buttons).
**Test:** On the Flight dashboard (phone and computer) tap "Upcoming": it folds to one line with a count; tap again to open. Reload the page: it stays how you left it.
**Patch:** _patches/15-dashboard-collapse-any-screen.patch (apply after 01-14)

## 16 - Mobile dash view (revision 3: alerts actually arrive)
**Feedback:** "I see the buttons and tests but never got an alert"
**Why they didn't arrive:** pushes were sent empty. The phone had to fetch the alert text from OpsHub in the background using the login cookie, and an iPhone home-screen app usually doesn't have that cookie once it's in the background (unless "Remember me" was ticked). So the fetch hit the login page and nothing showed. Also, the VAPID contact was "mailto:admin@flywithkate.local", which Apple's push service can reject.
**What changed:** Each push now carries its alert inside it, encrypted the standard Web Push way (RFC 8291). The encryption is pure Python, so there's still no pip install. The phone shows the alert without needing to be logged in. The VAPID contact is now https://flywithkate.com, tokens last 1 hour, and pushes are sent with high urgency and a 10-minute TTL. The service worker shows the payload, then quietly checks the old queue as a backup, skipping duplicates. If a push ever arrives empty it still shows "You have a new alert - tap to open", because iPhone switches alerts off for sites whose pushes show nothing. Queued alerts older than 30 minutes are dropped. My Account now has a **Send me a test alert** button next to Enable.
**Files:** push.py (AES-128-GCM + `encrypt_payload`, payload in `send_web_push`/`queue_and_push`, VAPID subject), static/push-sw.js, flight.py (`push_pending` drops stale alerts), app.py (`account_push_test`), templates/account.html (test button).
**Test:** Deploy, then on the phone open OpsHub from the home-screen icon > My Account and tap Turn off, then Enable (this refreshes the worker). Tap Send me a test alert: the banner says "sent to 1 device" and the phone shows the alert within a few seconds. Then try Admin > Notifications > Test Phone Alerts.
**Patch:** _patches/16-phone-alerts-encrypted-payload.patch (apply after 01-15)

## 17 - Logging student landings (revision: one line per student on phones)
**Feedback:** "I see the landing marked but too clunky on student side. on mobile app need one line per student include as much basic information for mobile but do not exceed one line per. extra info upon clicking"
**What changed:** On a phone the Students tab is now a tidy list with exactly one line per student. Each line shows the name (cut off with "..." if long), days since their last lesson ("3d" or "today"), landings in the last 90 days, solo currency (a green badge with days left, red "Due", or "No dual") and account balance (green credit or red "-$246" owed). A small key line at the top explains the columns. Tap a line to open the rest underneath: last lesson and last landing (both link to the flight), full solo status, account, instructor and plane rates (tap to edit), username and status, plus an "Open student" button. Tap again to close it. Search still filters the list. Tablets and computers keep the full table unchanged.
**Files:** templates/flight/students.html.
**Test:** On your phone open Students: every student fits on one line. Tap one to see the details, then tap again to close it.
**Patch:** _patches/17-students-one-line-mobile.patch (apply after 01-16)

**Deploy all:** copy the changed files (app.py, db.py, flight.py, schema.sql, templates/account.html, templates/asset_detail.html, templates/base.html, templates/admin_base.html, templates/academy.html, templates/_back_button.html, templates/flight/base_flight.html, templates/flight/schedule.html, templates/flight/_schedule_live.html, templates/flight/dashboard.html, templates/flight/_dashboard_live.html, templates/flight/students.html, templates/flight/student_form.html, templates/flight/planes.html, templates/flight/plane_rate_form.html, templates/flight/cfi_form.html, templates/flight/_early_start_modal.html, push.py, templates/admin_notifications.html, templates/_confirm_modal.html, templates/flight/log_detail.html, templates/flight/billing.html, templates/flight/log_history.html, templates/flight/log_active.html, templates/dashboard.html, templates/squawks.html, templates/orders.html, templates/project_detail.html, templates/admin_system_log.html, templates/admin_system.html, templates/trash.html, templates/admin_reset.html, templates/part_detail.html, static/push-sw.js) into the newest shopinv_vN (or apply the patches in order with `patch -p1`), then rsync to the Pi and `sudo systemctl restart opshub`. Database changes from 04, 05 and 10 run automatically on restart.

## 18 - fix (Flight dashboard crash: "Could not build url for endpoint 'push_service_worker'")
**Cause:** app.py, flight.py and a few templates in shopinv_v10 were changed at about 21:30-21:40 (the dashboard "chip" board and one-file-per-error log) starting from an older copy that didn't have the queued changes. Copying those files over v10 and the Pi dropped the /push-sw.js route (so every Fly with Kate! page crashed in base_flight.html), plus the aircraft photo fallback, the three alert checkboxes, the early-start prompt, the admin/My Account test alerts, the encrypted phone-alert payload, confirmations on the dashboard and the one-line Students list.
**What changed:** Three-way merge of both lines of work. Kept the new dashboard chip board (Today + Upcoming by day, chips grouped by time and plane) and the per-file Admin error log, and put back everything from 01-17. Ported into the new board: chips for flights that aren't due yet get the amber Start that asks "move to now?"; Approve/Deny use the confirm pop-up; the Error Log "Clear" uses the confirm pop-up; open/closed dashboard sections are remembered on the device. flight.py now also adds the early-start check to today's flights (the server refuses early starts without the prompt, so the chip needed it). The old queued collapse code in dashboard.html (dash-toggle-btn) is replaced by the new board's collapse.
**Files:** app.py, flight.py, push.py, static/push-sw.js, static/css/style.css, templates/account.html, templates/admin_home.html, templates/admin_system_log.html, templates/flight/_dashboard_live.html, templates/flight/dashboard.html, templates/flight/students.html (push.py, push-sw.js, account.html and students.html are unchanged in the queue but were missing from v10/the Pi).
**Tested:** scratch database with Flask: /flight/dashboard, /flight/dashboard/live, /flight/schedule, /flight/students, /account, /admin, /admin/notifications, /admin/system/log and /push-sw.js all load; every url_for() in templates resolves; early-start chips appear only for flights more than 30 min out; a logged error shows on the Error Log page.
**Patch:** _patches/18-fix-dashboard-crash-merge.patch (apply after 01-17)
**Heads-up:** make future edits in shopinv_queue (or copy queue files into v10 first), not in an older copy, or the same thing happens again.

## 19 - Sync: brought in the other chat's 22:05 edits to shopinv_v10 (plane colors + shared Details pop-up)
**Why:** app.py, db.py, flight.py, style.css, asset_form.html and the flight dashboard/schedule templates in shopinv_v10 were rewritten at 22:05 from a copy that didn't have the queued changes again. As-is, v10 would crash the Flight pages again (no /push-sw.js route) and drop the phone-alert tests, the three alert checkboxes, the aircraft photo fallback, the early-start prompt and the confirm pop-ups.
**What changed:** Merged both lines of work. Kept from v10: each plane gets its own color picked on the plane's edit page (taken colors are greyed out), dashboard chips show an instructor stripe plus a plane stripe, the dashboard chip "Details" button opens the same flight pop-up as the Schedule (now in templates/flight/_schedule_detail_modal.html), List view scrolls to the latest flight, and the new calendar CSS. Kept from the queue: everything in 01-18. Ported into the new shared pop-up: the early-start "Start Early..." warning and move-to-now prompt, and the confirm pop-up on Cancel Flight. The dashboard's early-start chips still get their can_start data.
**Files:** app.py, db.py, flight.py, static/css/style.css, templates/asset_form.html, templates/flight/_dashboard_live.html, templates/flight/_schedule_detail_modal.html, templates/flight/dashboard.html, templates/flight/schedule.html
**Patch:** _patches/19-sync-v10-plane-colors-details-popup.patch (apply after 01-18)

## 20 - reset buttons (Flight School resets in Admin > Reset Data)
**What changed:** Admin > Reset Data is now split into Shop and Flight School. New Flight School buttons: Reset Schedule (all bookings/requests), Reset Balances (student account history, balances to $0), Reset Students (with their flights, bookings, account history and student-only logins; station accounts like "Shop" are kept), Reset Instructors (all except master admins' own profiles; their flights stay with no instructor, upcoming bookings get flagged "needs review"), plus a red "Reset All Flight School Data" that needs RESET typed in a box and does everything including planes. Every reset runs all-or-nothing and says what went wrong if it can't run.
**Fixes:** the existing Reset Flight Log and Reset Planes buttons would have errored when a flight had a billing entry or a plane had bookings/to-dos/compression checks/photos (foreign keys are on). They now clear the linked rows first.
**Files:** app.py, templates/admin_reset.html
**Tested:** scratch database: each button alone, in different orders, and Reset All (wrong word refused, "reset" accepted); master admin login and CFI profile kept, a student who also has shop access keeps their login with the flight role removed; all Flight/admin pages still load.
**Patch:** _patches/20-flight-school-reset-buttons.patch (apply after 01-19)

## 21 - Permsisions (owner account can't be changed by other master admins)
**What changed:** The first master admin account (Frank) is now the **Owner**, shown with a lock badge in Admin > Accounts. Other master admins can't open or save changes to the owner's account (name, roles, master admin, active, password), and the owner's password shows as "hidden" to them. They also can't save changes to the owner's instructor or student profile on the Flight School side. The owner can still change everyone, including demoting or deactivating other master admins. The owner is remembered in app_settings ('owner_user_id') the first time it's needed (oldest master admin), so adding new admins never moves it.
**Files:** auth.py (owner_user_id / is_owner / owner_locked), app.py (Accounts list + edit), flight.py (cfi_edit / student_edit), templates/admin_users.html
**Tested:** scratch database with two master admins: the second one sees "Owner" and "Locked", can't see the owner's password, and is turned away from editing the owner in Accounts and Instructors; the owner edits and demotes the second admin fine.
**Patch:** _patches/21-owner-account-lock.patch (apply after 01-20)

## 22 - Dashboard shows all flights in progress (2026-09-24)
- Dashboard's "flights in progress" banner now lists every running flight school-wide (was only the logged-in CFI's own), including solos, with the instructor's first name.
- Button renamed "View / End Flights"; ending is still limited to the assigned CFI, the solo student, or an admin on the Active Flights page.
- Files: flight.py, templates/flight/_dashboard_live.html

## 23 - Dashboard chip colors match the plane/instructor profiles, wider stripes (2026-09-24)
- **Cause:** the dashboard's plane stripe read the shop asset form color (assets.color), while the Schedule and its legend use the color picked in Flight School > Planes > Edit (assets.schedule_color). Bookings with no instructor (solo / needs CFI) got a made-up "Solo" hash color that could look like a real CFI's.
- **What changed:** plane stripe now uses the Planes > Edit color first (then the shop form color, then the Schedule's grey for "no color picked"). Instructor stripe is the CFI's picked color (unchanged logic, same as the Schedule); no-instructor bookings get a plain light-grey stripe. Stripes are wider (instructor 7px -> 12px, plane 4px -> 10px) and hovering a stripe names the instructor / plane.
- **Files:** flight.py, static/css/style.css, templates/flight/_dashboard_live.html
- **Tested:** scratch database: CFI with purple, plane with asset color blue + schedule color yellow; /flight/dashboard and /flight/dashboard/live show purple + yellow stripes, solo chip shows grey + yellow; /flight/schedule still loads.
- **Patch:** _patches/23-dashboard-schedule-colors.patch (apply after 01-22)

## 24 - Dashboard header: date/day, local + Zulu clocks, sunrise/sunset and civil twilight (2026-09-24)
- To the right of "Welcome, ..." there's now a small time box: day and date, a live Local clock (America/New_York) and Zulu clock (HHMMZ, ticks every 15 s in the browser), and today's civil twilight begin, sunrise, sunset and civil twilight end at N89. Hover a sun time for its Zulu time. On a phone the box drops under the welcome line at full width.
- Sun times come from new sun.py, which works them out locally (NOAA sunrise equation, ~1 min accuracy) for the local calendar day - no internet needed, and it shows for students/stations too, not just the CFI weather card. (The weather card's sunrise-sunset.org times use the UTC day, so after 8 PM Eastern they show tomorrow's; left as-is.)
- Files: sun.py (new), flight.py (import sun, sun_times in dashboard context), templates/flight/dashboard.html
- Tested: scratch database, /flight/dashboard, /flight/dashboard/live, /flight/schedule load; 2026-09-23 at N89 gives civil 6:17 AM, sunrise 6:45 AM, sunset 6:53 PM, civil 7:21 PM.
- Patch: _patches/24-dashboard-clock-sun-times.patch (apply after 01-23)

## 25 - Dashboard: compact weather + NOTAMs quick view moved up (2026-09-24)
- Weather and NOTAMs now sit together near the top of the dashboard (under the in-progress / Next Lesson banners, above Today's Schedule) instead of between Upcoming and Recent Flights.
- Weather is one compact strip: icon + temp (feels-like, conditions), wind arrow/speed/direction/gust, clouds, visibility, precip, and the METAR on one line (full text on hover; wraps on a phone). Background icon shrunk. Sunrise/sunset/civil twilight removed here because the header time box (24) shows them.
- NOTAMs card shows a count badge ("1 active" / "None" / "Not loaded") plus the FAA link; click the header to open the list. Uses the same remembered open/closed toggle as the schedule sections (key "notams"), starts closed.
- Files: templates/flight/_dashboard_live.html, templates/flight/dashboard.html (CSS for the compact strip)
- Tested: scratch database with stubbed weather/NOTAMs (configured with one NOTAM, and not configured): /flight/dashboard and /flight/dashboard/live load and the conditions row renders before Today's Schedule.
- Patch: _patches/25-dashboard-compact-conditions.patch (apply after 01-24)

## 26 - Account: one centralized My Account for every program (2026-09-24)
- **Before:** My Account extended the Maintenance layout (base.html), so opening it from Flight School, Academy or Admin dropped you on the Maintenance side.
- **What changed:** My Account now has its own neutral OpsHub layout (new templates/account_base.html) with buttons for every program the person can use, and a "Back to <program>" button for the one they opened it from. Each program's user menu links with ?from=shop|flight|academy|admin (falls back to the referring page, remembered in session across Save / test alert). The Cancel button and navbar Back also return to that program.
- New "Your Programs" card lists each program the account can open (Maintenance, Flight School, Academy, Admin) with the role there (Admin/Tech/Student, Instructor (CFI)/Student, Master admin/Owner). Read-only; access still changed in Admin > Accounts.
- Reminder checkboxes are grouped by program and only shown for programs the person has; a hidden program's settings are kept on save instead of being switched off (before, a flight-only user saving the page cleared their shop reminders).
- Program picker (home) gets a "My Account" link next to Log Out. Works the same for every user, not just admins.
- **Files:** app.py (ACCOUNT_FROM_PROGRAMS, _account_programs, _account_came_from, _render_account, account_page, account_push_test redirect), templates/account_base.html (new), templates/account.html, templates/base.html, templates/flight/base_flight.html, templates/academy.html, templates/admin_base.html, templates/home_launcher.html
- **Sync:** before this change, brought the other chat's new shopinv_v10 edits into the queue (Schedule Day-view hour timeline + month-cell mini timeline in flight.py/_schedule_live.html/schedule.html, smooth plane-marker movement in log_active.html), keeping queue changes 22-25 in flight.py.
- **Tested:** scratch database with master admin, CFI-only and shop-tech-only users: /account opens in the new layout from each program with the right Back button and program list; saving keeps the from-program; CFI-only save keeps shop reminders, tech-only save keeps flight alerts; /shop, /admin, /academy, /flight pages, Schedule Day view and the launcher still load.
- **Patch:** _patches/26-centralized-my-account.patch (apply after 01-25)

## 27 - student: solo sign-off date + 90-day expiry, flagged like solo currency (2026-09-24)
- **What changed:** Student add/edit form has two new fields any CFI can fill in: Solo Sign-off Date and Solo Sign-off Expires. The expiry fills itself in as sign-off + 90 days (typed-in expiry wins). The student page shows a "Solo sign-off" status (good to / expired / none on file); the Students list shows it in the Solo column (and the phone view's details, with an "S/O" badge when expired).
- **Flagging:** _schedule_review_flag() now takes the booking's date. A solo (no instructor) booked on a date after the student's sign-off expiry is flagged for review with the reason "Solo sign-off expired ...", exactly like an over-currency solo (dashboard Needs Review alert, acknowledge, clears when a CFI is added). Both reasons show if both apply. Students with no sign-off on file aren't flagged for this (only for currency, as before). Saving new sign-off dates re-checks that student's upcoming solo bookings and flags/clears them.
- **Files:** flight.py (SOLO_SIGNOFF_VALID_DAYS, _us_date, _solo_signoff_expiry_default, _solo_signoff_status, _schedule_review_flag flight_date arg + all 4 call sites, _refresh_solo_review_flags, _solo_signoff_from_form, student_new/student_edit/students_list), db.py (students.solo_signoff_date / solo_signoff_expires migration - runs automatically on restart), schema.sql, templates/flight/student_form.html, templates/flight/students.html
- **Sync:** before this change, brought the other chat's new shopinv_v10 edits into the queue (needs-review alert moved to the top of the Flight dashboard with an Acknowledge button + db.py needs_review_acknowledged columns, end time on dashboard chips). shopinv_v10's flight.py / _dashboard_live.html / style.css were missing queue changes 22-25 (all-flights-in-progress, chip colors/wider stripes, clock/sun times, compact conditions); those are re-merged on top of the other chat's edits, so this idea's copy step restores them in shopinv_v10 too.
- **Tested:** scratch database: expiry defaults to +90 days, a solo booked the day after expiry is flagged, one before isn't, saving a sign-off re-flags an existing future solo; Students list and student edit pages render.
- **Patch:** _patches/27-student-solo-signoff.patch (apply after 01-26)

## 28 - Planes: no rental rate, plane cost comes from the student's Plane Rate (2026-09-24)
- **Sync first:** brought in the other chat's latest shopinv_v10 edit ("needs_review_active": an acknowledged Needs Review flight no longer shows the warning triangle / red outline on the Schedule and dashboard chips). flight.py (_decorate_schedule_row + dashboard chip rows) and templates/flight/_dashboard_live.html merged by hand on top of the queue's dashboard changes; templates/flight/_schedule_live.html taken from shopinv_v10 as-is.
- Flight School > Planes: Rental Rate column removed; the "Rate & Color" button is now "Color" and the edit page only sets the Schedule color. Saving no longer touches assets.rental_rate (the column and old values stay in the database, unused).
- Billing (_row_with_cost / _LOG_ROW_SQL): plane rate = the student's Plane Rate (students.plane_rate_override) only. No fallback to the plane's rate, so a student with no Plane Rate is charged $0 for the plane. Instructor rate unchanged (student's Instructor Rate, else the CFI's).
- Student form / Students list wording: "Plane Rate" is what the student pays for any plane; blank shows "Not set" instead of "plane's rate". Maintenance asset page no longer shows a Rental Rate row.
- **Files:** flight.py, templates/flight/planes.html, templates/flight/plane_rate_form.html, templates/flight/student_form.html, templates/flight/students.html, templates/asset_detail.html (+ sync: templates/flight/_dashboard_live.html, templates/flight/_schedule_live.html)
- **Tested:** scratch database: student with $120 Plane Rate billed $180 for 1.5 hrs, student with none billed $0 plane; /flight/planes, plane color edit (GET/POST), students list/edit/new, asset page, log list/detail all load.
- **Patch:** _patches/28-plane-rate-from-student.patch (apply after 01-27; the sync merge isn't in it since it comes from shopinv_v10)

## 29 - "Log a Flight" folded into Schedule Flight ("Flight Already Complete?") (2026-09-24)
- **Removed:** the "Log a Flight" nav link (and its guided-tour step), the dashboard's Log a Flight tile (now a "Schedule Flight" tile), and the Active Flight page's "log a flight" link (now points at Schedule Flight with the box checked).
- **Schedule Flight** (CFI/admin, new bookings only - not edits, not student requests) gets a "Flight Already Complete?" switch at the top. Checked: time, duration, booking notes, private notes and Repeats hide; Hobbs/Tach start/end, oil, ground time, landings, Squawks/Notes and Already paid show (same fields as the old log form, Hobbs/Tach start fill from the plane). Button reads "Log Flight"; saving logs it into Flight History (no calendar booking), advances the plane's Hobbs/Tach, raises the squawk, and auto-deducts from the student's balance, exactly like before.
- flight.py: log_new's save logic moved into _save_logged_flight(conn, form, date_field, notes_field), used by log_new and by schedule_new when already_complete is set. Squawk textarea on the schedule form is "log_notes" so it doesn't clash with booking notes.
- /flight/log/new still works for "Log Flight" from a scheduled booking (pre-filled). Opened bare (old bookmark), it redirects to /flight/schedule/new?complete=1, which opens with the switch on.
- **Files:** flight.py, templates/flight/schedule_form.html, templates/flight/base_flight.html, templates/flight/dashboard.html, templates/flight/log_active.html
- **Tested:** scratch database + headless browser: switch hides/shows the right fields and relabels the button; logging from it saves the flight (Hobbs, landings, squawk) and updates the plane; Hobbs-end-below-start error keeps the switch on; normal scheduling and Repeats still work; booking "Log Flight" pre-fill still saves and completes the booking; students don't get the switch.
- **Patch:** _patches/29-log-flight-into-schedule.patch (apply after 01-28)

## 30 - Alerts tab: acknowledged flags go somewhere, stay until resolved, with an archive (2026-09-24)
- **Sync first:** brought in the other chat's newest shopinv_v10 edit to templates/flight/_dashboard_live.html (weather/NOTAMs moved up to right under Next Lesson). Merged by moving the queue's compact conditions strip (change 25) to that spot. shopinv_v10's flight.py and static/css/style.css were missing queue changes 22-25 again (all flights in progress, chip colors/wider stripes, clock/sun times); the queue versions are kept, so this idea's copy step restores them in shopinv_v10.
- **New Alerts tab** (Flight School nav, CFIs/admins, with a red badge for new ones / amber for acknowledged-but-open). Laid out like Squawks: Open (not acknowledged), Acknowledged Not Yet Resolved (shows who acknowledged and when), and a Resolved Alerts Archive (reason, who acknowledged, how/who/when resolved; latest 50 with "Show all").
- **Resolve buttons** go straight to the fix: solo sign-off expired -> the student's Solo Sign-off fields (#solo-signoff anchor); over solo currency -> the booking to add an instructor (plus "Check currency" on the student); no instructor -> the booking (Assign CFI). The dashboard's Needs Review banner uses the same Resolve button and has an "All Alerts" link.
- **Acknowledge** now lands on the Alerts tab. It doesn't clear anything: the caution triangle stays on the Schedule blocks and dashboard chips (new needs_review_flag) until the flag actually clears or the booking is flown/cancelled. The red outline is still only for unacknowledged ones.
- **Resolution tracking:** new flight_alerts table (db.py migration runs on restart; schema.sql). flight._sync_flight_alerts() runs after every Flight School form post (blueprint after_request, records the logged-in person as resolver) and when the Alerts page opens. It opens an alert for every flagged booking, copies acknowledgement, re-checks flagged solos so a renewed sign-off or a new dual flight clears them, and archives alerts whose booking is no longer flagged (Instructor assigned / Student cleared for solo / Booking cancelled / Flight flown / Booking deleted). Admin schedule reset also deletes those bookings' alerts.
- **Files:** flight.py, db.py, schema.sql, app.py (_wipe_schedule), templates/flight/alerts.html (new), templates/flight/base_flight.html (nav + tour step), templates/flight/_dashboard_live.html, templates/flight/_schedule_live.html, templates/flight/student_form.html
- **Tested:** scratch database: sign-off-expired solo + no-instructor booking both show in Open with the right Resolve links and a badge of 2; acknowledging moves one to Acknowledged with "by Kate Admin" and keeps its triangle on the Schedule and dashboard chip; saving a new sign-off on the student archives the solo alert (resolved by Kate Admin), assigning a CFI archives the other; dashboard, live dashboard and student edit pages load.
- **Patch:** _patches/30-flight-alerts-tab.patch (apply after 01-29; the sync merge isn't in it)

## 31 - Any CFI or admin can acknowledge a late flight (2026-09-24)
- **Sync first:** shopinv_v10 and the queue matched; nothing to bring in.
- The "Running more than 15 min past its scheduled block" banner on Active Flight: Acknowledge now works for any CFI (not just the assigned one) and any admin, plus the solo student as before. New flight._can_ack_overdue(); log_ack_overdue uses it instead of _can_end_flight. Ending/pausing a flight is unchanged (still assigned CFI / solo student / admin).
- People who can't acknowledge (e.g. a student watching someone else's flight) no longer see the button, instead of seeing it and getting an error.
- **Files:** flight.py, templates/flight/log_active.html
- **Tested:** py_compile; code review of the route + template.
- **Patch:** _patches/31-any-cfi-ack-late-flight.patch (apply after 01-30)

## 32 - Active Flight on a phone: flight list above the map (2026-09-24)
- **Sync first:** the other chat wrote a new **Week view** for the Schedule into shopinv_v10 (flight.py, templates/flight/schedule.html, templates/flight/_schedule_live.html) at 01:58. Its flight.py and _schedule_live.html were built on an older copy missing queue changes 22-24 and 27-31 (all flights in progress, schedule colors, clock/sun, solo sign-off, plane rate, log-into-schedule, Alerts tab, CFI ack). Merged by hand: only the Week view hunks were applied onto the queue's flight.py; _schedule_live.html was 3-way merged (clean); schedule.html taken from shopinv_v10 as-is (only Week view CSS + a padding tweak). This idea's copy step brings shopinv_v10 back up to date with all of that.
- templates/flight/log_active.html: on a phone the Active Flight list now comes first and the live map below it (dropped the order-1/order-2 swap). Desktop layout unchanged: list left, map right.
- **Files:** flight.py, templates/flight/log_active.html, templates/flight/schedule.html, templates/flight/_schedule_live.html
- **Tested:** scratch database: Active Flight renders the list before the map; any CFI, the admin can acknowledge a late flight, a student can't (no button, POST refused); Schedule Week/Day/Month/List and the live Week refresh all load with the booking showing.
- **Patch:** _patches/32-active-list-above-map-mobile.patch (log_active.html only; the Week-view sync merge isn't in it)

## 33 - Late flight: updated ETA, and flag the bookings it may delay (2026-09-24)
- **Sync first:** at 02:05 shopinv_v10's templates/flight/base_flight.html was overwritten with an older copy that drops the Alerts tab link and tour step (change 30), with nothing new in it. Kept the queue version and added it to this idea's copy step, so the copy puts the Alerts tab back in shopinv_v10.
- **Active Flight card:** once a flight's scheduled block is up (shown live, no reload), a "Running late? Put in an updated ETA" box appears with +15 / +30 / +45 min buttons or a time picker, and a Clear link. Same people who can acknowledge (any CFI, admin, or the solo student). Setting an ETA also quiets the running-late banner/pushes until that time passes.
- **Affected bookings:** worked out live by flight._eta_impacts(): still-booked flights later that day sharing the plane, instructor or student, starting before the ETA + 15 min turnaround (ETA_TURNAROUND_MIN). Nothing is stored on them, so the flags go away on their own when the late flight ends, the ETA is cleared/changed, or the booking starts or is cancelled.
- **Where they show:** listed on the late flight's card ("May delay 2 later bookings"); an amber "N Bookings May Start Late" banner on the dashboard; an hourglass icon + dashed orange outline on the Schedule blocks (Day/Week/Month/List, live refresh), with the reason on hover.
- **Push:** the affected bookings' CFI and student get "Your flight may start late" if they have Running late alerts on (My Account).
- **Database:** flights.eta_at, flights.eta_set_by (db.py migration runs on restart; schema.sql updated).
- **Files:** flight.py, db.py, schema.sql, templates/flight/log_active.html, templates/flight/_schedule_live.html, templates/flight/_dashboard_live.html (+ templates/flight/base_flight.html for the sync)
- **Tested:** scratch database: late dual flight + 4 later bookings; a CFI sets +45 min -> exactly the same-plane and same-CFI bookings flagged (other plane/people and a 3-hour-later booking not); pushes go to those bookings' CFIs; overdue banner stays quiet while the ETA is ahead; dashboard banner, Schedule Day/Week/Month/List and live refresh show the icons; past time rejected; Clear and ending the flight remove the flags; a non-solo student gets no ETA box and can't post one.
- **Patch:** _patches/33-late-flight-updated-eta.patch (apply after 01-32)

## 34 - Winds Aloft header: outlined nav buttons like Fly with Kate! (2026-09-24)
- **Sync first:** templates/base.html matched shopinv_v10; nothing to bring in (the pending v10 differences are queue changes 31-33 plus the base_flight.html restore noted in 33).
- templates/base.html: the Winds Aloft (shop) top bar's links (Scan, Parts, Projects, Aircraft, Calendar, Orders, Laborers, Squawks, Activity, and the user menu) now sit in the same outlined, rounded "title block" buttons as the Fly with Kate! header. Same CSS as .flight-nav-boxed, added as .shop-nav-boxed in base.html. Colors (dark bar, yellow Scan) unchanged.
- **Files:** templates/base.html
- **Tested:** template parses; CSS identical to the flight header's. Not rendered with Bootstrap here (CDN/package downloads blocked in this run).
- **Patch:** _patches/34-winds-aloft-boxed-header.patch (apply after 01-33)

## 35 - Acknowledge / ETA: clearer buttons and a visible "it worked" (requeue of 31: "I see the eta option but I cannot click") (2026-09-24)
- **Sync first - NEEDS A DECISION:** at 02:13-02:14 the other chat wrote its own Alerts rework into shopinv_v10 (app.py, db.py, flight.py, templates/flight/alerts.html: a flight_review_log table, needs_review_flagged_at, Open/Resolved tabs). That's a second version of queue change 30 (flight_alerts table), and it's built on an older copy missing queue changes 22-24 and 27-34 (all flights in progress, schedule colors, clock/sun, solo sign-off, plane rate, log-into-schedule, Alerts #30, CFI ack, list-above-map, late-flight ETA). The two Alerts designs can't be merged line by line, so the queue keeps its lineage. **The other chat's files are saved untouched in _tmp/v10-at-0214-other-chat-alerts/.** This idea's copy step brings shopinv_v10 back to the queue's version of those files. If you'd rather keep the other chat's Alerts page, requeue with that note and I'll port the queue features onto it instead.
- Couldn't reproduce a dead button: in a real browser (phone and desktop widths) +15/+30/+45, the time picker + Set ETA, and Acknowledge all click and save. The likely cause is how it looks and feels: Acknowledge was a faint white outline on pink (looked disabled), the +N buttons were outlined, and the result only showed in a flash message at the top of the page, out of view on a phone.
- Acknowledge is now a solid white button with red bold text and a check. +15/+30/+45 are solid amber; Set ETA is dark.
- Tapping shows "Saving..." with a spinner right away and locks the form so it can't double-post (the tapped +N value is still sent).
- After saving, a green note appears inside the flight card: "ETA saved: 3:07 AM. 2 later bookings flagged." or "Acknowledged - the running-late alert is quiet for 15 min." (log_active reads ?eta_saved= / ?acked= from the redirect).
- **Files:** flight.py, templates/flight/log_active.html (copy step also includes app.py, db.py, templates/flight/alerts.html to put shopinv_v10 back on the queue version - see above)
- **Tested:** scratch database + headless Chromium with Bootstrap 5.3: every button is the top element at its center and saves on click (phone 390px and desktop); time picker path saves; inline notes show; earlier ETA/ack tests all still pass.
- **Patch:** _patches/35-eta-ack-tap-feedback.patch (apply after 01-34)

## 36 - Winds Aloft Manage menu: Laborers, Labor Pay, Billing, Stats (2026-09-24)
- **Sync first:** shopinv_v10 matched the queue (you copied 35's files in, so the queue's Alerts version stays). Brought in the other chat's new tools/predeploy_check.py (it's in the copy step so v10 keeps it).
- **Manage dropdown** in the Winds Aloft header (next to your name, outlined like the rest): Laborers, Labor Pay, Billing, Stats for shop admins; everyone else gets My Pay. Laborers moved out of the top bar into it; Orders stays in the top bar.
- **Labor Pay** (/shop/pay): each laborer's clocked hours and pay for the period, with every session (date, project, task, hours, rate at clock-in, pay). Shows who's still clocked in. Non-admins see only the laborer whose name matches their login ("My Pay"). Defaults to this week.
- **Billing** (/shop/billing): every project with parts or labor in the period: parts at sell price + labor = total, with an Invoice button (the existing customer invoice CSV). Totals at top and bottom; your parts cost noted. Defaults to this month.
- **Stats** (/shop/stats): labor hours and dollars, parts billed/cost/margin, projects worked, total billed, hours by laborer, hours by aircraft, most-used parts.
- All three share a period picker: This Week / Last Week / This Month / Last Month / This Year / All Time, or a custom date range.
- **Files:** app.py (_shop_period, _shop_parts_usage, shop_pay, shop_billing, shop_stats), templates/base.html, new templates/_shop_period.html, shop_pay.html, shop_billing.html, shop_stats.html, tools/predeploy_check.py (from the other chat)
- **Tested:** scratch database with 2 laborers, 2 projects, parts in/out: pay/billing/stats totals match by hand ($230 week, $350 all-time labor, $120 parts, $470 billed); tech sees only their own pay, gets bounced from Billing, and their menu shows My Pay only; screenshots at desktop and phone width; tools/predeploy_check.py passes.
- **Patch:** _patches/36-shop-manage-menu-pay-billing-stats.patch (apply after 01-35)

## 37 - Every program header highlights the section you're in (2026-09-24)
- **Sync first:** shopinv_v10 unchanged since 35 (the remaining differences are change 36, not yet copied).
- Winds Aloft (templates/base.html), Fly with Kate! (templates/flight/base_flight.html) and Admin (templates/admin_base.html): the nav button for the page you're on is a solid white box with dark bold text; the others stay outlined. The Manage button lights up on any page in its menu, and that page is highlighted inside the dropdown.
- Section is worked out from the URL: shop Scan (/scan), Parts (/parts, /labels), Projects, Aircraft (/assets, /maintenance), Calendar, Orders, Squawks, Activity, Manage (/laborers, /shop/...); flight Schedule, Active Flight (/flight/log/active), Flight History (other /flight/log pages), Students, Alerts, Manage (Planes, CFIs, My Pay, Billing, Stats). Dashboards (the logo link) have no box lit. Admin already marked its active tab; it now uses the same solid look.
- **Files:** templates/base.html, templates/flight/base_flight.html, templates/admin_base.html. base.html includes change 36's Manage menu, so the copy step also carries 36's files (app.py + the shop_* templates); it's harmless if you've already copied 36.
- **Tested:** scratch database: 26 pages across shop/flight/admin each light exactly the right button (and Manage + item for menu pages); screenshots of both headers.
- **Patch:** _patches/37-header-highlight-current-section.patch (apply after 01-36)

## 38 - Late flight: automatic heads-up to the affected CFI and student (2026-09-24)
- **Sync first:** shopinv_v10 unchanged; the other differences are changes 36-37, not yet copied.
- When an updated ETA flags later bookings (change 33), each affected booking's CFI and student now get an automatic notice, with no "Running late" opt-in needed: "N123 is running late - new ETA 3:29 PM. Your 3:04 PM N123 flight today may start a little late. Please still arrive at your normal time unless someone reaches out to you."
- Sent as a phone push to everyone affected, plus email/text (their chosen channels) for anyone with "Upcoming scheduled flights" reminders on. Email/text goes out in the background so the page doesn't wait. The label on My Account now mentions it.
- Once per booking per ETA (notification_log, category eta_delay): re-saving the same ETA doesn't repeat it, and a new ETA sends an update. The person who entered the ETA isn't notified about their own change.
- The automatic 15-min "running late" alert with no ETA entered still goes only to that flight's instructor (or admins for a solo), since there's no new time to pass on yet.
- **Files:** flight.py (log_set_eta; imports notify, threading), templates/account.html
- **Tested:** scratch database: CFI B sets +45 -> pushes to CFI A and the student on both affected bookings (not CFI B), even with Running late alerts off; the student with flight reminders on gets the email; same ETA again sends nothing; a new ETA sends a fresh round.
- **Patch:** _patches/38-late-flight-auto-notify.patch (apply after 01-37)

## 39 - Medical class + expiration for students and CFIs (2026-09-24)
- **Sync (02:48 edit from the other chat):** shopinv_v10 got a new Month-view layout: fixed lane per plane, white blocks with plane/instructor edge colors (flight.py _layout_month_cell_timeline + plane_order, templates/flight/schedule.html CSS, templates/flight/_schedule_live.html month_lane_block/month_table). Its flight.py was on the current queue base, so the diff applied cleanly. Its _schedule_live.html was on an older base without changes 30 and 33, so it was 3-way merged (clean) and keeps the caution triangle for acknowledged flags plus the late-flight hourglass. Those were also added to the new month_lane_block. schedule.html taken from shopinv_v10 as-is. The copy step includes all three.
- **Fields:** Medical Class (First, Second, Third, BasicMed, or not tracked) and Medical Expires on the student form and the CFI form (#medical anchor). New columns students/cfis.medical_class and medical_expires (db.py migration runs on restart; schema.sql). Changes are logged like the other student/CFI fields.
- **CFI rule (hard stop):** a CFI whose medical has run out by the booking date can't be booked, approved, edited onto, rescheduled onto, or started. It's reported like a scheduling conflict ("...medical expired 09-23-2026 - a CFI can't fly without a current medical"), and a repeating series just skips the dates past the expiry. Saving a past expiry on a CFI warns how many of their bookings fall after it.
- **Student rule:** dual is always fine. A solo on a date past the student's medical expiry gets flagged for review (red outline, dashboard Needs Review, Alerts tab) with an "Update medical" Resolve button. It clears on its own when the medical is updated or an instructor is added (existing re-check on student save + _sync_flight_alerts).
- **Alerts when expired:** a "Medicals Needing Attention" banner on the Flight School dashboard lists students and CFIs whose medical is expired or runs out within 30 days, each with an Update button. Badges on the Students list (Med / Medical expired) and the CFIs list (Medical expired / Medical Nd / class), plus a Medical tile on the student page.
- Blank expiry = not tracked, so nothing is checked for people you haven't filled in yet.
- **Files:** flight.py, db.py, schema.sql, templates/flight/student_form.html, templates/flight/students.html, templates/flight/cfi_form.html, templates/flight/cfis.html, templates/flight/_dashboard_live.html (+ templates/flight/_schedule_live.html, templates/flight/schedule.html from the sync)
- **Tested:** every Schedule view (Month with the new lanes, Week, Day, List, Quarter, Year, live refresh) loads with flags/hourglass showing; scratch database: CFI with a medical expiring in 5 days bookable at +2 days, blocked at +10 ("runs out ..., before this flight"); student with an expired medical: dual saves unflagged, solo flagged with the medical reason and an Update medical link on Alerts; updating the medical clears that reason; CFI set to expired -> warning on save and Start blocked; new CFI saves class/expiry; dashboard banner + list badges show; every page loads; tools/predeploy_check.py OK.
- **Patch:** _patches/39-medical-class-expiration.patch (apply after 01-38)

## 40 - Student pilot certificate, ratings and badge tiers (2026-09-24)
- **Sync first:** shopinv_v10 unchanged since the 02:48 Month-view edit merged in 39.
- **Student form** (#pilot-certs): Pilot Certificate (Student, Sport, Recreational, Private, Commercial, ATP) and Ratings & Endorsements checkboxes (Instrument, Complex, High Performance, Tailwheel, Multi-Engine, CFI, CFII, MEI). New columns students.pilot_certificate and pilot_ratings (comma list); db.py migration on restart; schema.sql. Changes are logged.
- **Badge hierarchy:** the certificate sets the tier, in color and icon: Student (grey) < Sport (teal) < Rec (green) < PPL (blue) < CPL (purple, gradient) < ATP (gold, shiny). Each rating adds a gold star; CFI/CFII/MEI add an instructor tag. Shown next to the name on the Students list (desktop + phone) and large on the student's page. pilot_badge_info() is a template global with a score (certificate then ratings), ready for the Academy leaderboard.
- **Files:** flight.py, db.py, schema.sql, templates/flight/student_form.html, templates/flight/students.html, new templates/flight/_pilot_badge.html
- **Tested:** scratch database with one student per tier: list shows 8 badges in the right colors with stars/instructor tag; editing and adding a student saves certificate + ratings; pages load.
- **Patch:** _patches/40-student-certificates-ratings-badges.patch (apply after 01-39)

## 41 - Flight Academy: points, achievements and leaderboards (2026-09-24)
- **Sync first:** shopinv_v10 unchanged since 39's merge.
- The Flight Academy page (was a placeholder) is now the leaderboard. **academy.py** (new) works it all out live from the school's logged flights plus what students log themselves; only their own entries are stored (new academy_entries table; db.py migration on restart; schema.sql).
- **Points:** flight hours 10/hr, solo bonus 5/hr, day landings 1, night landings 3 (from the flight log); student-logged: distance 0.1/nm, landings 1, night landings 3, cross-countries 15, night hours 10/hr, instrument hours 10/hr; plus achievement bonuses and the certificate/ratings badge from change 40 (Sport 100, Rec 120, Private 200, Commercial 300, ATP 500, +50 per rating). All spelled out in a "How points work" box.
- **Achievements:** First Flight, First Solo, 10/50/100 Hours (Century), Greaser (100 landings), Night Owl (10 night landings), Cross-Country, Road Trip (1,000 nm), In the Soup (10 instrument hrs). Unlocked ones are gold; locked ones show a progress bar.
- **Leaderboards:** Points, Hours, Landings, Distance, Achievements (top 10, ties share a rank, trophies for the top 3, your own row highlighted, pilot badges next to names), for All Time / This Year / This Month.
- **Log Your Flying:** students log date, what, amount and a note for themselves; a CFI/admin can log for any student. Recent entries are listed with a remove button (your own, or any for staff). A student sees their points, rank, hours and badge count at the top.
- **Access unchanged:** still only accounts with Flight Academy Access (Accounts) plus master admins. Students need that box ticked to use it.
- **Files:** new academy.py, app.py (academy_page, academy_entry_add, academy_entry_delete), db.py, schema.sql, templates/academy.html. Uses templates/flight/_pilot_badge.html and the pilot columns from change 40, so copy 40 first.
- **Tested:** scratch database with 3 students and 4 flights: points match by hand (357 = 27 hrs + 6 solo + 8 day + 6 night + 60 achievements + 250 PPL/instrument); student logging, bad input rejected, staff student picker, delete; all three periods load; screenshot.
- **Patch:** _patches/41-academy-leaderboard.patch (apply after 01-40)

## 42 - Master admin can delete a flight from Flight History (2026-09-24)
- **Sync first:** shopinv_v10 unchanged; log_detail.html and log_history.html matched.
- New route flight.log_delete (POST /flight/log/<id>/delete), master admin only (admin_required; CFIs are bounced). There's a trash button on each Flight History row and a "Delete Flight" button on the flight's detail page, both admin-only, with a red confirm showing the date, plane, student and charge.
- Deleting removes the flight's ledger deduction(s) and puts the student's balance back. A flight still in progress sends its booking back to "scheduled". Its squawk (if any) goes with it. The plane's Hobbs/Tach aren't rolled back (fix on the plane if needed). The deletion is recorded in field_change_log (entity "flight", field "deleted", with a summary and who did it).
- **Files:** flight.py, templates/flight/log_detail.html, templates/flight/log_history.html
- **Tested:** scratch database: a $315 flight takes the balance from 500 to 185, and deleting it restores 500 and removes the ledger row; a CFI doesn't see the button and their POST is refused; an in-progress flight's booking returns to scheduled; the log entry is written.
- **Patch:** _patches/42-admin-delete-flight.patch (apply after 01-41)
- **Sync note (02:57):** the other chat added templates/flight/availability.html to shopinv_v10 (work in progress: it uses a flight.schedule_availability route that doesn't exist yet). Copied into the queue so it's kept; left out of the copy steps until its route lands.

## 43 - A badge for each rating (2026-09-24)
- **Sync first:** shopinv_v10 matched the queue (you'd copied 36-42). templates/flight/availability.html had been removed from v10, so the queue's copy was moved to _tmp/availability.html.removed-from-v10.
- Each rating/endorsement now gets its own outlined badge in its own color and icon: IFR (slate, cloud), Complex (orange, gear), High Perf (red, gauge), Tailwheel (brown), Multi (teal, fan), and purple CFI / CFII / MEI. They're short on the Students list (desktop + phone), full names on the student's page, and next to "My Achievements" in the Academy. They show even with no certificate set (rating_badge_items() template global; RATING_BADGES in flight.py).
- The certificate pill keeps its stars (one per rating) but drops the "CFI/CFII/MEI" tag, since those now have their own badges.
- **Files:** flight.py, templates/flight/_pilot_badge.html, templates/flight/student_form.html, templates/flight/students.html, templates/academy.html
- **Tested:** scratch database: 8 students across all tiers show 28 rating badges; a student with ratings and no certificate shows just the rating badges; edit/new still save; screenshots of the list and the profile; predeploy check OK.
- **Patch:** _patches/43-rating-badges.patch (apply after 01-42)

## 44 - Calendar: block size follows booking length in the Month view (2026-09-24)
- **Sync first:** shopinv_v10 unchanged (the only differences are change 43, not yet copied).
- Day and Week views already draw each booking to scale by duration. The Month view's lane blocks (the other chat's 02:48 layout) were all one fixed height. Now each one's height follows its length: 0.4 px per minute (MONTH_BLOCK_PX_PER_MIN), so a standard 1.5 hr block is 36px, a custom 1 hr is 24px and a 2 hr is 48px, with an 18px minimum so a 30-min booking still shows its time and plane. A booking with no length set is drawn as the standard 1.5 hr. Stacking within a plane's lane and the cell height use the real heights, so nothing overlaps or clips.
- **Files:** flight.py (_layout_month_cell_timeline, new MONTH_BLOCK_PX_PER_MIN / MONTH_BLOCK_MIN_PX), templates/flight/_schedule_live.html (month_lane_block height)
- **Tested:** scratch database with 1.5 / 1 / 2 / 0.5 hr and no-length bookings: heights 36 / 24 / 48 / 18 / 36 px; Month, Day, Week, Quarter, Year, List and the live refresh all load; screenshot of a month cell.
- **Patch:** _patches/44-month-blocks-sized-by-duration.patch (apply after 01-43)

## 45 - Scheduling: type the time, plus bigger blocks with smaller text (2026-09-24)
- **Sync (03:24 edit from the other chat):** shopinv_v10 got CFI aircraft-category endorsements: High Performance, Complex and Tailwheel on the CFI form/list, a CFI_CREDENTIALS list and _cfi_creds_from_form in flight.py, and 3 new cfis columns in db.py/schema.sql. Its flight.py was on the pre-43 queue base, so the diff applied cleanly onto the queue's. db.py, schema.sql, templates/flight/cfis.html and cfi_form.html were taken as-is (the queue had nothing newer in them). The copy step includes them.
- The Time picker's hour, minute and AM/PM boxes are now text boxes you can type in (tapping one selects it). Hour takes 1-12 (or 13-23 as 24-hour), minutes 0-59, AM/PM takes a or p. The hour box also takes a whole time: 2:45p, 245p, 10:10a, 14:30 or 1430. The arrows and quick picks work as before; Up/Down arrow keys step the box you're in, and Enter finishes the time instead of submitting the form.
- Typed minutes are kept exactly (10:10 stays 10:10), and editing an existing booking no longer rounds its time to the quarter hour. The minute arrows step to the next/previous quarter hour from wherever it is.
- A one-line hint under the picker explains it.
- **Bigger blocks, smaller text (added to the idea while it ran):** the Month view's time axis and block heights now share one scale (0.45 px/min, up from 0.25 for the axis), so blocks are bigger: 1.5 hr = 40px, 1 hr = 27px, 2 hr = 54px, min 22px. Back-to-back bookings sit edge to edge instead of overlapping. Month block text is smaller (.56rem) with a third line on blocks tall enough ("to 9:30a · 1.5 hr"; hidden on phones). Week view block text is a bit smaller (.62/.58rem) so more fits in its narrow columns. Month day cells are taller as a result.
- **Files:** templates/flight/schedule_form.html, flight.py, templates/flight/_schedule_live.html, templates/flight/schedule.html (+ db.py, schema.sql, templates/flight/cfis.html, cfi_form.html from the sync)
- **Tested:** month heights 40.5/27/54/22 px for 1.5/1/2/0.5 hr with a screenshot; every Schedule view + live refresh loads; CFI endorsement columns migrate; medical tests still pass; predeploy check OK. Headless Chromium: typing 2 then p then 47 gives 14:47; up arrow 15:00; down twice 14:30; 10:10a, 1430 and 16 all parse; Enter doesn't submit; arrow keys step; quick pick 11:00 still works; a booking saved with 3:25p stores 15:25; no JS errors.
- **Patch:** _patches/45-type-the-time.patch (apply after 01-44)

## 46 - Scheduling: "Intro Flight" preset (1-hr blocks) (2026-09-24)
- **Sync first:** shopinv_v10 unchanged since 45's merge.
- Under Duration on the Schedule Flight form there's a Preset toggle: Lesson (1.5 hr) or Intro Flight (1 hr). Intro Flight sets the duration to 1, starts the notes with "Intro flight" (so it's easy to spot in the booking's details), and swaps the quick picks to a 1-hour grid on the hour (8:00a-7:00p). Lesson puts back 1.5 hr, the usual 1.5-hr quick picks, and removes the "Intro flight" note it added. Quick picks now set the duration of whichever grid they belong to.
- Editing a booking highlights the matching preset (notes starting "Intro flight" -> Intro; 1.5 hr -> Lesson).
- **Files:** templates/flight/schedule_form.html (builds on 45's version of the same file)
- **Tested:** headless Chromium: Lesson is on by default; Intro -> 1 hr, note added, hourly grid; quick pick 1:00p -> 13:00 at 1 hr; back to Lesson -> 1.5 hr and note removed; an intro booking saves as 13:00 / 1.0 hr / "Intro flight" and reopens with Intro highlighted; no JS errors.
- **Patch:** _patches/46-intro-flight-preset.patch (apply after 01-45)

## 47 - Logbook: sticker starters from projects, templates, and a searchable logbook per plane (2026-09-24)
- **Sync first:** shopinv_v10 had new edits from the other chat (CFI gender field, Availability view, Flight Finder in flight.py, db.py, schema.sql, cfi_form.html, _schedule_live.html, plus new availability.html and finder.html). Merged them into the queue by re-applying queue changes 43-46 on top of v10's copies; no conflicts. _synced/ now records the sync point.
- **Logbook Starter** (new button on every project): one draft entry per sub area - "Engine: Replaced Valve cover gasket using P/N LW-13387 (qty 2)." with the return-to-service endorsement and a signature line - plus a combined entry for the whole project from the template for its type (Annual, 100-Hour, Oil Change, Repair). Type is guessed from the maintenance item the project completed or its name, and can be changed. Date, tach and Hobbs default from the plane. Each draft goes to the Airframe, Engine or Propeller log (guessed from the sub area name; changeable). Edit, then Save to logbook or Copy.
- **Part Number** (new optional field on the part form) feeds the P/N; blank = the barcode, unless it's a generated SHOP- barcode (then a blank line to fill in).
- **Templates** (Manage > Logbook > Templates, admins): one per project type x log, type/paste or upload a .txt; {placeholders} like {tail}, {tach}, {work}, {parts}, {engine_serial}. Blank = built-in wording.
- **Manage > Logbook** (admins and techs): search by N number, log, date range, project, or text. Each plane's profile has a Logbooks card (Airframe / Engine / Prop counts) opening a compact, year-grouped, newest-first list with tap-to-expand entries. Each entry can be edited (plus signed-by / cert #), deleted (admins), and printed as a ~5.5in sticker.
- **Database:** new logbook_entries and logbook_templates tables and parts.part_number column; created automatically on restart (db.py migration, schema.sql). No new pip packages.
- **Files:** new logbook.py (blueprint) and 6 new templates (logbook_*.html); app.py (2 lines to register + part number on new/edit part), db.py, schema.sql, templates/base.html, project_detail.html, asset_detail.html, part_form.html.
- **Tested:** scratch database with a plane, 4 parts, sub areas Engine/Brakes/Propeller + untagged oil: starters, all 4 project types, save, search filters, plane tabs, edit, print, template save/upload/reset, delete, tech permissions, part number save; migration run twice; predeploy check passes.
- **Patch:** _patches/47-logbook-starter.patch

## 48 - Flight Academy: pilot logbook, part dual/part solo, and progress toward every certificate (2026-09-24)
- **Sync first:** shopinv_v10 matched the queue (changes 43-47 already copied in); no merge needed. _synced/ refreshed.
- **Pilot logbook (Academy > Logbook):** every logged flight (Log Flight, Schedule's "Flight Already Complete", or ending a running flight) creates a pending entry in the student's logbook: date, plane (N number + make/model), total time from Tach (Hobbs if no Tach), dual received / solo / PIC, landings, and the instructor's name with their CFI certificate # and expiration. The student opens it, adds route, cross-country, night, actual/simulated instrument, approaches, PIC and remarks, then approves it. Totals across the top, pending ones highlighted, a badge on the tab. Instructors see any student (picker) and their own total dual given. Export to CSV. On first restart every flight already logged gets a pending entry so history carries over (runs once).
- **Part dual, part solo:** new "Solo Portion (hrs)" on Log Flight, Edit Flight, Schedule's complete mode and the End Flight form. That part logs as solo/PIC instead of dual and is not billed for the instructor (plane still billed for the whole flight). Bookings get a "Part dual, part solo" checkbox; the End Flight form shows a "booked dual + solo" tag. Flight detail shows the solo portion.
- **CFI profile:** Flight Instructor Certificate # and expiration (printed on logbook entries).
- **Progress (Academy > Progress):** First Solo (61.87), Sport (61.313), Recreational (61.99), Private (61.109), Instrument (61.65), Commercial (61.129), Multi-Engine add-on (61.63), CFI (61.183), ATP (61.159). Each has a shimmering overall % bar and every requirement as a line with its 14 CFR reference, a green check when met, its own bar and hours (e.g. 3.2 / 10 hrs), and the flights that count toward it. Hour/landing items total from the logbook automatically (dual XC, solo XC, night dual, instrument dual, PIC XC etc. worked out per entry; "within 2 calendar months" items only count recent flights); knowledge tests, the long XCs, towered landings and endorsements are checked off by an instructor (with date/note); First Solo uses the solo sign-off on the student's profile; CFI uses the certificate/ratings on the profile. The track after the student's current certificate opens first.
- **Database:** new pilot_logbook and academy_milestones tables; new columns cfis.cfi_cert_number/cfi_cert_expires, flights.solo_hours, scheduled_flights.part_solo. All created automatically on restart (db.py). No new pip packages.
- **Files:** new pilotlog.py (blueprint) and templates _academy_tabs.html, academy_base.html, academy_logbook.html, academy_logbook_entry.html, academy_progress.html; app.py (register), db.py, schema.sql, flight.py, templates/academy.html, flight/cfi_form.html, log_new.html, log_active.html, schedule_form.html, log_detail.html.
- **Tested:** migration on a copy of the current database shape with 2 old flights (backfilled correctly, second restart doesn't duplicate); logging a 2.0-hr Hobbs / 1.6 Tach flight with 0.6 solo -> logbook 1.6 total / 1.0 dual / 0.6 solo, charge $384 (plane 2.0 x $150 + instructor 1.4 x $60); editing the solo part re-bills and refreshes the pending entry; part-solo booking saved/cleared; start -> end flow creates the entry; student completes + approves (times locked for students), can't edit after approving, other students get 404; instructor milestone check-off; CSV export; deleting a flight removes its pending entry; CFI cert fields save; predeploy check passes.
- **Patch:** _patches/48-pilot-logbook-progress.patch (apply after 47)

## 49 - Schedule without a student profile (Guest / Intro), Intro preset defaults to it (2026-09-24)
- **Sync:** while this was being built the other chat changed shopinv_v10 (Availability Week/Month views, Flight Finder female/male instructor filter: flight.py, availability.html, finder.html). Merged into the queue with a 3-way merge (no conflicts) and _synced/ refreshed; availability/finder pages re-tested.
- **Guest / Intro:** the student picker (Schedule Flight, Edit Booking, Log Flight, Edit Flight) has a new first choice, "Guest / Intro - no profile (one-time flyer)", with optional name and phone boxes. Behind it is one shared placeholder student (created automatically on restart: a station-type account with no login, not on the leaderboard). The name is stored on the booking and carried onto the flight (started from the booking, logged after the fact, or entered with Flight Already Complete), and shows everywhere the student name would, as "Jane Doe (guest)" - calendar views, schedule list, Active Flight, Flight History, alerts, conflict messages.
- **No false conflicts:** several guest bookings at the same time (different planes/instructors) don't count as the same student double-booked. Plane and instructor conflicts still apply.
- **Intro Flight preset** now picks Guest / Intro automatically when no student has been chosen yet (a real student you already picked is kept).
- **Billing:** a guest flight is charged to the placeholder account like any other flight (collect at the desk); its logbook entry stays on that account, which never shows in the Academy.
- **Database:** new columns scheduled_flights.guest_name/guest_phone and flights.guest_name, plus the placeholder student; all added automatically on restart (db.py). No new pip packages.
- **Includes 48:** flight.py, db.py and schema.sql build on change 48 (Flight academy), so this idea's copy steps list 48's files too; copying either one brings both.
- **Files:** flight.py, db.py, schema.sql, templates/flight/schedule_form.html, templates/flight/log_new.html.
- **Tested:** migration twice (one placeholder); two guest intros at the same time on different planes both book; a real student double-booking is still caught; guest names on day/month calendar, history, detail, active board; edit booking keeps/updates name + phone; log from booking copies the name; Flight Already Complete with a guest; start -> active flow; browser check that Intro selects Guest only when no student is picked and the name boxes show/hide; predeploy check passes.
- **Patch:** _patches/49-guest-intro-bookings.patch (apply after 48; flight.py hunks are against the pre-merge file)

## 50 - After scheduling, open the Month view with the new booking highlighted (2026-09-24)
- **Sync:** the other chat removed the time-entry hint line in shopinv_v10's schedule_form.html while this was being built; merged into the queue (3-way, no conflicts) and _synced/ refreshed.
- After Schedule Flight saves, the Schedule opens on the **Month** view for that booking's month with the new booking outlined in a pulsing green (a repeating series highlights every booking it created, on the first one's month) and scrolled into view. It also works in the Day/Week/List views if you switch.
- The highlight **goes away on your first click anywhere, or when you leave the page**. The `new` marker is taken out of the address, so a reload, the 15-second live refresh after that click, or coming Back won't bring it back. Busy days show every block while something is highlighted (no "+N more" hiding it).
- Student flight requests (pending approval) still go back to the student's dashboard as before. A booking that hit a conflict still shows the red conflict view.
- **Includes 48 and 49:** flight.py builds on them, so the copy steps list their files too.
- **Files:** flight.py (schedule_new redirect + new_ids), templates/flight/_schedule_live.html, templates/flight/schedule.html. No database change.
- **Tested:** single booking redirects to ?view=month&...&new=<id>; weekly series -> new=<all ids>; browser: 1 green block on load, 0 after a click with the param gone from the URL, 0 after reload; predeploy check passes.
- **Patch:** _patches/50-highlight-new-booking.patch (apply after 49)

## 51 - Dashboard: All / Mine toggle for Today's Schedule, Upcoming and Recent Flights (2026-09-24)
- **Sync first:** shopinv_v10 unchanged since 50's merge.
- CFIs get an **All | Mine** switch at the top of the dashboard's schedule card. **All** (the default) is the whole school, as before. **Mine** narrows Today's Schedule (and its "X of Y complete" count), Upcoming, the next-lesson countdown and Recent Flights to flights you're the instructor on, and Recent Flights is then titled "Your Recent Flights".
- The choice is remembered for your login (session), so the 15-second live refresh and coming back to the dashboard keep it. Students' dashboards are unchanged (no toggle).
- **Includes 48-50:** flight.py builds on them, so the copy steps list their files too.
- **Files:** flight.py (_dashboard_context, dashboard), templates/flight/_dashboard_live.html. No database change.
- **Tested:** two CFIs with bookings today/upcoming and a logged flight: All shows both, Mine only your own, live refresh keeps Mine, switching back to All restores both; student dashboard has no toggle; predeploy check passes.
- **Patch:** _patches/51-dashboard-all-mine.patch (apply after 50)

## 52 - Students: landing currency only after first solo (2026-09-24)
- **Sync first:** shopinv_v10 unchanged since 50's merge.
- New **"Has completed first solo"** checkbox (plus First Solo Date, today if left blank) on the student profile (Students > Add/Edit), next to the solo sign-off. Changes are recorded in the field history.
- Until it's ticked, landing currency is hidden: the profile's Last landing / Last 90 days boxes become a "shows once first solo is marked complete" note, and the Students list shows "Pre-solo" instead of last landing / 90-day landings (phone and desktop). Station accounts are unaffected.
- On first restart, students who already have a solo flight logged are marked as past first solo (dated by their earliest solo flight), so their landing currency doesn't disappear. Runs once. Untick it on anyone who shouldn't have it.
- **Database:** new students.first_solo_date column (added automatically on restart).
- **Includes 48-51:** flight.py/db.py/schema.sql build on them, so the copy steps list their files too.
- **Files:** flight.py, db.py, schema.sql, templates/flight/student_form.html, templates/flight/students.html.
- **Tested:** migration twice (student with a solo flight gets its date; one without stays pre-solo); list shows Pre-solo; ticking with a blank date sets today and the landing boxes appear; unticking clears it; Add Student with a date saves it; predeploy check passes.
- **Patch:** _patches/52-landing-currency-after-first-solo.patch (apply after 51)

## 53 - Schedule Flight: type to find a student (2026-09-24)
- **Sync first:** shopinv_v10 now matches the queue exactly (changes 47-52 copied over), so nothing to merge. _synced/ refreshed.
- A search box ("Type to find a student...") sits above the Student list on Schedule Flight / Edit Booking (also used by Flight Already Complete). Each character narrows the list, matching anywhere in the name or at the start of any word. Only one match? It's picked for you. Enter picks the first match. No match turns the box red. Guest / Intro and the student already picked always stay in the list.
- Rebuilds the list rather than hiding options, so it works on iPhone/iPad Safari too. Nothing changes on the server.
- **Files:** templates/flight/schedule_form.html only.
- **Tested (browser):** 4 extra students: "sa" -> 4 matches, "sam" -> Sam + Samantha, "mi" -> Mike Sanders + Sarah Miller, Enter picks Mike, "zzz" -> red box, "bob" -> Bob Jones auto-picked; predeploy check passes.
- **Patch:** _patches/53-student-search-on-schedule.patch

## 54 - New booking: gray out the screen except the new booking (requeue of 50: "highlight not really noticeable - gray out the screen except the new addition, gone on first click or scroll") (2026-09-24)
- **Sync first:** shopinv_v10 unchanged since 53.
- **What changed from 50:** instead of just a green glow, the Schedule now opens with the whole screen grayed out (a dark layer over everything) except for the new booking(s), which show through a cut-out with the green outline. A pill at the bottom says "Your new booking - tap anywhere to continue". A repeating series spotlights every new booking visible on the page.
- **Clears on the first click, scroll, swipe or key press** (and on leaving the page), fading out in a quarter second. The address loses its `new` marker at the same time, so reload/Back/live refresh don't bring it back. The automatic scroll to the booking doesn't count as your scroll.
- Month view is still where it lands (unchanged from 50). The cut-outs follow window resizes.
- **Files:** templates/flight/schedule.html only.
- **Tested (browser):** after booking -> overlay + label shown with a cut-out exactly around the new block; mouse-wheel scroll clears it and strips the param; a click clears it; a key press clears it; predeploy check passes.
- **Patch:** _patches/54-spotlight-new-booking.patch (apply after 50)

## 55 - Schedule: no booking in the past (2026-09-24)
- **Sync first:** shopinv_v10 unchanged since 53.
- Booking a flight for a date/time that has already passed now stops with an error: "<date> at <time> is in the past - a flight can't be booked before now. If it already happened, tick "Flight Already Complete?" and log it with its date and time." Earlier today counts as past (with a 5-minute grace for "right now"); today with no time set is fine.
- Applies to Schedule Flight (including the first date of a repeating series and students' own requests), Edit Booking when the date/time is changed into the past (fixing notes etc. on a booking whose time has passed still works), the quick Reschedule, and approving a student request whose time has already gone by (edit or deny it instead).
- **Flight Already Complete?** still takes any past date - that's the way to enter past flights.
- The date picker on Schedule Flight starts at today (lifted when Flight Already Complete is ticked, and not applied when editing an existing booking).
- **Files:** flight.py, templates/flight/schedule_form.html. No database change.
- **Tested:** yesterday / earlier today / repeating series starting yesterday -> error; today with no time and tomorrow -> booked; Flight Already Complete for yesterday -> logged; reschedule into the past refused; edit of a past booking without moving it -> saved, moving it within the past -> refused; approving a past request refused; predeploy check passes.
- **Patch:** _patches/55-no-past-bookings.patch

## 56 - CFI profiles: admins see all, CFIs see only their own, students none (2026-09-24)
- **Sync first:** shopinv_v10 unchanged since 53.
- **Checked the existing access (no change needed):** Manage > CFIs, New CFI and Edit CFI (any CFI) are master-admin only; the old /admin/cfis pages are admin-only redirects; a CFI's Pay page opens only for that CFI or an admin; students can't open any of these, and the CFIs menu item only shows for admins. Tested every route as admin, CFI and student.
- **New: My CFI Profile** (Manage menu, for CFIs) - their own profile only: username, instruction rate, their pay rate, credentials, medical, schedule color (read-only, "set by an admin"), plus their Flight Instructor Certificate # and expiration, which they can update themselves (logged in the field history). Rates/name/credentials still only change through an admin. Pay page's back button goes to My CFI Profile for a CFI.
- **Includes 55's server check:** flight.py also carries change 55 (no past bookings).
- **Files:** flight.py (cfi_me route), new templates/flight/cfi_me.html, templates/flight/base_flight.html, templates/flight/cfi_pay.html. No database change.
- **Tested:** access matrix (admin/CFI/student x CFIs list, new, edit own/other, admin pages, pay own/other, my profile); a CFI posting extra fields (name, rate) to My CFI Profile only changes the certificate fields; predeploy check passes.
- **Patch:** _patches/56-cfi-profile-access.patch

## 57 - CFI signature pad, signature on logbook entries (2026-09-24)
- **Sync:** the other chat wrapped the labor table in project_detail.html in a scrolling box in shopinv_v10; the queue hadn't touched that file, so v10's copy was brought in and _synced/ refreshed. Not part of this change's files.
- **My CFI Profile:** a signature box under the certificate # - draw with a finger, stylus or mouse, Clear to start over, Save. Stored as a PNG. A new signature also goes onto that CFI's students' **pending** logbook entries; approved entries keep the one they were signed with.
- **Logbook entries:** every new entry with dual time copies the instructor's signature at that moment, and it shows under the instructor's name and certificate # on the entry. If the CFI hasn't signed yet it says so.
- **Admins (Edit CFI):** see the signature and can remove it (only the CFI can draw one). Changes are logged in the field history.
- Server checks it's a real PNG and not oversized; anything else is ignored.
- **Database:** cfis.signature, cfis.signature_updated_at, pilot_logbook.instructor_signature (added automatically on restart).
- **Includes 55 and 56:** flight.py carries them and My CFI Profile comes from 56, so its files are in the copy list.
- **Files:** db.py, schema.sql, flight.py, pilotlog.py, new templates/flight/_signature_pad.html, templates/flight/cfi_me.html, cfi_form.html, base_flight.html, cfi_pay.html, templates/academy_logbook_entry.html.
- **Tested (browser):** drew a signature -> saved (about 5 KB), pad shows the saved one, pending entry picked it up and shows it; a fake PNG is ignored; Clear posts a removal; admin removal works; migration twice; predeploy check passes.
- **Patch:** _patches/57-cfi-signature.patch (apply after 56)

## 58 - Pilot logbook uses Hobbs time, not Tach (2026-09-24)
- **Sync first:** shopinv_v10 unchanged since 57's sync.
- A logbook entry's total time now comes from the flight's **Hobbs** readings (Tach only when no Hobbs readings were entered). Dual/solo/PIC split the same way from that total. The entry page says "Total (Hobbs)".
- On restart, entries still **pending** are re-figured once from Hobbs; entries a student already approved are left exactly as they were (edit or reopen them if needed).
- Billing, the leaderboard and currency already used Hobbs - unchanged.
- **Includes 55-57:** db.py/pilotlog.py/flight.py build on them (signature, My CFI Profile, no past bookings), so the copy list is the same as 57's.
- **Files:** pilotlog.py, db.py, templates/academy_logbook_entry.html (+ 57's files, see steps).
- **Tested:** a pending entry made from Tach (1.2) became 1.5 (Hobbs) after restart, an approved one stayed 0.8; a new flight with 2.0 Hobbs / 1.6 Tach logs 2.0; runs once; predeploy check passes.
- **Patch:** _patches/58-logbook-hobbs-time.patch (apply after 57)

## 59 - Schedule Flight: one box to search and pick the student (2026-09-24)
- **Sync first:** shopinv_v10 now matches the queue exactly (47-58 copied over); _synced/ refreshed.
- Replaces 53's separate search box + list. The Student field is now a single box: click or tap it and the whole list drops down right under it; start typing and the list narrows with every character (matching anywhere in the name, the matching letters highlighted). Tap a name to pick it, or use the arrow keys and Enter (Enter picks the top match). Escape or clicking away puts back the current pick. The arrow button on the right opens the full list.
- Guest / Intro stays at the top of the list. The Intro preset still switches the box to Guest when no student is picked.
- Saving with no student picked now outlines the box in red and keeps you on the form instead of a hidden browser message. The real student list is still what gets submitted, so nothing changed on the server.
- **Files:** templates/flight/schedule_form.html only.
- **Tested (browser):** open shows all; "sa"/"sam" narrow as typed; Enter picks Sam and closes; clicking back in selects the text so typing replaces it; clicking "Sarah Miller" picks her; "zzz" shows "No student matches"; Escape restores; a real booking saves with the picked student; empty student is blocked with a red box; Intro -> Guest; predeploy check passes.
- **Patch:** _patches/59-student-combo-picker.patch

## 60 - Signature box you can actually see and use, also on Edit CFI (requeue of 57: "I see text to sign in cfi profile for saving signature but no place or actual option to do so") (2026-09-24)
- **Sync first:** shopinv_v10 matches the queue (57-59 copied over); nothing to merge.
- **What changed from 57:** in 57, **Edit CFI** (Manage > CFIs, where an admin looks at a CFI's profile) only showed text saying the CFI signs on their own My CFI Profile - no box. Now Edit CFI has the signature box too, so you can sign right there. My CFI Profile still has it for CFIs signing themselves.
- The box itself is now unmistakable: a white area with a dashed border, "Sign here" and a signature line drawn inside, 170px tall and full width (up to 520px). Its styling is built in, so it shows on every page and phone. Clear resets it (and removes a saved one on Save).
- Saving from Edit CFI stores the signature (logged in the field history) and puts it on that CFI's pending logbook entries, same as signing on My CFI Profile.
- **Files:** flight.py (cfi_edit), templates/flight/_signature_pad.html, templates/flight/cfi_form.html, templates/flight/cfi_me.html. No database change.
- **Tested (phone-size browser, 390px, touch):** Edit CFI shows the dashed box; drawing in it and saving stores the signature; predeploy check passes.
- **Patch:** _patches/60-signature-box-visible.patch (apply after 57)
