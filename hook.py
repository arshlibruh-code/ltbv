#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / ".venv" / "bin" / "python"
DAEMON = ROOT / "daemon.py"
BASE_URL = "http://127.0.0.1:7333"
DISABLED = ROOT / ".voice-disabled"


def request(path: str, payload: dict | None = None, timeout: float = 0.08) -> bool:
    try:
        if payload is None:
            urllib.request.urlopen(BASE_URL + path, timeout=timeout).read()
        else:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                BASE_URL + path,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=timeout).read()
        return True
    except Exception:
        return False


def speak(payload: dict, retry: bool = True) -> None:
    if request("/speak", payload, 0.08):
        return
    if retry:
        time.sleep(0.3)
        request("/speak", payload, 0.08)


def daemon_up() -> bool:
    return request("/health", None, 0.05)


def start_daemon() -> None:
    try:
        subprocess.Popen(
            [str(PYTHON), str(DAEMON)],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        return


def ensure_daemon() -> None:
    if daemon_up():
        return
    start_daemon()
    deadline = time.perf_counter() + 0.25
    while time.perf_counter() < deadline:
        if daemon_up():
            return
        time.sleep(0.02)


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0

    if DISABLED.exists():
        return 0

    event = payload.get("hook_event_name") or payload.get("event") or ""
    ensure_daemon()

    if event == "UserPromptSubmit":
        prompt = payload.get("prompt") or payload.get("user_prompt") or ""
        request(
            "/turn/start",
            {
                "cwd": payload.get("cwd"),
                "prompt": prompt,
                "session_id": payload.get("session_id"),
                "turn_id": payload.get("turn_id") or payload.get("prompt_id"),
                "transcript_path": payload.get("transcript_path"),
            },
            0.2,
        )
        return 0

    agent = "claude" if os.environ.get("CLAUDE_PROJECT_DIR") else "codex"

    if event == "Notification":
        cwd = payload.get("cwd")
        project = Path(cwd).name if cwd else "This project"
        speak(
            {
                "event": event,
                "text": f"{project} needs your input",
                "ts": time.time(),
                "cwd": cwd,
                "agent": agent,
                "session_id": payload.get("session_id"),
                "turn_id": payload.get("turn_id") or payload.get("prompt_id"),
            }
        )
        return 0

    if event != "Stop":
        return 0

    text = payload.get("last_assistant_message") or ""
    transcript_path = payload.get("transcript_path")
    if not text.strip() and not transcript_path:
        return 0

    speak(
        {
            "event": event,
            "text": text,
            "ts": time.time(),
            "cwd": payload.get("cwd"),
            "agent": agent,
            "transcript_path": transcript_path,
            "session_id": payload.get("session_id"),
            "turn_id": payload.get("turn_id") or payload.get("prompt_id"),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
