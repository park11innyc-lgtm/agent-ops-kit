#!/usr/bin/env python3
"""Stop hook: block end-of-turn while browser tabs Claude opened are still open.

Scans the session transcript for tab-creating calls (claude-in-chrome tabs_create_mcp,
Claude_Browser tabs_create) and tab-closing calls. If creates > closes, the turn is
blocked with an instruction to close them. stop_hook_active prevents an infinite loop.
Log: $TAB_GUARD_LOG, default ~/.agent-ops/logs/tab_guard.log
"""
import json, sys, os, time

CREATE = ("mcp__claude-in-chrome__tabs_create_mcp", "mcp__Claude_Browser__tabs_create")
CLOSE = ("mcp__claude-in-chrome__tabs_close_mcp", "mcp__Claude_Browser__tabs_close")
LOG = os.path.expanduser(os.environ.get("TAB_GUARD_LOG") or "~/.agent-ops/logs/tab_guard.log")


def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    if data.get("stop_hook_active"):
        return  # already nudged once this turn; don't loop
    path = data.get("transcript_path")
    if not path or not os.path.exists(path):
        return
    created = {"chrome": 0, "app": 0}
    closed = {"chrome": 0, "app": 0}
    with open(path, errors="ignore") as f:
        for line in f:
            if "tabs_c" not in line:
                continue
            try:
                content = json.loads(line).get("message", {}).get("content", [])
            except Exception:
                continue
            if not isinstance(content, list):
                continue
            for c in content:
                if not isinstance(c, dict) or c.get("type") != "tool_use":
                    continue
                name = c.get("name", "")
                kind = "chrome" if "claude-in-chrome" in name else "app"
                if name in CREATE:
                    created[kind] += 1
                elif name in CLOSE:
                    closed[kind] += 1
                elif name.endswith("browser_batch"):
                    for a in (c.get("input") or {}).get("actions", []) or []:
                        n = (a or {}).get("name", "")
                        if n.startswith("tabs_create"):
                            created[kind] += 1
                        elif n.startswith("tabs_close"):
                            closed[kind] += 1
    open_chrome = created["chrome"] - closed["chrome"]
    open_app = created["app"] - closed["app"]
    if open_chrome <= 0 and open_app <= 0:
        return
    parts = []
    if open_chrome > 0:
        parts.append(f"{open_chrome} tab(s) in the user's real Chrome (claude-in-chrome)")
    if open_app > 0:
        parts.append(f"{open_app} in-app Browser tab(s)")
    reason = ("Tab hygiene: you opened " + " and ".join(parts) + " this session and haven't closed them. "
              "Run tabs_context, then close every tab YOU created (tabs_close_mcp / tabs_close) so the "
              "Claude tab group disappears. Don't close the user's own tabs. If the user explicitly asked to "
              "keep a page open, say so in one line and stop.")
    sid = data.get("session_id")
    if not sid:
        tp = data.get("transcript_path") or ""
        base = os.path.basename(tp)
        sid = base[:-6] if base.endswith(".jsonl") else "unknown"
        sid = sid or "unknown"
    log(f"block session={sid} chrome={open_chrome} app={open_app}")
    print(json.dumps({"decision": "block", "reason": reason}))


if __name__ == "__main__":
    main()
