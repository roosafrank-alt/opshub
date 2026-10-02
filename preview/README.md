# OpsHub preview copy

Everything in this folder (and run_preview.py at the repo root) is only for a PREVIEW copy of OpsHub that runs next to the live app on its own port with a copy of the data. The live app never imports it.

- `python3 run_preview.py --tree <folder> --port 5051 --label preview` runs the new code, changes outlined in pink.
- `python3 run_preview.py --tree <folder-with-main> --port 5052 --label before` runs today's code for the "before" side.
- `/__preview/compare` shows before and after next to each other, `/__preview/all` lists every change, `/__preview/outbox` shows what was blocked.
- Builders mark changed elements with `data-change="ID"` and list each change in `preview/changes/<part>.json`.
