import io, json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wip_gate_v1 as W


def pr(n, draft=False, review=None):
    return {"number": n, "title": "t%d" % n, "isDraft": draft, "reviewDecision": review,
            "statusCheckRollup": [{"conclusion": "SUCCESS"}], "createdAt": "2026-01-0%dT00:00:00Z" % n}


class T(unittest.TestCase):
    def setUp(self):
        self.S = W.load_settings()
        self.S.update(state_dir=tempfile.mkdtemp(), repos={"p": ["o/r"]}, wip_limit={"p": 2}, enforce=True)

    def run_check(self, prs, now=1000):
        out = io.StringIO()
        return W.check(self.S, "p", gh=lambda a: prs, now=now, out=out), out.getvalue()

    def test_under_limit(self):
        self.assertEqual(self.run_check([pr(1), pr(2)])[0], 0)

    def test_over_limit_holds(self):
        rc, o = self.run_check([pr(1), pr(2), pr(3)])
        self.assertEqual(rc, 3); self.assertIn("HOLD", o)

    def test_drafts_and_approved_not_counted(self):
        self.assertEqual(self.run_check([pr(1), pr(2), pr(3, draft=True), pr(4, review="APPROVED")])[0], 0)

    def test_observe_mode_warns(self):
        self.S["enforce"] = False
        rc, o = self.run_check([pr(1), pr(2), pr(3)])
        self.assertEqual(rc, 0); self.assertIn("WARN", o)

    def test_gh_failure_never_blocks(self):
        rc, o = self.run_check(None)
        self.assertEqual(rc, 0); self.assertIn("UNKNOWN", o)

    def test_snapshot_reused_until_stale(self):
        self.run_check([pr(1), pr(2), pr(3)], now=1000)
        self.assertEqual(self.run_check([], now=1000 + 60)[0], 3)              # fresh snapshot: no re-read
        self.assertEqual(self.run_check([], now=1000 + 41 * 60)[0], 0)         # stale: re-collected

    def test_untracked_project(self):
        self.assertEqual(W.check(self.S, "other", gh=lambda a: [], out=io.StringIO()), 0)


if __name__ == "__main__":
    unittest.main()
