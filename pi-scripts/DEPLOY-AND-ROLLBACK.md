# Deploy backup + rollback contract

This describes how "Deploy to Pi" and the "Roll back last deploy" button on
the Idea Queue artifact are meant to work together, so whoever builds the
auto-deploy webhook runner (see the OpsHub auto-deploy plan — queued ideas
get coded, pushed to `idea-queue`, then deployed to the Pi automatically)
knows what it needs to read and write.

## What exists today

- **`opshub-predeploy-backup.sh`** — run on the Pi before every rsync. Tars
  up the current live app dir to `~/opshub-backups/backup-<timestamp>.tar.gz`
  and prunes old ones (keeps the last 10). The "Deploy to Pi" step generated
  by the Idea Queue artifact (`withChecks()`) already calls this over ssh
  right before the rsync, so every deploy from here on has a backup to
  undo to. It's a no-op (exit 0) if there's no app dir yet, so a first
  install is unaffected.
- **`opshub-rollback.sh`** — run on the Pi (manually, or by the future
  runner). Restores the newest `backup-*.tar.gz`, stops/starts the `opshub`
  systemd service, and checks the app answers on port 5050. It also backs
  up whatever was live right before restoring, as `pre-rollback-*.tar.gz`,
  so a rollback is itself undoable.
- **The artifact's "Roll back last deploy" button** (Idea Queue, Summary
  tab, next to Deploy to Pi) — only shown once a deploy has actually gone
  live (`meta/deploy.doneAt` set). On click + confirm, it writes to the
  same Firestore-style doc the deploy button uses:

  ```js
  db.doc("meta/deploy").set({
    ...existing fields kept...,
    rollbackRequestedAt: <ISO timestamp>,
    rollbackStatus: "requested",
    rollbackMessage: "",
  })
  ```

## What the runner still needs to do (not built yet)

Whatever process watches `meta/deploy` for `status:"requested"` and runs
the deploy (per the OpsHub auto-deploy plan) should also watch for
**`rollbackStatus:"requested"`** and, when it sees it:

1. SSH to the Pi and run `~/pi-scripts/opshub-rollback.sh`.
2. On success, write back:
   ```js
   { rollbackStatus: "done", rollbackDoneAt: <ISO timestamp>, rollbackMessage: "" }
   ```
3. On failure (non-zero exit, or the app doesn't come back up), write back:
   ```js
   { rollbackStatus: "failed", rollbackMessage: "<short reason, e.g. last few lines of the script's output>" }
   ```
   (leave `rollbackDoneAt` unset so the artifact keeps showing the button as
   available to retry)

The artifact already renders all three states (`requested` / `done` /
`failed`) — see `renderDeploy()`'s rollback status block — so no artifact
changes are needed once the runner exists; it just needs to write the
fields above to the same `meta/<docId>` doc (`deploy` for opshub; see
`deployDocId()` for other apps, though only opshub's deploy step currently
makes a backup to roll back to).

## Rolling back by hand, right now

Until the runner exists, rolling back is a manual SSH step:

```bash
ssh <user>@opshub.taila1bcc5.ts.net
~/pi-scripts/opshub-rollback.sh
```

(First time: copy both scripts in this folder to `~/pi-scripts/` on the Pi
and `chmod +x` them, same as the uptime script's install note.)
