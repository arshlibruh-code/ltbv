import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import daemon


class DaemonContractTest(unittest.TestCase):
    def setUp(self):
        daemon.pending.clear()
        daemon.generations.clear()
        daemon.worker_running = False
        daemon.generation = 0
        daemon.timeline_events.clear()
        daemon.voice_inbox.clear()

    def test_same_repo_parallel_turns_get_distinct_jobs(self):
        with mock.patch.object(daemon.threading, "Thread"):
            daemon.enqueue_speak("one", "/repo", session_id="a", turn_id="1")
            daemon.enqueue_speak("two", "/repo", session_id="b", turn_id="2")
        self.assertEqual(len(daemon.pending), 2)

    def test_zero_volume_never_ducks_media(self):
        original = daemon.config["volume"]
        try:
            daemon.config["volume"] = 0
            self.assertFalse(daemon.media_should_duck([b"\x01\x00"] * 20))
            daemon.config["volume"] = 1
            self.assertTrue(daemon.media_should_duck([b"\x01\x00"] * 20))
        finally:
            daemon.config["volume"] = original

    def test_turn_start_does_not_wait_for_git(self):
        blocker = mock.Mock()
        with mock.patch.object(daemon, "git_snapshot", blocker), mock.patch.object(daemon.threading, "Thread") as thread:
            record = daemon.store_turn({"cwd": "/repo", "prompt": "test it", "session_id": "a", "turn_id": "1"})
        blocker.assert_not_called()
        thread.assert_called_once()
        self.assertEqual(record["git_state"], "pending")

    def test_restart_state_excludes_prompt_but_keeps_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "turns.json"
            record = {
                "key": "s|t|/repo",
                "cwd": "/repo",
                "started_at": 9999999999,
                "request_intent": "fix + test",
                "prompt": "private prompt",
            }
            with mock.patch.object(daemon, "TURN_STATE_PATH", path):
                daemon.turn_records.clear()
                daemon.turn_records[record["key"]] = record
                daemon.save_turn_records()
                persisted = json.loads(path.read_text())[0]
                daemon.turn_records.clear()
                daemon.load_turn_records()
        self.assertNotIn("prompt", persisted)
        self.assertEqual(daemon.turn_records[record["key"]]["request_intent"], "fix + test")

    def test_radio_bulletin_receives_turn_evidence(self):
        turn = {"request_intent": "fix + test", "started_at": 1, "turn_id": "1", "git": None, "git_state": "pending"}
        job = {"source_jobs": [{"cwd": "/repo", "raw": "Finished.", "turn_id": "1"}]}
        with mock.patch.object(daemon, "take_turn", return_value=turn), \
             mock.patch.object(daemon, "transcript_tool_evidence", return_value={"verification": "passed", "tests": ["smoke"]}), \
             mock.patch.object(daemon, "condense", return_value="repo passed smoke") as condense, \
             mock.patch.object(daemon, "apply_pronunciations", side_effect=lambda text, cwd: (text, 0)):
            daemon.bulletin_content(job)
        source = condense.call_args.args[0]
        self.assertIn("Request intent: fix + test", source)
        self.assertIn("Actual tool evidence: smoke passed", source)

    def test_temporal_context_marks_resolved_failure(self):
        daemon.timeline_events.append({"cwd": "/repo", "verification": "failed", "intent": "blocker"})
        text = daemon.temporal_context("/repo", "passed", "success")
        self.assertIn("resolves", text)

    def test_recap_contains_only_structured_arc(self):
        daemon.timeline_events.extend([
            {"cwd": "/repo", "project": "repo", "verification": "failed", "intent": "blocker", "semantic": []},
            {"cwd": "/repo", "project": "repo", "verification": "passed", "intent": "success", "semantic": ["queue now preserves parallel turns"]},
        ])
        recap = daemon.recap_text("/repo")
        self.assertIn("earlier failure was resolved", recap.lower())
        self.assertIn("queue now preserves parallel turns", recap.lower())

    def test_voice_inbox_persists_and_drains_one_briefing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "inbox.json"
            with mock.patch.object(daemon, "INBOX_PATH", path), \
                 mock.patch.object(daemon, "append_log"), \
                 mock.patch.object(daemon, "publish"):
                daemon.inbox_add(
                    "/repo",
                    "Smoke passed and the queue now preserves both turns.",
                    {"verification": "passed", "verification_tests": ["smoke"]},
                    "success",
                )
                self.assertEqual(json.loads(path.read_text())[0]["project"], "repo")
                text, count = daemon.drain_voice_inbox()
        self.assertEqual(count, 1)
        self.assertIn("repo", text)
        self.assertIn("Smoke passed", text)
        self.assertEqual(daemon.voice_inbox, [])

    def test_inbox_only_holds_normal_agent_replies(self):
        original = daemon.config["voice_inbox_enabled"]
        try:
            daemon.config["voice_inbox_enabled"] = True
            self.assertTrue(daemon.should_hold_for_inbox("reply"))
            self.assertFalse(daemon.should_hold_for_inbox("notification"))
            self.assertFalse(daemon.should_hold_for_inbox("reply", prepared=True))
        finally:
            daemon.config["voice_inbox_enabled"] = original

    def test_media_lease_recovers_saved_spotify_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            lease = Path(tmp) / "lease.json"
            lease.write_text(json.dumps({"saved_volume": 72}))
            with mock.patch.object(daemon, "DUCK_LEASE_PATH", lease), \
                 mock.patch.object(daemon, "spotify_running", return_value=True), \
                 mock.patch.object(daemon, "spotify_volume", return_value=15), \
                 mock.patch.object(daemon, "fade_spotify_volume") as fade, \
                 mock.patch.object(daemon, "append_log"):
                self.assertTrue(daemon.recover_media_state())
            fade.assert_called_once_with(15, 72, daemon.config["duck_fade_ms"])
            self.assertFalse(lease.exists())

    def test_spotify_duck_never_raises_quiet_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            lease = Path(tmp) / "lease.json"
            with mock.patch.object(daemon, "DUCK_LEASE_PATH", lease), \
                 mock.patch.object(daemon, "spotify_running", return_value=True), \
                 mock.patch.object(daemon, "spotify_volume", return_value=10), \
                 mock.patch.object(daemon, "fade_spotify_volume") as fade:
                daemon.spotify_duck_on()
            fade.assert_not_called()
            self.assertFalse(lease.exists())


if __name__ == "__main__":
    unittest.main()
