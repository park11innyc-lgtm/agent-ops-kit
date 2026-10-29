import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paste_to_keychain_v1 as P


class T(unittest.TestCase):
    def test_names(self):
        self.assertTrue(P.valid_name("agent-ops-openrouter"))
        for bad in ("", "a b", "a;rm", "x\"y", "a" * 129, "-s$(id)", "-U"):
            self.assertFalse(P.valid_name(bad), bad)

    def test_prompt_cannot_break_out_of_applescript(self):
        s = P.dialog_script('evil" & do shell script "id', 't')
        self.assertIn('\\"', s)
        self.assertNotIn('evil" &', s)

    def test_clean_value(self):
        self.assertEqual(P.clean_value(" abc\n"), "abc")
        for bad in ("", "  ", 'a"b', "a'b", "a\\b"):
            self.assertIsNone(P.clean_value(bad))

    def test_control_chars_refused(self):
        self.assertIsNone(P.clean_value("abc\x00def"))
        self.assertIsNone(P.clean_value("abc\x7fdef"))

    def test_bad_name_refused_before_any_dialog(self):
        self.assertEqual(P.main(["bad name"]), 2)


if __name__ == "__main__":
    unittest.main()
