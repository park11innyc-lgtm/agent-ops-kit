import io, json, os, sys, tempfile, unittest
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import route_v1 as R

KEY = "sk-" + "TESTSECRET-1234567890"  # built at runtime so the repo holds no key-shaped literal
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = R.load_settings()
        self.ledger = os.path.join(self.tmp, "l.jsonl")
        self.cfg["lanes"]["ledger_path"] = self.ledger
        self.calls = []

    def post(self, url, headers, body, timeout):
        self.calls.append((url, body))
        if "openrouter" in url:
            return 200, {"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 1000, "completion_tokens": 500}}
        return 200, {"content": [{"text": "hi"}], "usage": {"input_tokens": 1000, "output_tokens": 500}}

    def run_r(self, args, key=KEY):
        out = io.StringIO()
        rc = R.main(args, cfg=self.cfg, keyfn=lambda s, a=None: key, post=self.post, now=NOW, out=out)
        return rc, out.getvalue()

    def test_lane_choice(self):
        self.assertEqual(json.loads(self.run_r(["x", "--kind", "draft"])[1])["lane"], "openrouter")
        d = json.loads(self.run_r(["x", "--kind", "spec-critique"])[1])
        self.assertEqual((d["lane"], d["model"]), ("anthropic_api", "claude-opus-5-5"))
        d = json.loads(self.run_r(["x", "--kind", "local-only", "--run"])[1])
        self.assertEqual(d["lane"], "plan"); self.assertEqual(self.calls, [])

    def test_pii_reroute(self):
        rc, o = self.run_r(["mail bob" + "@" + "example.com", "--kind", "draft", "--run"])
        d = json.loads(o)
        self.assertEqual(d["lane"], "anthropic_api"); self.assertTrue(d["rerouted"])
        self.assertTrue(all("openrouter" not in u for u, _ in self.calls))
        f = os.path.join(self.tmp, "f.txt"); open(f, "w").write("call 555-555-0100")
        d = json.loads(self.run_r(["clean", "--kind", "draft", "--files", f])[1])
        self.assertTrue(d["rerouted"])

    def test_ceiling(self):
        open(self.ledger, "w").write(json.dumps({"at": "2026-10-17T01:00:00Z", "lane": "openrouter", "usd": 10.0}) + "\n")
        rc, o = self.run_r(["x", "--kind", "draft", "--run"])
        self.assertEqual(rc, 4); self.assertTrue(json.loads(o)["refused"]); self.assertEqual(self.calls, [])
        # yesterday's spend does not count
        open(self.ledger, "w").write(json.dumps({"at": "2026-10-16T01:00:00Z", "lane": "openrouter", "usd": 50}) + "\n")
        self.assertEqual(self.run_r(["x", "--kind", "draft", "--run"])[0], 0)

    def test_ledger_and_no_key_leak(self):
        outf = os.path.join(self.tmp, "o.txt")
        rc, o = self.run_r(["x", "--kind", "draft", "--run", "--out", outf])
        self.assertEqual(rc, 0); self.assertEqual(open(outf).read(), "hi")
        rec = json.loads(open(self.ledger).read().splitlines()[-1])
        for k in ("at", "lane", "model", "in", "out", "usd"):
            self.assertIn(k, rec)
        self.assertEqual((rec["in"], rec["out"]), (1000, 500)); self.assertGreater(rec["usd"], 0)
        self.assertNotIn(KEY, o); self.assertNotIn(KEY, open(self.ledger).read())

    def test_failed_call_books_worst_case(self):
        def boom(*a, **k):
            raise TimeoutError("slow")
        self.post = boom
        rc, o = self.run_r(["x", "--kind", "draft", "--run"])
        self.assertEqual(rc, 1)
        rec = json.loads(open(self.ledger).read().splitlines()[-1])
        self.assertEqual(rec["status"], "error:TimeoutError"); self.assertGreater(rec["usd"], 0)

    def test_attached_secret_stays_local(self):
        f = os.path.join(self.tmp, "notes.md"); open(f, "w").write("db " + "pass" + "word: hunter2-example\n")
        rc, o = self.run_r(["summarize this file", "--files", f, "--run"])
        d = json.loads(o)
        self.assertEqual(d["lane"], "plan"); self.assertTrue(d["rerouted"]); self.assertEqual(self.calls, [])

    def test_missing_key(self):
        rc, o = self.run_r(["x", "--kind", "draft", "--run"], key=None)
        self.assertEqual(rc, 3); self.assertEqual(self.calls, [])


class ReviewFixes(unittest.TestCase):
    def test_compact_phone_is_caught(self):
        cfg = R.load_settings()
        self.assertTrue(R.pii_hits("call (555)555-0100", cfg))
        self.assertTrue(R.pii_hits("call 5555550100 now", cfg))

    def test_local_only_words_beat_generic_words(self):
        cfg = R.load_settings()
        local = [k for k, v in cfg["lanes"]["kind_to_lane"].items() if v == "plan" and k in cfg["lanes"]["kind_keywords"]]
        self.assertTrue(local)
        w = cfg["lanes"]["kind_keywords"][local[0]][0]
        self.assertNotEqual(R.decide("summarize " + w, "summarize " + w, None, cfg)["lane"], "openrouter")


if __name__ == "__main__":
    unittest.main()
