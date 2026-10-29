import json, os, stat, subprocess, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE / "codex_review_v1.py"
FAKE_KEY = "ghp_" + "a1" * 15  # built at runtime so the repo never holds a key-shaped literal

FAKE_CODEX = """#!/usr/bin/env python3
import json, sys
a = sys.argv
out = a[a.index("-o") + 1]
json.dump({"summary": "reviewed; token %s seen", "findings": [
  {"severity": "high", "title": "t1", "file": "a.py", "line": 3, "body": "leak %s"},
  {"severity": "low", "title": "t2", "file": "b.py", "line": None, "body": "minor"}]}, open(out, "w"))
""" % (FAKE_KEY, FAKE_KEY)


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        codex = self.tmp / "codex"
        codex.write_text(FAKE_CODEX); codex.chmod(codex.stat().st_mode | stat.S_IXUSR)
        s = json.load(open(HERE / "settings_v1.json"))
        s.update(codex_bin=str(codex), reviews_dir=str(self.tmp / "reviews"), post_to_github=False,
                 projects={"p1": [str(self.tmp / "src")]})
        self.settings = self.tmp / "settings.json"
        self.settings.write_text(json.dumps(s))
        (self.tmp / "src").mkdir(); (self.tmp / "src" / "a.py").write_text("x = 1\n")
        self.env = {**os.environ, "CODEX_REVIEW_SETTINGS": str(self.settings)}

    def run_tool(self, *args, script=TOOL, stdin=None):
        return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True,
                              env=self.env, input=stdin)

    def test_review_redacts_and_ledgers(self):
        r = self.run_tool("review", str(self.tmp / "src"), "--unit", "u1", "--project", "p1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("2 findings", r.stdout)
        led = (self.tmp / "reviews" / "ledger.json").read_text()
        self.assertNotIn(FAKE_KEY, led)
        self.assertIn("[REDACTED-SECRET]", led)
        md = list((self.tmp / "reviews" / "p1" / "u1").glob("*.md"))
        self.assertEqual(len(md), 1); self.assertNotIn(FAKE_KEY, md[0].read_text())

    def test_status_resolve_and_hook(self):
        self.run_tool("review", str(self.tmp / "src"), "--unit", "u1", "--project", "p1")
        r = self.run_tool("status", "--project", "p1", "--brief")
        self.assertIn("2 open review findings (1 high/critical)", r.stdout)
        h = self.run_tool(script=HERE / "session_hook_v1.py", stdin=json.dumps({"cwd": str(self.tmp / "src")}))
        self.assertIn("for p1", h.stdout)
        fid = json.loads((self.tmp / "reviews" / "ledger.json").read_text())["units"]["u1"]["findings"][0]["id"]
        self.assertEqual(self.run_tool("resolve", fid, "fixed", "patched").returncode, 0)
        self.assertIn("1 open", self.run_tool("status", "--project", "p1", "--brief").stdout)
        self.assertNotEqual(self.run_tool("resolve", "nope", "fixed", "x").returncode, 0)

    def test_bad_names_and_scope_refused(self):
        for args in (["--unit", "../x", "--project", "p1"], ["--unit", "u", "--project", "p1", "--scope", "commit:$(id)"]):
            r = self.run_tool("review", str(self.tmp / "src"), *args)
            self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.tmp / "reviews" / "ledger.json").exists())

    def test_dry_run_touches_nothing(self):
        r = self.run_tool("review", str(self.tmp / "src"), "--unit", "u1", "--project", "p1", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("DRY RUN", r.stdout)
        self.assertFalse((self.tmp / "reviews" / "ledger.json").exists())

    def test_pem_block_fully_redacted(self):
        import re
        pem = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKj34\nGkxFhD90vcNLYLInFEX\n-----END " + "RSA PRIVATE KEY-----"
        text = "before\n" + pem + "\nafter"
        for pat in json.load(open(HERE / "settings_v1.json"))["secret_patterns"]:
            text = re.sub(pat, "[REDACTED-SECRET]", text)
        self.assertEqual(text, "before\n[REDACTED-SECRET]\nafter")

    def test_partial_review_does_not_suppress_full(self):
        src = self.tmp / "src"
        genv = {**self.env, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "@".join(["t", "example.invalid"]),
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "@".join(["t", "example.invalid"])}
        for cmd in (["init", "-q"], ["add", "."], ["commit", "-qm", "init"]):
            subprocess.run(["git", "-C", str(src), *cmd], env=genv, check=True, capture_output=True)
        (src / "a.py").write_text("x = 2\n")
        r1 = self.run_tool("review", str(src), "--unit", "u1", "--project", "p1", "--scope", "uncommitted")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        r2 = self.run_tool("review", str(src), "--unit", "u1", "--project", "p1")
        self.assertNotIn("already reviewed", r2.stdout)
        r3 = self.run_tool("review", str(src), "--unit", "u1", "--project", "p1")
        self.assertIn("already reviewed", r3.stdout)
        (src / "new.py").write_text("a = 1\n")
        self.assertEqual(self.run_tool("review", str(src), "--unit", "u1", "--project", "p1").returncode, 0)
        (src / "new.py").write_text("a = 2\n")  # untracked content edit must not reuse the old review
        self.assertNotIn("already reviewed", self.run_tool("review", str(src), "--unit", "u1", "--project", "p1").stdout)

    def test_escaped_and_straddling_secrets_redacted(self):
        esc = "\\u0067" + FAKE_KEY[1:]  # JSON escape of the first char: only matchable after decoding
        (self.tmp / "codex").write_text("#!/usr/bin/env python3\nimport sys\na = sys.argv\nopen(a[a.index('-o') + 1], 'w').write("
            + repr('{"summary": "s", "findings": [{"severity": "high", "title": "t", "file": "a.py", "line": 1, "body": "leak %s"}]}' % esc) + ")\n")
        r = self.run_tool("review", str(self.tmp / "src"), "--unit", "u1", "--project", "p1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(FAKE_KEY, (self.tmp / "reviews" / "ledger.json").read_text())
        self.assertNotIn(FAKE_KEY, self.run_tool("status", "--project", "p1").stdout)
        # a token straddling the 600-char stderr cut must not leak its tail
        (self.tmp / "codex").write_text("#!/usr/bin/env python3\nimport sys\nsys.stderr.write('x' * 50 + %r + 'y' * 580)\nsys.exit(1)\n" % FAKE_KEY)
        r = self.run_tool("review", str(self.tmp / "src"), "--unit", "u1", "--project", "p1", "--force")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn(FAKE_KEY[14:], r.stderr); self.assertIn("[REDACTED-SECRET]", r.stderr)

    def test_daily_cap(self):
        s = json.loads(self.settings.read_text()); s["max_reviews_per_day"] = 1; self.settings.write_text(json.dumps(s))
        self.run_tool("review", str(self.tmp / "src"), "--unit", "u1", "--project", "p1")
        r = self.run_tool("review", str(self.tmp / "src"), "--unit", "u2", "--project", "p1")
        self.assertIn("daily cap", r.stderr)


if __name__ == "__main__":
    unittest.main()
