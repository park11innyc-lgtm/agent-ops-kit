#!/usr/bin/env python3
"""Review every production unit in settings `units_file` once, one at a time.

units_file: {"units": [{"unit": "name", "project": "id", "path": "~/code/app/src"}]}
Resumable and idempotent: units that already have a review in the ledger are skipped. The run stops at the
daily cap or under swap pressure (codex_review_v1 enforces both), so the next run continues where it left off.
Run it daily from cron or launchd.
"""
import json, os, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
S = json.load(open(os.environ.get("CODEX_REVIEW_SETTINGS") or HERE / "settings_v1.json"))
RD = Path(os.path.expanduser(S["reviews_dir"]))
units = json.load(open(os.path.expanduser(S["units_file"])))["units"]
ledger = json.load(open(RD / "ledger.json"))["units"] if (RD / "ledger.json").exists() else {}

todo = [u for u in units if u["unit"] not in ledger]
print(f"backfill: {len(units) - len(todo)}/{len(units)} reviewed, {len(todo)} to go", flush=True)
for u in todo:
    path = os.path.expanduser(u["path"])
    if not os.path.exists(path):
        print(f"skip {u['unit']}: missing {path}", flush=True); continue
    r = subprocess.run([sys.executable, str(HERE / "codex_review_v1.py"), "review", path,
                        "--unit", u["unit"], "--project", u["project"]], capture_output=True, text=True)
    msg = (r.stdout or r.stderr).strip().splitlines()
    print(msg[-1] if msg else f"{u['unit']}: rc={r.returncode}", flush=True)
    if r.returncode and any(k in (r.stderr or "") for k in ("daily cap", "swap", "disabled")):
        print("backfill paused; next run continues", flush=True); break
