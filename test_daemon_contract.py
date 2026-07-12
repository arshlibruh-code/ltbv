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

    def test_same_repo_parallel_turns_get_distinct_jobs(self):
        with mock.patch.object(daemon.threading, "Thread"):
            daemon.enqueue_speak("one", "/repo", session_id="a", turn_id="1")
            daemon.enqueue_speak("two", "/repo", session_id="b", turn_id="2")
        self.assertEqual(len(daemon.pending), 2)

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


if __name__ == "__main__":
    unittest.main()
