"""Preview-copy tooling for OpsHub (never imported by app.py itself).

run_preview.py imports the normal app, then calls init(app, ...) here to:
  * switch off everything that could reach the outside world (email, texts,
    phone alerts, Wave, label printer, reboot/restart commands), because a
    preview runs on a COPY of the live data,
  * add a top banner, highlight the elements each change touched, and
  * add a side-by-side page (before on the left, preview on the right).
The live app never loads this package, so none of it can affect the live site.
"""
from .safety import neutralize  # noqa: F401
from .overlay import init  # noqa: F401
