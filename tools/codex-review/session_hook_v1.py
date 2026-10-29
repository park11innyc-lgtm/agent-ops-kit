#!/usr/bin/env python3
"""SessionStart hook: tell the agent about open review findings for the current folder's project.

Maps the session's cwd to a project through settings `projects` ({id: [folders]}, longest match wins).
Prints one line, or nothing when there are no open findings. Never blocks a session start: any error exits 0 silently.
"""
import json, os, subprocess, sys
from pathlib import Path

try:
    cwd = json.load(sys.stdin).get("cwd") or os.getcwd()
except Exception:
    cwd = os.getcwd()
try:
    here = Path(__file__).resolve().parent
    S = json.load(open(os.environ.get("CODEX_REVIEW_SETTINGS") or here / "settings_v1.json"))
    cwd = os.path.realpath(cwd)
    best, proj = "", None
    for pid, dirs in S.get("projects", {}).items():
        for d in dirs:
            d = os.path.realpath(os.path.expanduser(d))
            if d != "/" and (cwd == d or cwd.startswith(d + os.sep)) and len(d) > len(best):
                best, proj = d, pid
    args = [sys.executable, str(here / "codex_review_v1.py"), "status", "--brief"]
    if proj:
        args += ["--project", proj]
    out = subprocess.run(args, capture_output=True, text=True, timeout=10).stdout.strip()
    if out:
        print(out)
except Exception:
    pass
sys.exit(0)
