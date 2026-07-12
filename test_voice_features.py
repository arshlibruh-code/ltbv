import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import voice_features as vf


class VoiceFeaturesTest(unittest.TestCase):
    def test_intent_and_build_signals(self):
        self.assertEqual(vf.classify_intent("SMOKE FAIL, permission denied"), "blocker")
        self.assertEqual(vf.classify_intent("All checks passed. Done."), "success")
        self.assertEqual(vf.classify_intent("Which one do you want?"), "needs_input")
        self.assertEqual(vf.detect_build_signal("SMOKE PASS"), "tests_passed")

    def test_adaptive_budgets(self):
        self.assertLess(vf.adaptive_word_budget("success"), vf.adaptive_word_budget("blocker"))
        self.assertEqual(vf.adaptive_word_budget("update", kind="notification"), 9)
        self.assertEqual(len(vf.trim_to_words("one two three four five", 3).split()), 3)

    def test_redacts_known_secret_shapes(self):
        raw = "token=abcdef123456 and sk-abcdefghijklmnopqrstuvwxyz"
        clean, count = vf.redact_sensitive(raw)
        self.assertNotIn("abcdef123456", clean)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", clean)
        self.assertGreaterEqual(count, 2)

    def test_project_pronunciations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".ltbv").mkdir()
            (root / ".ltbv" / "pronounce.json").write_text(
                json.dumps({"WebGPU": "Web G P U", "ltbv": "let there be voice"})
            )
            with mock.patch.object(vf, "git_root", return_value=str(root)):
                spoken, count = vf.apply_pronunciations("WebGPU powers ltbv.", str(root))
            self.assertEqual(spoken, "Web G P U powers let there be voice.")
            self.assertEqual(count, 2)

    def test_earcon_is_stable_pcm(self):
        first, meta = vf.earcon_pcm("stagr", "success", "tests_passed", 24000)
        second, _ = vf.earcon_pcm("stagr", "success", "tests_passed", 24000)
        other, _ = vf.earcon_pcm("holision", "success", "tests_passed", 24000)
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertEqual(len(first) % 2, 0)
        self.assertTrue(meta["repo_earcon"])
        self.assertEqual(meta["build_signal"], "tests_passed")


if __name__ == "__main__":
    unittest.main()
