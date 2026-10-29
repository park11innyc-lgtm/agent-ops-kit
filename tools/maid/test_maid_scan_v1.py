import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        src = self.tmp / "proj"; src.mkdir()
        (src / "app.py.bak").write_text("x")
        (src / "tool_v1.py").write_text("x = 1\n"); (src / "tool_v2.py").write_text("x = 2\n")
        (src / "lib.py").write_text("def f():\n    # TODO remove after launch\n    return 1\n")
        (src / "test_empty.py").write_text("def test_nothing():\n    pass\n")
        mem = self.tmp / "mem"; mem.mkdir()
        (mem / "MEMORY.md").write_text("- [a](a.md)\n- [gone](gone.md)\n")
        (mem / "a.md").write_text("see [[b]]\n"); (mem / "b.md").write_text("x"); (mem / "orphan.md").write_text("x")
        s = json.load(open(HERE / "settings_v1.json"))
        s.update(report_dir=str(self.tmp / "reports"), state_dir=str(self.tmp / "state"),
                 targets={"p": [str(src)]}, memory_dirs={"p": str(mem)}, skip_projects=["dead"])
        self.cfg = self.tmp / "s.json"; self.cfg.write_text(json.dumps(s))

    def scan(self, *args):
        return subprocess.run([sys.executable, "-I", str(HERE / "maid_scan_v1.py"), *args], capture_output=True, text=True,
                              env={**os.environ, "MAID_SETTINGS": str(self.cfg)})

    def test_report(self):
        r = self.scan("p", "--stdout")
        self.assertEqual(r.returncode, 0, r.stderr); o = r.stdout
        self.assertIn("Backup files (.bak/.orig/~): **1**", o)
        self.assertIn("tool_v1.py (newest is v2)", o)
        self.assertIn("Stale-looking comments (TODO/temporary/legacy/remove-after…): **1**", o)
        self.assertIn("Test files with no assertion: **1**", o)
        self.assertIn("unreachable from MEMORY.md (direct or via other notes): **1**", o)   # b.md is reachable via [[b]]
        self.assertIn("missing `gone.md`", o)

    def test_read_only(self):
        before = sorted(p.name for p in (self.tmp / "proj").iterdir())
        self.scan("p", "--stdout")
        self.assertEqual(before, sorted(p.name for p in (self.tmp / "proj").iterdir()))

    def test_delta_and_skip(self):
        self.assertIn("no growth", self.scan("p", "--delta").stdout)
        (self.tmp / "proj" / "more.py.bak").write_text("x")
        self.assertIn("MAID grew", self.scan("p", "--delta").stdout)
        self.assertIn("Skipped project", self.scan("dead", "--stdout").stdout)


if __name__ == "__main__":
    unittest.main()
