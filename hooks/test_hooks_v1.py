import json, os, sqlite3, subprocess, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def run(script, payload, env=None, args=()):
    return subprocess.run([sys.executable, str(HERE / script), *args], input=payload, capture_output=True, text=True,
                          env={**os.environ, **(env or {})})


class GhLogGuard(unittest.TestCase):
    def rc(self, cmd):
        return run("gh_log_guard_v1.py", json.dumps({"tool_input": {"command": cmd}})).returncode

    def test_blocks(self):
        for cmd in ("gh run view 1 --log", "gh auth token", 'gh auth "token"', "gh run download 5",
                    "gh api repos/o/r/actions/runs/1/logs", "GH run view --LOG", "gh api $URL",
                    'part=auth; gh "$part" token', "x=gh; y=auth; $x $y token", "gh `echo auth` token",
                    "gh auth toke{n..n}", "gh run view 1 --lo{g..g}", "gh run view 1 {--lo,--lo}g",
                    "/opt/homebrew/bin/g[h] auth token", "/opt/homebrew/bin/g? auth token", "cd /x && {g,}h auth token",
                    "gh auth status -at", "gh auth git-credential get", "gh alias import a.yml"):
            self.assertEqual(self.rc(cmd), 2, cmd)

    def test_allows(self):
        for cmd in ("gh pr list", "gh run list --limit 5", "git status", "echo hi", "ls ~/.gh-tools", "ls *.py",
                    "[[ -f a ]] && echo y", 'echo "${HOME}"', "gh auth status"):
            self.assertEqual(self.rc(cmd), 0, cmd)

    def test_malformed_input_fails_open(self):
        self.assertEqual(run("gh_log_guard_v1.py", "not json").returncode, 0)


class TabGuard(unittest.TestCase):
    def transcript(self, names):
        f = Path(tempfile.mkdtemp()) / "s.jsonl"
        f.write_text("\n".join(json.dumps({"message": {"content": [{"type": "tool_use", "name": n, "input": {}}]}}) for n in names))
        return str(f)

    def out(self, names, active=False):
        env = {"TAB_GUARD_LOG": str(Path(tempfile.mkdtemp()) / "t.log")}
        return run("tab_guard_v1.py", json.dumps({"transcript_path": self.transcript(names), "stop_hook_active": active}), env).stdout

    def test_open_tab_blocks(self):
        o = self.out(["mcp__Claude_Browser__tabs_create"])
        self.assertEqual(json.loads(o)["decision"], "block")

    def test_closed_tab_passes(self):
        self.assertEqual(self.out(["mcp__Claude_Browser__tabs_create", "mcp__Claude_Browser__tabs_close"]), "")

    def test_no_loop(self):
        self.assertEqual(self.out(["mcp__Claude_Browser__tabs_create"], active=True), "")


class MemRedact(unittest.TestCase):
    def test_scrubs_db_and_logs(self):
        tmp = Path(tempfile.mkdtemp())
        secret = "Zq" + "9" * 10 + "abcXYZ" * 3            # 30 chars, letters + digits, built at runtime
        env_file = tmp / "app.env"; env_file.write_text("API_TOKEN=%s\nDEBUG=true\n" % secret)
        root = tmp / "mem"; root.mkdir()
        con = sqlite3.connect(root / "claude-mem.db")
        con.execute('create table "odd""name" (body text)')
        con.execute('insert into "odd""name" values (?)', ("ran curl with %s ok" % secret,))
        con.commit(); con.close()
        (root / "worker.log").write_text("token=%s\n" % secret)
        cfg = tmp / "settings.json"
        cfg.write_text(json.dumps({"mem_redact": {"mem_root": str(root), "db_name": "claude-mem.db",
                                                  "state_file": str(tmp / "state.json"), "secret_stores": [str(env_file)]}}))
        r = run("mem_redact_v1.py", "", {"MEM_REDACT_SETTINGS": str(cfg)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(secret, r.stderr + r.stdout)
        body = sqlite3.connect(root / "claude-mem.db").execute('select body from "odd""name"').fetchone()[0]
        self.assertEqual(body, "ran curl with [REDACTED] ok")
        # masked in place at the same length: a live writer's later appends are never truncated away
        self.assertEqual((root / "worker.log").read_text(), "token=%s\n" % ("*" * len(secret)))
        self.assertLessEqual(json.loads((tmp / "state.json").read_text())["clean_at"], (root / "worker.log").stat().st_mtime)

    def test_uppercase_secret_and_untyped_column(self):
        tmp = Path(tempfile.mkdtemp())
        seed = "QWERTYUIOP" + "ASDFGHJKLZ" + "234567"           # base32-shaped, all caps: must not be mistaken for a name
        env_file = tmp / "app.env"; env_file.write_text("TOTP_SECRET=%s\n" % seed)
        root = tmp / "mem"; root.mkdir()
        con = sqlite3.connect(root / "claude-mem.db")
        con.execute("create table obs (a text, b varchar(200))")
        con.execute("insert into obs values (?, ?)", ("saw %s" % seed, "also %s" % seed))
        con.commit(); con.close()
        cfg = tmp / "settings.json"
        cfg.write_text(json.dumps({"mem_redact": {"mem_root": str(root), "db_name": "claude-mem.db",
                                                  "state_file": str(tmp / "state.json"), "secret_stores": [str(env_file)]}}))
        r = run("mem_redact_v1.py", "", {"MEM_REDACT_SETTINGS": str(cfg)})
        self.assertEqual(r.returncode, 0, r.stderr)
        row = sqlite3.connect(root / "claude-mem.db").execute("select a, b from obs").fetchone()
        self.assertEqual(row, ("saw [REDACTED]", "also [REDACTED]"))

    def test_short_keyed_password_scrubbed(self):
        tmp = Path(tempfile.mkdtemp())
        pw = "Kx7" + "q2Lm9"                                   # 8 chars under a PASSWORD key
        env_file = tmp / "app.env"; env_file.write_text("DB_PASSWORD=%s\nPIN_CODE=48213307\n" % pw)
        root = tmp / "mem"; root.mkdir()
        con = sqlite3.connect(root / "claude-mem.db")
        con.execute("create table obs (a text)"); con.execute("insert into obs values (?)", ("pw %s pin 48213307" % pw,))
        con.commit(); con.close()
        cfg = tmp / "settings.json"
        cfg.write_text(json.dumps({"mem_redact": {"mem_root": str(root), "db_name": "claude-mem.db",
                                                  "state_file": str(tmp / "state.json"), "secret_stores": [str(env_file)]}}))
        r = run("mem_redact_v1.py", "", {"MEM_REDACT_SETTINGS": str(cfg)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sqlite3.connect(root / "claude-mem.db").execute("select a from obs").fetchone()[0], "pw [REDACTED] pin 48213307")


if __name__ == "__main__":
    unittest.main()
