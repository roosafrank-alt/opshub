# Idea Queue batching: before/after comparison

Batching went live on 2026-09-28 at about 01:05 UTC (main commit 25a0a34).
The instructions before and after are saved next to this file.
The one-week check is scheduled for 2026-10-05.

## Baseline (before batching)

The queue has only been running since 2026-09-26, so the baseline covers
2026-09-26 02:32 UTC to 2026-09-28 01:05 UTC (about 2 days), not a full week.

- Ideas finished: 88 (14 on Sep 26, 73 on Sep 27, 1 on Sep 28)
- Time per idea (startedAt to finishedAt): median 5.5 min, mean 5.8 min
- Total runner working time on ideas: 8.5 hours
- Ideas that needed more than one attempt: 3. Failed: 1
- meta/runner document version at go-live: 1067 (every write to it, such
  as heartbeats and notes, adds 1)

## What cannot be measured here

Claude token usage per run isn't recorded anywhere the runner can read.
The numbers above stand in for it. Frank's usage page on claude.ai is
the direct measure.

## How the after numbers are measured

Same method, on ideas finished from 2026-09-28 01:05 UTC to 2026-10-05:
count, median and mean minutes per idea, total hours, retries, failures.
Also count how many runs stopped right away with "Queue empty. Checked at"
and how many batches ran ("in a batch" in results), plus meta/runner
writes per day (its version on 2026-10-05 minus 1067, divided by 7).

## Parallel waves (added 2026-09-28)

After batching, the runner was changed to work up to 4 pending ideas at the
same time (one helper agent each), instead of one after another. The
instructions are saved as runner-instructions-2026-09-28-after-parallel.md.
Ideas finished after it went live count toward the "after" numbers too, but
time per idea (startedAt to finishedAt) no longer shows the speed-up, since
ideas in a wave overlap. To see it, compare how long the queue took to empty
after Frank added several ideas at once: the first startedAt to the last
finishedAt of each wave. Related ideas still say "Built in a batch with".
