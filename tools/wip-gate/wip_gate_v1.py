#!/usr/bin/env python3
"""WIP gate: cap how many PRs agents may have awaiting human review per project.

  wip_gate_v1.py check <project-id>    exit 0 ok / 3 over the limit (only when enforce=true; otherwise WARN, exit 0)
  wip_gate_v1.py collect [--board]     refresh the PR snapshot from GitHub (gh CLI); --board prints a text board

Agents run `check` before spawning any worker that will open a PR. An over-limit project means: batch-approve,
merge or close first, and tell the human instead of spawning. A GitHub read failure never blocks (it reports UNKNOWN).
Counted PRs = open, not draft (unless count_drafts), not already approved (unless exclude_approved is false).
Settings: settings_v1.json next to this file, or WIP_GATE_SETTINGS. Stdlib + gh only.
"""
import json, os, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_settings():
    return json.load(open(os.environ.get("WIP_GATE_SETTINGS") or HERE / "settings_v1.json"))


def gh_json(args):
    try:
        r = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=60)
        return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None
    except Exception:
        return None


def ci_of(roll):
    if not roll: return "none"
    st = [(c.get("conclusion") or c.get("state") or c.get("status") or "").upper() for c in roll]
    if any(s in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED") for s in st): return "fail"
    if any(s in ("PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED", "") for s in st): return "pending"
    return "pass"


def prs_for(repo, gh=gh_json):
    l = gh(["pr", "list", "-R", repo, "--state", "open", "--limit", "1000", "--json",
            "number,title,isDraft,reviewDecision,statusCheckRollup,createdAt"])
    if l is None:
        return None
    return [dict(repo=repo, number=p["number"], title=p["title"][:80], draft=p["isDraft"],
                 review=p.get("reviewDecision") or "none", ci=ci_of(p.get("statusCheckRollup")), created=p["createdAt"])
            for p in l]


def collect(S, gh=gh_json, now=None):
    prs, prs_ok = [], {}
    for repos in S["repos"].values():
        for repo in repos:
            r = prs_for(repo, gh); prs_ok[repo] = r is not None
            prs += r or []
    wip = {}
    for proj, repos in S["repos"].items():
        c = [p for p in prs if p["repo"] in repos]
        cnt = [p for p in c if (S["count_drafts"] or not p["draft"]) and not (S["exclude_approved"] and p["review"] == "APPROVED")]
        lim = S["wip_limit"].get(proj, S["wip_limit_default"])
        wip[proj] = dict(unknown=[r for r in repos if not prs_ok.get(r)], open=len(c), counted=len(cnt), limit=lim,
                         over=len(cnt) > lim, approved=sum(1 for p in c if p["review"] == "APPROVED"))
    b = dict(generated=int(now or time.time()), prs_ok=prs_ok, wip=wip, prs=sorted(prs, key=lambda p: p["created"]))
    st = Path(os.path.expanduser(S["state_dir"])); st.mkdir(parents=True, exist_ok=True)
    tmp = st / ("board.json.tmp%d" % os.getpid()); tmp.write_text(json.dumps(b, indent=1)); tmp.replace(st / "board.json")
    return b


def board(S, gh=gh_json, now=None):
    """the snapshot if younger than stale_after_min, else a fresh collect"""
    try:
        b = json.load(open(Path(os.path.expanduser(S["state_dir"])) / "board.json"))
        if (now or time.time()) - b["generated"] < S["stale_after_min"] * 60:
            return b
    except Exception:
        pass
    return collect(S, gh, now)


def text_board(b):
    L = ["WIP BOARD " + time.strftime("%Y-%m-%d %H:%M", time.localtime(b["generated"]))]
    for k, w in b["wip"].items():
        L.append("  %s %d/%d %s (open %d, approved %d)%s" % (k, w["counted"], w["limit"], "OVER" if w["over"] else "ok",
                 w["open"], w["approved"], "  !! unreadable: " + ",".join(w["unknown"]) if w["unknown"] else ""))
    L.append("PRs, oldest first (%d)" % len(b["prs"]))
    for p in b["prs"][:15]:
        L.append("  %s#%d %s CI:%s rev:%s%s" % (p["repo"], p["number"], p["title"][:40], p["ci"], p["review"], " DRAFT" if p["draft"] else ""))
    return "\n".join(L)


def check(S, proj, gh=gh_json, now=None, out=sys.stdout):
    if not S["enabled"] or proj not in S["repos"]:
        print("wip: no limit tracked for '%s'" % proj, file=out); return 0
    b = board(S, gh, now); w = b["wip"].get(proj)  # a project added after the snapshot has no row yet
    if w is None or w["unknown"]:
        w = w or {"unknown": ["(not in snapshot)"]}
        print("wip: %s UNKNOWN (GitHub read failed for %s); not blocking" % (proj, ",".join(w["unknown"])), file=out); return 0
    if w["over"]:
        mode = S["enforce"]
        print("wip: %s %d awaiting review (open %d, approved %d), limit %d -> %s: batch-approve/merge/close first, "
              "tell the human instead of spawning PR work" % (proj, w["counted"], w["open"], w["approved"], w["limit"],
                                                               "HOLD" if mode else "WARN (observe mode)"), file=out)
        return 3 if mode else 0
    print("wip: %s %d/%d ok" % (proj, w["counted"], w["limit"]), file=out)
    return 0


def main(argv=None):
    a = sys.argv[1:] if argv is None else argv
    S = load_settings()
    if a[:1] == ["check"] and len(a) == 2:
        return check(S, a[1])
    if a[:1] == ["collect"]:
        b = collect(S)
        if "--board" in a: print(text_board(b))
        return 0
    print(__doc__); return 2


if __name__ == "__main__":
    sys.exit(main())
