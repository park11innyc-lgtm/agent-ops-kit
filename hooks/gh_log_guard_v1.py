#!/usr/bin/env python3
"""PreToolUse(Bash): block gh commands that can fetch Actions logs or tokens.

Closes gaps the permissions.deny globs leave: case variants, quoting, bash -c, shell
variables, brace/glob expansion, aliases. Exit 2 = block. Malformed input fails open
(never breaks Bash).

This is a tripwire on the command text, not a sandbox: a command can still write a
script file and run it later. Pair it with a gh token that has only the scopes your
agents need.
"""
import json, re, sys

try:
    cmd = (json.load(sys.stdin).get("tool_input") or {}).get("command") or ""
except Exception:
    sys.exit(0)

raw = cmd
cmd = re.sub(r"[\"'\\]", "", cmd)  # shell strips quotes/backslashes: gh auth "token" == gh auth token
flat = re.sub(r"[{}\[\]*?,]|\.\.", "", cmd)  # brace/glob forms collapse: g[h] -> gh, toke{n..n} -> token

# a globbed or brace-built command name (`/opt/homebrew/bin/g?`, `{g,}h`) can resolve to gh: block it outright
for name in re.findall(r"(?:^|[;&|(\n]|\$\()\s*(?:\w+=\S*\s+)*([^\s;&|()]+)", raw):
    if name.startswith("[") and not re.search(r"\w", name):
        continue  # [ and [[ test builtins
    if re.search(r"\w", name) and re.search(r"[?*]|\[[^\]]*\]|(?<!\$)\{", name):
        print("gh_log_guard_v1 blocked: globbed or brace-expanded command name", file=sys.stderr)
        sys.exit(2)

# case-insensitive: on a case-insensitive filesystem (macOS default) `GH` runs gh
if not re.search(r"(?<![\w.-])gh(?![\w.-])", flat, re.I):
    sys.exit(0)

reasons = []
if re.search(r"logs?\b|--log", flat, re.I):
    reasons.append("log fetch")
if re.search(r"\bauth\s+(token|git-credential|status\b.*(\s-[a-z]*t[a-z]*\b|--show-token))", flat, re.I):
    reasons.append("token read")
if re.search(r"\brun\s+download\b", flat, re.I):
    reasons.append("run download")
if re.search(r"\balias\s+(set|import)\b", flat, re.I):
    reasons.append("alias definition")
# any expansion in a command that mentions gh can assemble a blocked subcommand at run time
# (`p=auth; gh "$p" token`, `x=gh; $x ...`, `toke{n..n}`), so such a command is blocked; pass literal arguments instead
if re.search(r"[$`{}\[\]*?]|\\x|<\(", raw) or re.search(r"base64|\beval\b|\bsource\b|\bxargs\b", cmd, re.I):
    reasons.append("gh with shell expansion or indirection")

if reasons:
    print("gh_log_guard_v1 blocked: " + ", ".join(reasons), file=sys.stderr)
    sys.exit(2)
sys.exit(0)
