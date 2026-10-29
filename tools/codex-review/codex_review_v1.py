#!/usr/bin/env python3
"""Cross-model review gate: a second model (Codex CLI) reviews production code once, and the findings
land in a ledger the coding agent must clear (fix or dismiss with a reason) before new work.

  review <path> --unit NAME --project P [--scope full|commit:SHA|base:BRANCH|uncommitted] [--force] [--dry-run]
  status [--project P] [--unit NAME] [--brief]     open findings (--brief is the one-line SessionStart form)
  resolve <finding_id> fixed|dismissed "why"
  units                                            every unit plus its last review

Settings: settings_v1.json next to this file, or the file named by CODEX_REVIEW_SETTINGS.
Findings go to <reviews_dir>/ledger.json and one .md per review. For a GitHub repo they can also be posted
as a commit comment. Every output is secret-scanned and redacted before it is written or posted (fail closed).
A path inside a `pinned_checkouts` worktree is fetched and re-pinned to its remote ref before the review,
so the reviewer reads what ships, not a stale branch. --dry-run does that re-pin and prints what it would
review, without running Codex or touching the ledger.
"""
import argparse, datetime as dt, fcntl, hashlib, fnmatch, json, os, re, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEV = ["low", "medium", "high", "critical"]
X = lambda p: Path(os.path.expanduser(p))

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["summary", "findings"],
    "properties": {
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["severity", "title", "file", "line", "body"],
            "properties": {
                "severity": {"type": "string", "enum": SEV},
                "title": {"type": "string"},
                "file": {"type": "string"},
                "line": {"type": ["integer", "null"]},
                "body": {"type": "string"}}}}}}


def load_settings(path=None):
    return json.load(open(path or os.environ.get("CODEX_REVIEW_SETTINGS") or HERE / "settings_v1.json"))


S = load_settings()


def rd():
    d = X(S["reviews_dir"]); d.mkdir(parents=True, exist_ok=True)
    return d


def sh(cmd, cwd=None, timeout=60, env=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, **env} if env else None)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def load():
    p = rd() / "ledger.json"
    return json.load(open(p)) if p.exists() else {"units": {}, "daily": {}}


def save(d):
    p = rd() / "ledger.json"
    tmp = p.with_suffix(".tmp%d" % os.getpid())
    with open(tmp, "w") as fh: json.dump(d, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, p)


def ledger_lock():
    """short exclusive lock around a ledger load..save; separate from the long review lock so resolve never waits on a review"""
    lk = open(rd() / ".ledger.lock", "w"); fcntl.flock(lk, fcntl.LOCK_EX); return lk


def redact(text):
    hits = 0
    for pat in S["secret_patterns"]:
        text, n = re.subn(pat, "[REDACTED-SECRET]", text)
        hits += n
    return text, hits


def redact_tree(o):
    """redact every string in decoded JSON: escapes like \\u0067hp_ only become a matchable token after json.loads"""
    if isinstance(o, str):
        return redact(o)
    if isinstance(o, list):
        pairs = [redact_tree(v) for v in o]
        return [v for v, _ in pairs], sum(n for _, n in pairs)
    if isinstance(o, dict):
        pairs = {k: redact_tree(v) for k, v in o.items()}
        return {k: v for k, (v, _) in pairs.items()}, sum(n for _, n in pairs.values())
    return o, 0


def swap_pct():
    """macOS swap use in percent; 0 elsewhere or when unreadable (the gate then never pauses)"""
    if sys.platform != "darwin":
        return 0.0
    _, out, _ = sh(["sysctl", "-n", "vm.swapusage"])
    m = re.search(r"total = ([\d.]+)M\s+used = ([\d.]+)M", out)
    return 100 * float(m.group(2)) / float(m.group(1)) if m and float(m.group(1)) else 0.0


def secret_paths_under(path):
    """settings `secret_paths` that sit inside the reviewed folder, relative to it, so the prompt can forbid them"""
    out = []
    for s in S.get("secret_paths", []):
        sp = X(s).resolve()
        if str(sp).startswith(str(path) + os.sep):
            out.append(str(sp.relative_to(path)))
    return out


def git_info(path):
    rc, top, _ = sh(["git", "-C", str(path), "rev-parse", "--show-toplevel"])
    if rc:
        return None
    _, sha, _ = sh(["git", "-C", top, "rev-parse", "HEAD"])
    _, url, _ = sh(["git", "-C", top, "remote", "get-url", "origin"])
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+?)(\.git)?$", url or "")
    return {"top": top, "sha": sha, "gh": f"{m.group(1)}/{m.group(2)}" if m else None}


def review_ident(gi, scope, path, target):
    """what one review covered: commit + scope + reviewed path + working-tree diff, so a partial or dirty review never suppresses a different one"""
    if not gi:
        return None
    rel = str(Path(target) if target else ".")
    _, diff, _ = sh(["git", "-C", str(path), "diff", "HEAD", "--", rel])
    _, untracked, _ = sh(["git", "-C", str(path), "ls-files", "--others", "--exclude-standard", "--", rel])
    blobs = []
    for f in (untracked or "").splitlines():
        try:
            blobs.append(f + ":" + hashlib.sha256((Path(path) / f).read_bytes()).hexdigest())
        except OSError:
            blobs.append(f + ":unreadable")
    h = hashlib.sha256("\0".join([gi["sha"], scope, str(path), rel, diff or "", *blobs]).encode()).hexdigest()
    return h[:16]


def under(path, root):
    return path == root or str(path).startswith(str(root) + os.sep)


def repin(path):
    """Fetch the remote ref and hard-pin the pinned checkout that holds `path` (creating it if missing).

    Returns {root, ref, sha} or None when `path` is not in a pinned checkout. Only a linked worktree of `repo`
    on a detached HEAD is ever reset or cleaned, so the repo's main checkout (its branch and untracked files)
    and any branch someone works on are refused even if settings are misconfigured. The fetch updates only
    refs/remotes/<remote>/<ref>; it never touches another checkout's working tree."""
    for pc in S.get("pinned_checkouts", []):
        root = X(pc["path"]).resolve()
        if not under(path, root):
            continue
        repo, remote, ref = X(pc["repo"]).resolve(), pc.get("remote", "origin"), pc.get("ref", "main")
        rref, quiet = f"{remote}/{ref}", {"GIT_TERMINAL_PROMPT": "0"}
        rc, _, err = sh(["git", "-C", str(repo), "fetch", "--quiet", "--no-tags", remote,
                         f"+refs/heads/{ref}:refs/remotes/{rref}"], timeout=180, env=quiet)
        if rc:
            sys.exit(f"re-pin {root}: fetch {rref} failed: {redact(err)[0][:300]}")
        if not root.exists():
            rc, _, err = sh(["git", "-C", str(repo), "worktree", "add", "--quiet", "--detach", str(root), rref],
                            timeout=300)
            if rc:
                sys.exit(f"re-pin {root}: worktree add failed: {redact(err)[0][:300]}")
            sh(["git", "-C", str(repo), "worktree", "lock", "--reason", "codex-review pinned checkout", str(root)])
        g = lambda *c: sh(["git", "-C", str(root), *c])
        top, gd = g("rev-parse", "--show-toplevel")[1], g("rev-parse", "--absolute-git-dir")[1]
        cd = g("rev-parse", "--path-format=absolute", "--git-common-dir")[1]
        rcd = sh(["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"])[1]
        on_branch = g("symbolic-ref", "-q", "HEAD")[0] == 0
        if not top or Path(top).resolve() != root or gd == cd or cd != rcd or on_branch:
            sys.exit(f"re-pin {root}: refused, not a detached linked worktree of {repo} "
                     f"(top={top or '-'}, main_worktree={gd == cd}, on_branch={on_branch})")
        rc, _, err = g("checkout", "--quiet", "--force", "--detach", rref)
        if rc:
            sys.exit(f"re-pin {root}: checkout {rref} failed: {redact(err)[0][:300]}")
        removed = g("clean", "-ffd")[1]          # stray untracked files; gitignored ones are left alone
        head, want = g("rev-parse", "HEAD")[1], g("rev-parse", rref)[1]
        dirty = g("status", "--porcelain")[1]
        if not head or head != want or dirty:
            sys.exit(f"re-pin {root}: HEAD {head[:7]} vs {rref} {want[:7]}, dirty={bool(dirty)}; not reviewing")
        n = len(removed.splitlines())
        print(f"re-pinned {root} -> {rref} @ {head[:7]}" + (f" ({n} stray file(s) removed)" if n else ""))
        return {"root": str(root), "ref": rref, "sha": head}
    return None


SCOPE_RE = re.compile(r"^(full|uncommitted|(commit|base):[A-Za-z0-9._/@^~-]{1,200})$")


def build_prompt(scope, skip, target=None):
    if target and scope == "full":
        scope = "file:" + target
    what = {
        "full": "the whole codebase in the current directory. It is already in production and has never been reviewed.",
        "uncommitted": "the uncommitted changes (run `git diff HEAD` and `git status`).",
    }.get(scope)
    if not what and scope.startswith("file:"):
        what = f"only the file {scope[5:]} in the current directory. It is already in production and has never been reviewed."
    if not what and scope.startswith("commit:"):
        what = f"the changes in commit {scope[7:]} (run `git show {scope[7:]}`)."
    if not what and scope.startswith("base:"):
        what = f"the changes on this branch versus {scope[5:]} (run `git diff {scope[5:]}...HEAD`)."
    return f"""You are a senior code reviewer. Review {what}

Report only real defects that would bite in production: incorrect behavior, crashes, data loss or
corruption, security holes (injection, auth, exposed secrets, unsafe deserialization), broken error
handling, race conditions, resource leaks, and destructive operations without guards. Skip style, naming
and nitpicks. Every finding needs a concrete failure scenario in `body`. If the code is sound, return
an empty findings list. Do not invent problems.

Rules: you are read-only, so do not modify anything. Never open or read credential files: .env*,
*token*, *secret*, *.pem, *.key, config.toml, .git/config{', or: ' + ', '.join(skip) if skip else ''}.
Never quote a secret value, even if you see one. Say "hardcoded credential at file:line" instead.
Skip {', '.join(S.get('exclude_globs', []))}.
`file` is relative to the current directory. `summary` is 1-3 sentences on what was reviewed and its overall health."""


def post_github(gi, unit, findings, summary):
    floor = SEV.index(S["github_min_severity"])
    sel = [f for f in findings if SEV.index(f["severity"]) >= floor]
    rc, _, _ = sh(["gh", "api", f"repos/{gi['gh']}/commits/{gi['sha']}", "-q", ".sha"])
    if rc:
        return "skipped: commit not on GitHub (push first)"
    lines = [f"### Codex review - `{unit}` @ {gi['sha'][:7]}", "", summary, ""]
    lines += [f"- **{f['severity'].upper()}** `{f['file']}:{f['line'] or '-'}` - {f['title']}\n  {f['body']}" for f in sel]
    if not sel:
        lines.append(f"No findings at or above {S['github_min_severity']}.")
    lines += ["", "<sub>codex_review_v1</sub>"]
    body, _ = redact("\n".join(lines))
    rc, out, err = sh(["gh", "api", "-X", "POST", f"repos/{gi['gh']}/commits/{gi['sha']}/comments",
                       "-f", f"body={body}", "-q", ".html_url"])
    return out if rc == 0 else f"failed: {redact(err)[0][:200]}"


def safe_name(s):
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", s) or s in (".", ".."):
        sys.exit(f"bad name {s!r}: letters, digits, . _ - only")
    return s


def cmd_review(a):
    if not S["enabled"]:
        sys.exit("codex-review disabled in settings")
    safe_name(a.unit); safe_name(a.project)
    if not SCOPE_RE.match(a.scope):
        sys.exit(f"bad scope {a.scope!r}")
    path = X(a.path).resolve()
    lk = open(rd() / ".lock", "w")              # one review at a time, machine-wide; held until exit
    try:                                        # taken before the re-pin so no running review sees files move
        fcntl.flock(lk, fcntl.LOCK_EX | (fcntl.LOCK_NB if a.dry_run else 0))
    except BlockingIOError:
        sys.exit("another review holds the lock; dry run not started (retry when it finishes)")
    d = load()                                  # read under the lock so a waiting review can't clobber the last one
    today = dt.date.today().isoformat()
    capped = d["daily"].get(today, 0) >= S["max_reviews_per_day"] and not a.force
    if capped and not a.dry_run:
        sys.exit(f"daily cap {S['max_reviews_per_day']} reached")
    pin = repin(path)
    target = None
    if path.is_file():                          # single-file unit: run in its folder, review only that file
        target, path = path.name, path.parent
    elif not path.is_dir():
        sys.exit(f"not found: {path}")
    gi = git_info(path)
    for pc in S.get("pinned_checkouts", []):    # the people-facing checkout of a pinned repo is not what ships
        if gi and a.scope == "full" and Path(gi["top"]).resolve() == X(pc["repo"]).resolve():
            sys.exit(f"{path} is the working checkout of {pc['repo']} (any branch, can be weeks old). Review "
                     f"{X(pc['path']) / path.relative_to(Path(gi['top']).resolve())} instead "
                     f"(re-pinned to {pc.get('remote', 'origin')}/{pc.get('ref', 'main')} before each review).")
    if a.scope != "full" and not gi:
        sys.exit(f"scope {a.scope} needs git; use --scope full")
    u = d["units"].get(a.unit, {})
    ident = review_ident(gi, a.scope, path, target)
    done = ident and u.get("last_review", {}).get("ident") == ident and not a.force
    if done and not a.dry_run:
        print(f"{a.unit}: already reviewed at {gi['sha'][:7]}"); return
    pause = S.get("swap_pause_pct")
    sp = swap_pct()
    deferred = pause is not None and sp >= pause and not a.force
    if deferred and not a.dry_run:
        sys.exit(f"swap {sp:.0f}% >= {pause}%: deferred")
    prompt = build_prompt(a.scope, secret_paths_under(path), target)
    md_dir = rd() / a.project / a.unit

    if a.dry_run:
        tag = gi["sha"][:7] if gi else "nogit"
        print("\n".join([
            f"DRY RUN {a.unit} ({a.project}): Codex not run, ledger untouched",
            f"  reviews   {path}" + (f" (file {target})" if target else "") + f" · scope {a.scope}",
            f"  commit    {gi['sha'] if gi else 'not git'}"
            + (f" = {pin['ref']} (re-pinned)" if pin else (" (not a pinned checkout)" if gi else "")),
            f"  git       {gi['top'] if gi else '-'} · {(gi['gh'] or 'local') if gi else '-'}",
            f"  writes    {md_dir / (dt.datetime.now().strftime('%Y%m%d-%H%M') + '-' + tag + '.md')}",
            f"  last      {(u.get('last_review', {}).get('sha') or '-')[:7]} on {u.get('last_review', {}).get('date', '-')}"
            + (" -> would SKIP (already reviewed: same commit, scope, path and working tree; --force overrides)" if done else " -> would review"),
            f"  gates     daily {d['daily'].get(today, 0)}/{S['max_reviews_per_day']}" + (" CAPPED" if capped else "")
            + f" · swap {sp:.0f}%/{pause if pause is not None else 'off'}" + (" DEFERRED" if deferred else ""),
            f"  prompt    {prompt.splitlines()[0][:160]}"]))
        return

    with tempfile.TemporaryDirectory() as td:
        sf, of = Path(td) / "schema.json", Path(td) / "out.json"
        sf.write_text(json.dumps(SCHEMA))
        cmd = [os.path.expanduser(S["codex_bin"]), "exec", "--ephemeral", "--ignore-user-config", "-s", "read-only",
               "--skip-git-repo-check", "-C", str(path), "--output-schema", str(sf), "-o", str(of), prompt]
        t0 = dt.datetime.now()
        env = {**os.environ, **({"CODEX_HOME": str(X(S["codex_home"]))} if S.get("codex_home") else {})}
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=S["timeout_sec"], env=env, stdin=subprocess.DEVNULL)
        secs = (dt.datetime.now() - t0).seconds
        if r.returncode or not of.exists():
            sys.exit(f"codex failed rc={r.returncode}: {redact(r.stderr)[0][-600:]}")
        res, hits = redact_tree(json.loads(of.read_text()))

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    tag = gi["sha"][:7] if gi else "nogit"
    for i, f in enumerate(res["findings"], 1):
        f.update(id=f"{a.unit}-{tag}-{i}", status="open", note="")
    md = md_dir / f"{stamp}-{tag}.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("\n".join(
        [f"# Codex review - {a.unit} ({a.project})", f"path `{path}` · scope `{a.scope}` · {tag}"
         + (f" = {pin['ref']}" if pin else "") + f" · {secs}s"
         + (f" · {hits} secret(s) redacted" if hits else ""), "", res["summary"], ""]
        + [f"## [{f['severity']}] {f['title']}\n`{f['file']}:{f['line'] or '-'}` · id `{f['id']}`\n\n{f['body']}\n"
           for f in res["findings"]]))
    can_post = gi and gi["gh"] and S["post_to_github"] and not any(
        fnmatch.fnmatch(gi["gh"], g) for g in S.get("github_post_exclude", []))
    gh = post_github(gi, a.unit, res["findings"], res["summary"]) if can_post else None
    _lk = ledger_lock()                         # re-read so resolutions saved during the review survive
    d = load(); u = d["units"].get(a.unit, {})
    prev = [f for f in u.get("findings", []) if f["status"] == "open"]
    d["units"][a.unit] = {
        "project": a.project, "path": str(path), "git": gi["gh"] if gi and gi["gh"] else ("local" if gi else "none"),
        "last_review": {"date": stamp, "sha": gi["sha"] if gi else None, "ident": ident, "scope": a.scope, "file": str(md),
                        "secs": secs, "github": gh, "redactions": hits, "pinned": pin["ref"] if pin else None},
        "findings": prev + res["findings"]}
    d["daily"][today] = d["daily"].get(today, 0) + 1
    save(d)
    n = {s: sum(f["severity"] == s for f in res["findings"]) for s in SEV}
    print(f"{a.unit}: {len(res['findings'])} findings "
          f"(crit {n['critical']}, high {n['high']}, med {n['medium']}, low {n['low']}) in {secs}s -> {md}"
          + (f" · github: {gh}" if gh else ""))


def cmd_status(a):
    d = load()
    rows = []
    for name, u in d["units"].items():
        if a.project and u["project"] != a.project:
            continue
        if a.unit and name != a.unit:
            continue
        rows += [(name, f) for f in u.get("findings", []) if f["status"] == "open"]
    rows.sort(key=lambda r: -SEV.index(r[1]["severity"]))
    if a.brief:
        if rows:
            top = sum(SEV.index(f["severity"]) >= 2 for _, f in rows)
            print(f"[codex] {len(rows)} open review findings ({top} high/critical) for "
                  f"{a.project or 'all projects'}. Run: python3 {__file__} status"
                  + (f" --project {a.project}" if a.project else "")
                  + ". Fix each or dismiss with a reason via `resolve`.")
        return
    for name, f in rows:
        print(f"{f['id']}  [{f['severity']}] {f['title']}  ({d['units'][name]['path']}/{f['file']}:{f['line'] or '-'})\n    {f['body']}")
    if not rows:
        print("no open findings")


def cmd_resolve(a):
    _lk = ledger_lock()
    d = load()
    # review passes can reuse ids, so resolve the first still-open match
    hits = [f for u in d["units"].values() for f in u.get("findings", []) if f["id"] == a.fid]
    hits.sort(key=lambda f: f["status"] != "open")
    if not hits:
        sys.exit(f"no finding {a.fid}")
    hits[0]["status"], hits[0]["note"] = a.status, a.why
    save(d); print(f"{a.fid} -> {a.status}")


def cmd_units(a):
    d = load()
    for name, u in sorted(d["units"].items()):
        lr = u.get("last_review", {})
        op = sum(f["status"] == "open" for f in u.get("findings", []))
        print(f"{name:32} {u['project']:11} {u['git']:34} last {lr.get('date', '-')}  open {op}")


def main(argv=None):
    p = argparse.ArgumentParser()
    sp = p.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("review"); r.add_argument("path"); r.add_argument("--unit", required=True)
    r.add_argument("--project", required=True); r.add_argument("--scope", default="full"); r.add_argument("--force", action="store_true")
    r.add_argument("--dry-run", action="store_true")
    s = sp.add_parser("status"); s.add_argument("--project"); s.add_argument("--unit"); s.add_argument("--brief", action="store_true")
    v = sp.add_parser("resolve"); v.add_argument("fid"); v.add_argument("status", choices=["fixed", "dismissed"]); v.add_argument("why")
    sp.add_parser("units")
    a = p.parse_args(argv)
    {"review": cmd_review, "status": cmd_status, "resolve": cmd_resolve, "units": cmd_units}[a.cmd](a)


if __name__ == "__main__":
    main()
