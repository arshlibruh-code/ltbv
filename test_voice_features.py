import json
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import voice_features as vf


class VoiceFeaturesTest(unittest.TestCase):
    def test_speech_eval_cases(self):
        cases = json.loads(Path("speech-evals.json").read_text())["cases"]
        for case in cases:
            with self.subTest(case=case["name"]):
                spoken, meta = vf.enforce_spoken_contract(
                    case["candidate"],
                    case["source"],
                    case["intent"],
                    case["budget"],
                    case.get("diff_summary", ""),
                )
                lowered = spoken.lower()
                self.assertEqual(meta["contract"], case["mode"])
                for phrase in case["must_include"]:
                    self.assertIn(phrase, lowered)
                for phrase in case["must_not_include"]:
                    self.assertNotIn(phrase, lowered)

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

    def test_diff_summary_uses_concepts(self):
        self.assertEqual(vf.concept_for_path("daemon.py"), "voice daemon")
        self.assertEqual(vf.concept_for_path("browser-extension/content.js"), "browser extension")

    def test_request_intent_is_compact(self):
        self.assertEqual(
            vf.request_intent("Fix this, run smoke, but do not commit or push."),
            "fix + test + do not commit + do not push",
        )

    def test_git_summary_ignores_unchanged_dirty_baseline(self):
        before = {
            "root": "/repo",
            "head": "abc",
            "branch": "feat",
            "status": " M daemon.py",
            "fingerprints": {},
        }
        outputs = {
            ("rev-parse", "--show-toplevel"): "/repo",
            ("rev-parse", "HEAD"): "abc",
            ("status", "--porcelain=v1", "--untracked-files=all"): " M daemon.py",
            ("branch", "--show-current"): "feat",
        }
        with mock.patch.object(vf, "_run_git", side_effect=lambda cwd, *args: outputs.get(args, "")):
            result = vf.git_change_summary("/repo", before)
        self.assertEqual(result["count"], 0)
        self.assertTrue(result["verified"])

    def test_claude_transcript_test_evidence(self):
        started = datetime.fromisoformat("2026-07-13T08:30:00+00:00").timestamp()
        evidence = vf.transcript_tool_evidence("test-fixtures/claude-transcript.jsonl", started_at=started)
        self.assertEqual(evidence["verification"], "passed")
        self.assertEqual(evidence["tests"], ["smoke"])

    def test_codex_transcript_failed_evidence(self):
        evidence = vf.transcript_tool_evidence("test-fixtures/codex-transcript.jsonl", turn_id="turn-new")
        self.assertEqual(evidence["verification"], "failed")
        self.assertEqual(evidence["tests"], ["unit tests"])

    def test_git_branch_change_is_explicit(self):
        before = {"root": "/repo", "head": "abc", "branch": "old", "status": "", "fingerprints": {}}
        outputs = {
            ("rev-parse", "--show-toplevel"): "/repo",
            ("rev-parse", "HEAD"): "abc",
            ("status", "--porcelain=v1", "--untracked-files=all"): "",
            ("branch", "--show-current"): "new",
        }
        with mock.patch.object(vf, "_run_git", side_effect=lambda cwd, *args: outputs.get(args, "")):
            result = vf.git_change_summary("/repo", before)
        self.assertTrue(result["branch_changed"])
        self.assertIn("switched from old to new", result["summary"].lower())

    def test_semantic_diff_extracts_behavior_and_coverage(self):
        patch = '\n'.join([
            '--- a/config.py',
            '+++ b/config.py',
            '-"duck_target": 25,',
            '+"duck_target": 15,',
            '+def test_same_repo_parallel_turns():',
        ])
        before = {"head": "abc", "status": ""}
        with mock.patch.object(vf, "_run_git", return_value=patch):
            facts = vf.semantic_diff_facts("/repo", before, ["config.py"], "abc")
        self.assertIn("duck target changed from 25 to 15", facts)
        self.assertIn("added coverage for same repo parallel turns", facts)

    def test_semantic_diff_from_real_git_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            target = root / "settings.py"
            target.write_text('"duck_target": 25,\n')
            subprocess.run(["git", "-C", str(root), "add", "settings.py"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "baseline"], check=True)
            before = vf.git_snapshot(str(root))
            target.write_text('"duck_target": 15,\n')
            result = vf.git_change_summary(str(root), before)
        self.assertIn("duck target changed from 25 to 15", result["semantic"])

    def test_contract_rejects_meta_speech(self):
        spoken, meta = vf.enforce_spoken_contract(
            "No action needed; just run git status again.",
            "Updated daemon.py and controller.html. SMOKE PASS.",
            "success",
            16,
            "Updated the voice daemon and controller.",
        )
        self.assertEqual(meta["contract"], "fallback")
        self.assertNotIn("no action needed", spoken.lower())
        self.assertIn("verification passed", spoken.lower())

    def test_contract_exposes_untested_work(self):
        spoken, meta = vf.enforce_spoken_contract(
            "The controller is ready.",
            "Updated the controller. Visual check was not run.",
            "warning",
            24,
        )
        self.assertEqual(meta["contract"], "fallback")
        self.assertIn("not tested", spoken.lower())

    def test_contract_uses_earcon_for_trivial_success(self):
        spoken, meta = vf.enforce_spoken_contract("Done.", "Done.", "success", 16)
        self.assertEqual(spoken, "")
        self.assertEqual(meta["contract"], "earcon_only")


if __name__ == "__main__":
    unittest.main()
