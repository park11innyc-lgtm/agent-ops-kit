#!/usr/bin/env python3
"""Maid scan: read-only cleanup candidates per project (dead code, stale comments,
dead tests, stale branches/worktrees, superseded files, orphan memory, expired one-shots).

Never deletes or edits anything. Writes one Markdown report per project to report_dir.
Usage: python3 -I maid_scan_v1.py <project-id>|all|<id,id,...> [--deep] [--stdout] [--delta]
  --deep   also run vulture (Python unused code) if it is already installed; never fetched.
  --delta  print only counts that grew since the last --delta run (state/counts.json); silent if none.
Settings: settings_v1.json next to this file, or MAID_SETTINGS.
"""
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
S = json.load(open(os.environ.get("MAID_SETTINGS") or os.path.join(HERE, "settings_v1.json")))
TODAY = dt.date.today()
EXP = os.path.expanduser
DATE_RE = re.compile(r"(20\d\d)-?(0[1-9]|1[0-2])-?([0-3]\d)")
VER_RE = re.compile(r"^(.*?)[_-]v(\d+)(\.[A-Za-z0-9]+)?$")
COMMENT_RE = re.compile(r"^\s*(#|//|--|/\*|\*)")
TEST_FILE_RE = re.compile(r"(^test_.*\.py$|_test\.py$|\.(test|spec)\.(ts|tsx|js|mjs)$)")
ASSERT_RE = re.compile(r"\bassert\b|\bexpect\(|\bassert\.|\bt\.(is|true|deepEqual)|\braises\(")
STALE = [re.compile(p, re.I) for p in S["stale_comment_patterns"]]
SKIPT = [re.compile(p) for p in S["skipped_test_patterns"]]
CAP = S["max_items_per_section"]


def git(repo, *args):
    try:
        return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return ""


def walk(root):
    skip = set(S["skip_dirs"])
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in skip and not x.startswith(".claude-worktree")]
        # A nested git worktree/repo is scanned as its own target, not as files of this one.
        if d != root and os.path.exists(os.path.join(d, ".git")):
            dirs[:] = []
            continue
        for f in files:
            p = os.path.join(d, f)
            if not os.path.islink(p):  # a symlink can point outside the target; report only real files
                yield p


def parse_date(s):
    m = DATE_RE.search(s)
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def git_section(repo):
    out = []
    if not (os.path.exists(os.path.join(repo, ".git"))):
        return out
    head = git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").strip() or "origin/main"
    merged = {b.strip().lstrip("*+ ").strip() for b in git(repo, "branch", "--merged", head).splitlines()}
    merged.discard("")
    wt = git(repo, "worktree", "list", "--porcelain").split("\n\n")
    wts = []
    for block in wt:
        path = re.search(r"^worktree (.+)$", block, re.M)
        br = re.search(r"^branch refs/heads/(.+)$", block, re.M)
        if path and path.group(1) != repo:
            b = br.group(1) if br else "(detached)"
            tag = "prunable" if "prunable" in block else ("merged" if b in merged else "")
            wts.append((path.group(1), b, tag))
    cutoff = TODAY - dt.timedelta(days=S["stale_branch_days"])
    old = []
    for line in git(repo, "for-each-ref", "--format=%(refname:short)|%(committerdate:short)", "refs/heads").splitlines():
        name, _, d = line.partition("|")
        try:
            if dt.datetime.strptime(d, "%Y-%m-%d").date() < cutoff and name not in merged:
                old.append(f"{name} (last commit {d})")
        except ValueError:
            pass
    mb = sorted(b for b in merged if b not in ("main", "master", head.split("/")[-1]))
    out.append(f"- Worktrees besides main checkout: **{len(wts)}** ({sum(1 for w in wts if w[2])} merged or prunable)")
    for p, b, t in [w for w in wts if w[2]][:CAP]:
        out.append(f"  - `{p}` on `{b}` [{t}]")
    out.append(f"- Local branches already merged into `{head}`: **{len(mb)}** (safe-delete candidates: `git branch -d`)")
    out.append(f"- Unmerged branches with no commit in {S['stale_branch_days']}d: **{len(old)}**")
    for o in old[:CAP]:
        out.append(f"  - {o}")
    return out


def file_sections(root):
    baks, versioned, stale_c, skipped, empty_tests = [], {}, [], [], []
    ccount = 0
    for p in walk(root):
        name = os.path.basename(p)
        rel = os.path.relpath(p, root)
        if ".bak" in name or name.endswith("~") or name.endswith(".orig"):
            baks.append(rel)
            continue
        stem, ext = os.path.splitext(name)
        m = VER_RE.match(stem + ext) or VER_RE.match(stem)
        if m:
            key = (os.path.dirname(p), m.group(1), ext)
            versioned.setdefault(key, []).append((int(m.group(2)), rel))
        if ext in S["code_ext"]:
            try:
                with open(p, errors="ignore") as fh:
                    text = fh.read(400_000)
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if COMMENT_RE.match(line) or " # " in line or " // " in line:
                    if any(r.search(line) for r in STALE):
                        ccount += 1
                        if len(stale_c) < CAP:
                            stale_c.append(f"`{rel}:{i}` {line.strip()[:110]}")
                if any(r.search(line) for r in SKIPT) and len(skipped) < CAP:
                    skipped.append(f"`{rel}:{i}` {line.strip()[:110]}")
            if TEST_FILE_RE.search(name) and not ASSERT_RE.search(text):
                empty_tests.append(rel)
    superseded = []
    for vs in versioned.values():
        if len(vs) > 1:
            vs.sort()
            superseded += [f"{r} (newest is v{vs[-1][0]})" for _, r in vs[:-1]]
    return baks, superseded, stale_c, ccount, skipped, empty_tests


def dated_dirs(root):
    """Top-level entries whose name carries a date that has passed (one-shot tasks, dated scratch)."""
    out = []
    dcut = TODAY - dt.timedelta(days=S["stale_dated_name_days"])
    try:
        for n in sorted(os.listdir(root)):
            d = parse_date(n)
            if d and d < dcut:
                out.append(f"{n} ({d})")
    except OSError:
        pass
    return out


def memory_section(mdir):
    out = []
    mdir = EXP(mdir)
    idx = os.path.join(mdir, "MEMORY.md")
    if not os.path.isfile(idx):
        return ["- No MEMORY.md here (couldn't read, not 'empty')."]
    files = {f for f in os.listdir(mdir) if f.endswith(".md") and f != "MEMORY.md"}
    # Reachable = linked from MEMORY.md directly or through other notes (index notes, [[slug]] links).
    reach, missing, queue = set(), set(), ["MEMORY.md"]
    while queue:
        cur = queue.pop()
        try:
            text = open(os.path.join(mdir, cur), errors="ignore").read()
        except OSError:
            continue
        refs = re.findall(r"\]\(([^)\s]+\.md)\)", text) + [s + ".md" for s in re.findall(r"\[\[([^\]|#]+)", text)]
        for r in refs:
            p = os.path.normpath(os.path.join(mdir, os.path.dirname(cur), r))
            if not os.path.exists(p):
                if cur == "MEMORY.md":
                    missing.add(r)
                continue
            if os.path.dirname(p) == os.path.normpath(mdir) and os.path.basename(p) not in reach:
                reach.add(os.path.basename(p))
                queue.append(os.path.basename(p))
    orphans = sorted(files - reach)
    missing = sorted(missing)
    out.append(f"- Memory files: {len(files)}; unreachable from MEMORY.md (direct or via other notes): **{len(orphans)}**; index links to missing files: **{len(missing)}**")
    out += [f"  - orphan `{o}`" for o in orphans[:CAP]]
    out += [f"  - missing `{m}`" for m in missing[:CAP]]
    return out


def vulture(root):
    # only an already-installed vulture: the scan never fetches or runs a package from the network
    if not shutil.which("vulture"):
        return ["- vulture skipped: not installed (couldn't run, not 'clean'). `pipx install vulture` to enable."]
    r = subprocess.run(["vulture", root, "--min-confidence", "90",
                        "--exclude", ",".join(S["skip_dirs"])], capture_output=True, text=True, timeout=600)
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    return [f"- vulture (≥90% confidence unused Python): **{len(lines)}**"] + [f"  - `{l[:140]}`" for l in lines[:CAP]]


def bullets(title, items, total=None):
    n = total if total is not None else len(items)
    out = [f"- {title}: **{n}**"]
    out += [f"  - {i}" for i in items[:CAP]]
    if n > CAP:
        out.append(f"  - … {n - CAP} more (raise max_items_per_section to list)")
    return out


def scan(project, deep):
    if project in S.get("skip_projects", []):
        return f"# Maid scan — {project}\nSkipped project (settings skip_projects).\n"
    lines = [f"# Maid scan — {project} — {TODAY}", "",
             "Read-only candidates. Nothing was changed. Each item still needs a reference check "
             "(grep / tests) and a human's batch approval before any delete.", ""]
    for t in S["targets"].get(project, []):
        root = EXP(t)
        lines.append(f"## `{t}`")
        if not os.path.isdir(root):
            lines += ["- Not found on this machine (couldn't read, not 'clean').", ""]
            continue
        lines += git_section(root)
        baks, sup, stale_c, ccount, skipped, empty = file_sections(root)
        lines += bullets("Backup files (.bak/.orig/~)", [f"`{b}`" for b in baks])
        lines += bullets("Superseded versioned files (older vN beside newer)", [f"`{s}`" for s in sup])
        lines += bullets("Stale-looking comments (TODO/temporary/legacy/remove-after…)", stale_c, ccount)
        lines += bullets("Skipped or focused tests", skipped)
        lines += bullets("Test files with no assertion", [f"`{e}`" for e in empty])
        dd = dated_dirs(root)
        if dd:
            lines += bullets(f"Top-level names dated > {S['stale_dated_name_days']}d ago", dd)
        if deep:
            lines += vulture(root)
        lines.append("")
    md = S["memory_dirs"].get(project)
    if md:
        lines += ["## Memory", *memory_section(md), ""]
    return "\n".join(lines) + "\n"


def counts(rep):
    """{"<target> | <section>": n} from a report's '- Section: **n**' lines."""
    out, target = {}, ""
    for line in rep.splitlines():
        if line.startswith("## "):
            target = line[3:].strip("`")
        m = re.match(r"- (.+?): \*\*(\d+)\*\*", line)
        if m:
            out[f"{target} | {m.group(1)[:60]}"] = int(m.group(2))
    return out


def delta(new):
    path = os.path.join(EXP(S.get("state_dir", os.path.join(HERE, "state"))), "counts.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    old = json.load(open(path)) if os.path.exists(path) else {}
    grew = [f"{k}: {old.get(k, 0)} -> {v}" for k, v in sorted(new.items()) if v > old.get(k, 0) and k in old]
    json.dump({**old, **new}, open(path, "w"), indent=1)  # merge: a subset run keeps other projects' baselines
    return grew


def main():
    if len(sys.argv) < 2 or not S.get("enabled"):
        print(__doc__)
        return 2
    deep, to_stdout, want_delta = "--deep" in sys.argv, "--stdout" in sys.argv, "--delta" in sys.argv
    projects = list(S["targets"]) if sys.argv[1] == "all" else sys.argv[1].split(",")
    allc = {}
    rdir = EXP(S["report_dir"])
    os.makedirs(rdir, exist_ok=True)
    for p in projects:
        rep = scan(p, deep)
        allc.update({f"{p} | {k}": v for k, v in counts(rep).items()})
        if want_delta:
            open(os.path.join(rdir, f"MAID_{p}_{TODAY}.md"), "w").write(rep)
        elif to_stdout:
            print(rep)
        else:
            path = os.path.join(rdir, f"MAID_{p}_{TODAY}.md")
            open(path, "w").write(rep)
            print(path)
    if want_delta:
        grew = delta(allc)
        print("\n".join(["MAID grew:"] + grew) if grew else "MAID: no growth")
    return 0


if __name__ == "__main__":
    sys.exit(main())
