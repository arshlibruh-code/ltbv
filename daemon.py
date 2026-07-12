#!/usr/bin/env python3
import array
import base64
import binascii
import importlib.util
import json
import os
import queue
import re
import resource
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
import wave
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from voice_features import (
    adaptive_word_budget,
    apply_pronunciations,
    classify_intent,
    detect_build_signal,
    earcon_pcm,
    enforce_spoken_contract,
    git_change_summary,
    git_snapshot,
    redact_sensitive,
    request_intent,
    transcript_tool_evidence,
    trim_to_words,
)

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 7333
TABLE_FALLBACK_TEXT = (
    "I made a table for this, but I won't read the whole thing out loud. "
    "Take a look and tell me what you think."
)
PREFIX_TEMPLATES = ("In {project}: ", "From {project}: ", "Update from {project}: ")
LOG_PATH = ROOT / ".voice.log"
CONFIG_PATH = ROOT / "config.json"
TURN_STATE_PATH = ROOT / ".turns.json"
TIMELINE_PATH = ROOT / ".timeline.json"
INBOX_PATH = ROOT / ".voice-inbox.json"
DUCK_LEASE_PATH = ROOT / ".media-duck-lease.json"
CONTROLLER_PATH = ROOT / "controller.html"
RING_DIR = ROOT / ".clips"
VOICES_DIR = ROOT / "voices"
KILL_PATH = ROOT / ".voice-disabled"
AUDITION_TEXT = "This is {voice}, speaking for your coding agents."
FFPLAY = shutil.which("ffplay")
FFMPEG = shutil.which("ffmpeg")
RING_SIZE = 8
PEAK_TARGET = 160
SAMPLE_RATE = 24000

DEFAULT_CONDENSE_PROMPT = (
    "You are speaking for a coding agent after it has finished a turn. "
    "The user hears this out loud, so speak the smallest truthful thing that lets them continue without looking. "
    "Lead with the result, blocker, or exact decision required. Mention evidence and uncertainty when they matter. "
    "Never discuss summarizing, prompts, the assistant, or the user. "
    "If the reply contains a table, summarize the table's meaning conversationally. "
)

CATALOG_VOICES = (
    "alba",
    "anna",
    "azelma",
    "bill_boerst",
    "caro_davy",
    "charles",
    "cosette",
    "eponine",
    "estelle",
    "eve",
    "fantine",
    "george",
    "giovanni",
    "jane",
    "javert",
    "jean",
    "juergen",
    "lola",
    "marius",
    "mary",
    "michael",
    "paul",
    "peter_yearsley",
    "rafael",
    "stuart_bell",
    "vera",
)

KOKORO_VOICES = (
    "af_alloy",
    "af_aoede",
    "af_bella",
    "af_heart",
    "af_jessica",
    "af_kore",
    "af_nicole",
    "af_nova",
    "af_river",
    "af_sarah",
    "af_sky",
    "am_adam",
    "am_echo",
    "am_eric",
    "am_fenrir",
    "am_liam",
    "am_michael",
    "am_onyx",
    "am_puck",
    "am_santa",
    "bf_alice",
    "bf_emma",
    "bf_isabella",
    "bf_lily",
    "bm_daniel",
    "bm_fable",
    "bm_george",
    "bm_lewis",
    "ef_dora",
    "em_alex",
    "em_santa",
    "ff_siwis",
    "hf_alpha",
    "hf_beta",
    "hm_omega",
    "hm_psi",
    "if_sara",
    "im_nicola",
    "jf_alpha",
    "jf_gongitsune",
    "jf_nezumi",
    "jf_tebukuro",
    "jm_kumo",
    "pf_dora",
    "pm_alex",
    "pm_santa",
    "zf_xiaobei",
    "zf_xiaoni",
    "zf_xiaoxiao",
    "zf_xiaoyi",
    "zm_yunjian",
    "zm_yunxi",
    "zm_yunxia",
    "zm_yunyang",
)


def torch_to_pcm(audio) -> bytes:
    data = audio.detach().cpu().clamp(-1, 1).numpy()
    return (data * 32767).astype("<i2").tobytes()


class TTSEngine:
    name = ""
    sample_rate = SAMPLE_RATE
    install_command = ""

    def installed(self) -> bool:
        return True

    def loaded(self) -> bool:
        return False

    def load(self):
        raise NotImplementedError

    def voices(self) -> list:
        raise NotImplementedError

    def synth(self, text: str, voice: str):
        raise NotImplementedError


class PocketEngine(TTSEngine):
    name = "pocket"
    sample_rate = SAMPLE_RATE
    install_command = "uv add pocket-tts"

    def __init__(self):
        self.model = None
        self.voice_states = {}

    def loaded(self) -> bool:
        return self.model is not None

    def load(self):
        if self.model is None:
            from pocket_tts.models.tts_model import TTSModel

            self.model = TTSModel.load_model(
                language="english", temp=float(config["temperature"])
            )
            self.sample_rate = self.model.sample_rate
        return self.model

    def voices(self) -> list:
        return list(CATALOG_VOICES) + [
            v for v in custom_voices() if v not in CATALOG_VOICES
        ]

    def voice_source(self, name: str) -> str:
        custom = VOICES_DIR / f"{name}.wav"
        return str(custom) if custom.exists() else name

    def voice_state_for(self, name: str):
        model = self.load()
        if name not in self.voice_states:
            self.voice_states[name] = model.get_state_for_audio_prompt(
                self.voice_source(name)
            )
        return self.voice_states[name]

    def synth(self, text: str, voice: str):
        model = self.load()
        voice_state = self.voice_state_for(voice)
        for chunk in model.generate_audio_stream(
            model_state=voice_state, text_to_generate=text
        ):
            yield torch_to_pcm(chunk)

    def reset(self) -> None:
        self.model = None
        self.voice_states = {}

    def drop_voice(self, name: str) -> None:
        self.voice_states.pop(name, None)


class KokoroEngine(TTSEngine):
    name = "kokoro"
    sample_rate = SAMPLE_RATE
    install_command = "uv add 'kokoro>=0.9.4'"

    def __init__(self):
        self.pipelines = {}

    def installed(self) -> bool:
        return importlib.util.find_spec("kokoro") is not None

    def loaded(self) -> bool:
        return bool(self.pipelines)

    def load(self, lang_code: str = "a"):
        if not self.installed():
            raise RuntimeError(f"kokoro is not installed. Run: {self.install_command}")
        if lang_code not in self.pipelines:
            os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
            import torch
            from kokoro import KPipeline

            device = "cpu"
            if (
                getattr(torch.backends, "mps", None)
                and torch.backends.mps.is_available()
            ):
                device = "mps"
            self.pipelines[lang_code] = KPipeline(
                lang_code=lang_code, repo_id="hexgrad/Kokoro-82M", device=device
            )
        return self.pipelines[lang_code]

    def voices(self) -> list:
        return list(KOKORO_VOICES)

    def synth(self, text: str, voice: str):
        if voice not in KOKORO_VOICES:
            voice = "af_heart"
        pipeline = self.load(voice[0])
        for result in pipeline(text, voice=voice, speed=1, split_pattern=r"\n+"):
            if result.audio is not None:
                yield torch_to_pcm(result.audio)


ENGINES = {"pocket": PocketEngine(), "kokoro": KokoroEngine()}


def custom_voices() -> list:
    try:
        return sorted(p.stem for p in VOICES_DIR.glob("*.wav"))
    except Exception:
        return []


def all_voices() -> list:
    return active_engine().voices()


CONFIG_SPEC = {
    "engine": {"default": "pocket", "kind": "engine"},
    "max_direct_chars": {"default": 400, "kind": "int", "min": 50, "max": 5000},
    "idle_exit_s": {"default": 600, "kind": "int", "min": 60, "max": 86400},
    "project_window_s": {"default": 600, "kind": "int", "min": 30, "max": 7200},
    "condense_provider": {"default": "ollama", "kind": "condense_provider"},
    "condense_ollama_model": {"default": "qwen3.5:4b", "kind": "str"},
    "condense_timeout_s": {"default": 30, "kind": "int", "min": 2, "max": 60},
    "rate": {"default": 1.0, "kind": "float", "min": 0.5, "max": 3.0},
    "volume": {"default": 1.0, "kind": "float", "min": 0.0, "max": 1.0},
    "temperature": {"default": 0.7, "kind": "float", "min": 0.1, "max": 1.5},
    "ducking_enabled": {"default": False, "kind": "bool"},
    "browser_youtube_ducking_enabled": {"default": False, "kind": "bool"},
    "browser_youtube_duck_target_volume": {"default": 15, "kind": "int", "min": 0, "max": 100},
    "repo_earcons_enabled": {"default": True, "kind": "bool"},
    "intent_earcons_enabled": {"default": True, "kind": "bool"},
    "build_sonification_enabled": {"default": True, "kind": "bool"},
    "adaptive_brevity_enabled": {"default": True, "kind": "bool"},
    "diff_narration_enabled": {"default": True, "kind": "bool"},
    "privacy_sentinel_enabled": {"default": True, "kind": "bool"},
    "radio_bulletins_enabled": {"default": True, "kind": "bool"},
    "radio_batch_window_ms": {"default": 450, "kind": "int", "min": 0, "max": 3000},
    "voice_inbox_enabled": {"default": False, "kind": "bool"},
    "duck_target_volume": {"default": 25, "kind": "int", "min": 0, "max": 100},
    "duck_fade_ms": {"default": 400, "kind": "int", "min": 0, "max": 3000},
    "duck_restore_delay_ms": {"default": 150, "kind": "int", "min": 0, "max": 3000},
    "quiet_hours": {"default": None, "kind": "quiet"},
    "voices": {
        "default": {"claude": "alba", "codex": "michael", "notification": "eve"},
        "kind": "voices",
    },
    "condense_prompt": {"default": DEFAULT_CONDENSE_PROMPT, "kind": "str"},
    "stream_playback": {"default": True, "kind": "bool"},
}


def git_rev() -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


GIT_REV = git_rev()


def git_runtime_state() -> dict:
    try:
        branch = subprocess.run(
            ["git", "-C", str(ROOT), "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "-C", str(ROOT), "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.strip()
        )
        return {"rev": git_rev(), "branch": branch or "detached", "dirty": dirty}
    except Exception:
        return {"rev": GIT_REV, "branch": "unknown", "dirty": None}


def hook_install_status() -> dict:
    expected = str(ROOT / "hook.py")
    events = ("Stop", "UserPromptSubmit", "Notification")
    status = {"claude": {}, "codex": {}}
    try:
        data = json.loads((Path.home() / ".claude" / "settings.json").read_text())
        hooks = data.get("hooks") or {}
        for event in events:
            status["claude"][event] = expected in json.dumps(hooks.get(event) or [])
    except Exception:
        status["claude"] = {event: False for event in events}
    try:
        text = (Path.home() / ".codex" / "config.toml").read_text()
        has_command = expected in text
        for event in events:
            status["codex"][event] = has_command and f"[[hooks.{event}]]" in text
    except Exception:
        status["codex"] = {event: False for event in events}
    return status


def ollama_reachable() -> bool:
    try:
        urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=0.5).read(1)
        return True
    except Exception:
        return False


def doctor_snapshot() -> dict:
    hooks = hook_install_status()
    provider = config.get("condense_provider", "none")
    ollama = ollama_reachable()
    condense_ok = provider == "none" or (provider == "ollama" and ollama)
    condense_detail = {
        "none": "disabled · local",
        "ollama": f"ollama · local · {'reachable' if ollama else 'not running'}",
    }.get(provider, provider)
    checks = [
        {"name": "daemon", "ok": True, "detail": f"127.0.0.1:{PORT}", "required": True},
        {
            "name": "tts",
            "ok": active_engine().installed(),
            "detail": f"{active_engine_name()} · {'loaded' if active_engine().loaded() else 'cold'}",
            "required": True,
        },
        {
            "name": "claude hooks",
            "ok": all(hooks["claude"].values()),
            "detail": ", ".join(name for name, ok in hooks["claude"].items() if not ok) or "all events",
            "required": True,
        },
        {
            "name": "codex hooks",
            "ok": all(hooks["codex"].values()),
            "detail": ", ".join(name for name, ok in hooks["codex"].items() if not ok) or "all events",
            "required": True,
        },
        {
            "name": "condense",
            "ok": condense_ok,
            "detail": condense_detail,
            "required": provider != "none",
        },
        {
            "name": "speech switch",
            "ok": not muted(),
            "detail": "enabled" if not muted() else "shut up is on",
            "required": False,
        },
        {
            "name": "browser adapter",
            "ok": (ROOT / "browser-extension" / "manifest.json").exists(),
            "detail": "packaged, browser load is manual",
            "required": False,
        },
        {
            "name": "playback",
            "ok": bool(FFPLAY or shutil.which("afplay")),
            "detail": "ffplay" if FFPLAY else "afplay",
            "required": True,
        },
    ]
    required_ok = all(check["ok"] for check in checks if check["required"])
    return {
        "ok": required_ok,
        "checks": checks,
        "hooks": hooks,
        "git": git_runtime_state(),
        "provider": provider,
        "locality": "local",
    }

state_lock = threading.Lock()
pending = {}
worker_running = False
generation = 0
generations = {}
active_player = None
active_cwd = None
active_cwds = set()
working_cwd = None
working_cwds = set()
last_activity = time.monotonic()
started_at = time.monotonic()
last_spoken_ts = None
last_spoken_text = None
last_spoken_clip = None
last_stages = None
now_speaking = None
sse_subscribers = []
sse_lock = threading.Lock()
recent_projects = {}
turn_records = {}
timeline_events = []
voice_inbox = []
prefix_counter = 0
clip_ring = OrderedDict()


def turn_key(cwd=None, session_id=None, turn_id=None) -> str:
    return "|".join(str(value or "") for value in (session_id, turn_id, cwd))


def job_key(cwd=None, kind="reply", session_id=None, turn_id=None) -> tuple:
    if kind == "reply" and (session_id or turn_id):
        return (cwd, kind, session_id, turn_id)
    return (cwd, kind)


def save_turn_records() -> None:
    try:
        cutoff = time.time() - 86400
        records = [
            {key: value for key, value in record.items() if key != "prompt"}
            for record in turn_records.values()
            if float(record.get("started_at") or 0) >= cutoff
        ][-64:]
        TURN_STATE_PATH.write_text(json.dumps(records, indent=2) + "\n")
    except Exception:
        append_log("turn_state_error", action="save")


def load_turn_records() -> None:
    try:
        records = json.loads(TURN_STATE_PATH.read_text()) if TURN_STATE_PATH.exists() else []
        cutoff = time.time() - 86400
        for record in records:
            if float(record.get("started_at") or 0) >= cutoff:
                turn_records[record["key"]] = record
    except Exception:
        append_log("turn_state_error", action="load")


def store_turn(data: dict) -> dict:
    cwd = data.get("cwd")
    prompt, _ = redact_sensitive(str(data.get("prompt") or ""), cwd)
    key = turn_key(cwd, data.get("session_id"), data.get("turn_id"))
    record = {
        "key": key,
        "cwd": cwd,
        "session_id": data.get("session_id"),
        "turn_id": data.get("turn_id"),
        "transcript_path": data.get("transcript_path"),
        "started_at": time.time(),
        "request_intent": request_intent(prompt),
        "git": None,
        "git_state": "pending" if cwd else "unavailable",
        "prompt": prompt,
    }
    with state_lock:
        turn_records[key] = record
        save_turn_records()
    def capture() -> None:
        snapshot = git_snapshot(cwd)
        with state_lock:
            current = turn_records.get(key)
            if not current:
                return
            current["git"] = snapshot or None
            current["git_state"] = "ready" if snapshot else "unavailable"
            save_turn_records()

    threading.Thread(target=capture, daemon=True).start()
    return record


def take_turn(job: dict) -> dict:
    cwd = job.get("cwd")
    exact = turn_key(cwd, job.get("session_id"), job.get("turn_id"))
    with state_lock:
        record = turn_records.pop(exact, None)
        if not record:
            candidates = [
                item for item in turn_records.values()
                if item.get("cwd") == cwd
                and (not job.get("session_id") or item.get("session_id") == job.get("session_id"))
            ]
            if candidates:
                record = max(candidates, key=lambda item: item.get("started_at", 0))
                turn_records.pop(record["key"], None)
        save_turn_records()
    return record or {}


def load_timeline() -> None:
    global timeline_events
    try:
        raw = json.loads(TIMELINE_PATH.read_text()) if TIMELINE_PATH.exists() else []
        cutoff = time.time() - 7 * 86400
        timeline_events = [event for event in raw if float(event.get("ts") or 0) >= cutoff][-200:]
    except Exception:
        timeline_events = []
        append_log("timeline_error", action="load")


def save_timeline() -> None:
    try:
        TIMELINE_PATH.write_text(json.dumps(timeline_events[-200:], indent=2) + "\n")
    except Exception:
        append_log("timeline_error", action="save")


def temporal_context(cwd: str | None, verification: str, intent: str) -> str:
    recent = [event for event in timeline_events if event.get("cwd") == cwd][-8:]
    if not recent:
        return ""
    prior = recent[-1]
    if prior.get("verification") == "failed" and verification == "passed":
        return "Earlier verification failed; this turn resolves it with passing checks."
    if prior.get("intent") == "blocker" and intent == "success":
        return "The previous turn was blocked; this turn reports the resolution."
    return ""


def remember_timeline(cwd: str | None, meta: dict, intent: str, build_signal: str | None) -> None:
    event = {
        "ts": round(time.time(), 3),
        "cwd": cwd,
        "project": project_name(cwd),
        "intent": intent,
        "request_intent": meta.get("request_intent") or "",
        "verification": meta.get("verification") or "unknown",
        "tests": meta.get("verification_tests") or [],
        "semantic": meta.get("semantic") or [],
        "diff_files": int(meta.get("diff_files") or 0),
        "build_signal": build_signal,
    }
    timeline_events.append(event)
    del timeline_events[:-200]
    save_timeline()


def recap_text(cwd: str | None = None) -> str:
    events = [event for event in timeline_events if not cwd or event.get("cwd") == cwd][-12:]
    if not events:
        return "No recent work is recorded."
    projects = []
    for event in events:
        name = event.get("project") or "project"
        if name not in projects:
            projects.append(name)
    latest = events[-1]
    failures = [event for event in events if event.get("verification") == "failed" or event.get("intent") == "blocker"]
    passed_after = failures and latest.get("verification") == "passed"
    facts = [fact for event in events for fact in event.get("semantic") or []]
    prefix = f"Recent work across {', '.join(projects[:3])}. " if len(projects) > 1 else f"In {projects[0]}. "
    if passed_after:
        prefix += "An earlier failure was resolved and the latest checks passed. "
    elif failures:
        prefix += "A blocker or failed check remains in the recent arc. "
    elif latest.get("verification") == "passed":
        prefix += "The latest checks passed. "
    if facts:
        prefix += facts[-1].rstrip(".") + "."
    return trim_to_words(prefix, 38)


def load_voice_inbox() -> None:
    global voice_inbox
    try:
        raw = json.loads(INBOX_PATH.read_text()) if INBOX_PATH.exists() else []
        voice_inbox = raw[-100:] if isinstance(raw, list) else []
    except Exception:
        voice_inbox = []
        append_log("inbox_error", action="load")


def save_voice_inbox() -> None:
    try:
        INBOX_PATH.write_text(json.dumps(voice_inbox[-100:], indent=2) + "\n")
    except Exception:
        append_log("inbox_error", action="save")


def inbox_add(cwd: str | None, text: str, meta: dict, intent: str) -> None:
    entry = {
        "ts": round(time.time(), 3),
        "cwd": cwd,
        "project": project_name(cwd) or "project",
        "text": trim_to_words(text, 42),
        "intent": intent,
        "verification": meta.get("verification") or "unknown",
        "tests": meta.get("verification_tests") or [],
        "semantic": meta.get("semantic") or [],
    }
    with state_lock:
        voice_inbox.append(entry)
        del voice_inbox[:-100]
        save_voice_inbox()
    append_log("inbox_add", project=entry["project"], intent=intent)
    publish({"event": "inbox", "count": len(voice_inbox)})


def inbox_briefing(entries: list[dict]) -> str:
    if not entries:
        return "You're caught up. No agent updates are waiting."
    latest_by_project = {}
    for entry in entries:
        latest_by_project[entry.get("project") or "project"] = entry
    ordered = sorted(
        latest_by_project.values(),
        key=lambda item: item.get("intent") not in {"blocker", "needs_input", "warning"},
    )
    clauses = []
    for entry in ordered[:5]:
        text = str(entry.get("text") or "update ready").strip().rstrip(".")
        clauses.append(f"{entry.get('project') or 'project'}: {text}")
    opening = f"{len(entries)} update{'s' if len(entries) != 1 else ''} while you were away. "
    return trim_to_words(opening + ". ".join(clauses) + ".", 72)


def drain_voice_inbox() -> tuple[str, int]:
    with state_lock:
        entries = list(voice_inbox)
        voice_inbox.clear()
        save_voice_inbox()
    text = inbox_briefing(entries)
    append_log(
        "return_briefing",
        count=len(entries),
        projects=sorted({e.get("project") for e in entries if e.get("project")}),
    )
    publish({"event": "inbox", "count": 0})
    return text, len(entries)


def should_hold_for_inbox(kind: str, prepared: bool = False) -> bool:
    return bool(config.get("voice_inbox_enabled") and kind == "reply" and not prepared)

config = {key: spec["default"] for key, spec in CONFIG_SPEC.items()}
duck_state = {
    "spotify": {
        "did_duck": False,
        "saved_volume": None,
        "ducked_at": None,
        "restoring": False,
    },
    "browser_youtube": {"active": False, "generation": 0},
}
duck_lock = threading.Lock()


def active_engine_name() -> str:
    return config.get("engine", "pocket")


def engine_by_name(name: str | None = None) -> TTSEngine:
    return ENGINES[name or active_engine_name()]


def active_engine() -> TTSEngine:
    return engine_by_name()


def rss_mb() -> float:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024, 1)


def now() -> float:
    return time.monotonic()


def bump_activity() -> None:
    global last_activity
    last_activity = now()


def json_response(handler: BaseHTTPRequestHandler, status: int, body: dict) -> None:
    payload = json.dumps(body).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def read_json(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    if length <= 0:
        return {}
    return json.loads(handler.rfile.read(length).decode("utf-8"))


def publish(event: dict) -> None:
    with sse_lock:
        subs = list(sse_subscribers)
    for sub in subs:
        try:
            sub.put_nowait(event)
        except Exception:
            pass


def append_log(event: str, **fields) -> None:
    payload = {"ts": round(time.time(), 3), "event": event, **fields}
    publish(payload)
    try:
        if LOG_PATH.exists():
            lines = LOG_PATH.read_text().splitlines()
            if len(lines) > 500:
                LOG_PATH.write_text("\n".join(lines[-250:]) + "\n")
        with LOG_PATH.open("a") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
    except Exception:
        return


def log_error(where: str) -> None:
    append_log("error", where=where, trace=traceback.format_exc()[-1500:])


def validate_field(key: str, value):
    spec = CONFIG_SPEC[key]
    kind = spec["kind"]
    if kind == "int":
        value = int(value)
        if not spec["min"] <= value <= spec["max"]:
            raise ValueError(key)
        return value
    if kind == "float":
        value = float(value)
        if not spec["min"] <= value <= spec["max"]:
            raise ValueError(key)
        return value
    if kind == "str":
        value = str(value).strip()
        if not value:
            raise ValueError(key)
        return value
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(key)
        return value
    if kind == "quiet":
        if value is None:
            return None
        start = str(value.get("start", ""))
        end = str(value.get("end", ""))
        for stamp in (start, end):
            if not re.fullmatch(r"\d{2}:\d{2}", stamp):
                raise ValueError(key)
            hours, minutes = int(stamp[:2]), int(stamp[3:])
            if hours > 23 or minutes > 59:
                raise ValueError(key)
        return {"start": start, "end": end}
    if kind == "voices":
        merged = dict(spec["default"])
        catalog = set(all_voices())
        for slot, name in dict(value).items():
            if slot not in merged or name not in catalog:
                raise ValueError(key)
            merged[slot] = name
        return merged
    if kind == "engine":
        value = str(value).strip()
        if value not in ENGINES or not ENGINES[value].installed():
            raise ValueError(key)
        return value
    if kind == "condense_provider":
        value = str(value).strip().lower()
        if value not in {"ollama", "none"}:
            raise ValueError(key)
        return value
    raise ValueError(key)


def load_config() -> None:
    global config
    cfg = {key: spec["default"] for key, spec in CONFIG_SPEC.items()}
    raw = {}
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text())
            if not isinstance(raw, dict):
                raise ValueError("config.json")
        except Exception:
            append_log("config_error", field="_file")
            raw = {}
    for key, value in raw.items():
        if key not in CONFIG_SPEC:
            continue
        try:
            cfg[key] = validate_field(key, value)
        except Exception:
            append_log("config_error", field=key)
    config = cfg


def save_config() -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")
    except Exception:
        append_log("config_error", field="_write")


def apply_config(data: dict) -> tuple[dict, list]:
    global config
    errors = []
    new = dict(config)
    drop_model = False
    for key, value in dict(data).items():
        if key not in CONFIG_SPEC:
            errors.append(key)
            continue
        try:
            cleaned = validate_field(key, value)
        except Exception:
            errors.append(key)
            append_log("config_error", field=key)
            continue
        if key == "temperature" and cleaned != new.get(key):
            drop_model = True
        new[key] = cleaned
    with state_lock:
        config = new
        if drop_model:
            engine = ENGINES.get("pocket")
            if hasattr(engine, "reset"):
                engine.reset()
    save_config()
    return new, errors


def in_quiet_hours() -> bool:
    quiet = config.get("quiet_hours")
    if not quiet:
        return False
    local = time.localtime()
    cur = local.tm_hour * 60 + local.tm_min
    sh, sm = quiet["start"].split(":")
    eh, em = quiet["end"].split(":")
    start = int(sh) * 60 + int(sm)
    end = int(eh) * 60 + int(em)
    if start == end:
        return False
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


def is_table_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if s.count("|") < 2:
        return False
    return s.startswith("|") or s.endswith("|") or bool(re.search(r"\w\s*\|\s*\w", s))


def strip_markdown_tables(raw: str) -> tuple[str, dict]:
    kept = []
    table_lines = 0
    separator_lines = 0
    for line in raw.splitlines():
        if is_table_line(line):
            table_lines += 1
            if re.fullmatch(r"\s*\|?[\s:|-]+\|[\s:|-|]*", line.strip()):
                separator_lines += 1
            continue
        kept.append(line)

    data_rows = max(0, table_lines - separator_lines - 1) if table_lines else 0
    return "\n".join(kept), {
        "table_lines": table_lines,
        "table_rows": data_rows,
        "table_skipped": table_lines > 0,
    }


def strip_markdown(text: str) -> str:
    text = re.sub(r"```[\s\S]*?```", " ", text)
    clean_lines = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith(("+++", "---", "@@", "diff ", "Traceback ", 'File "')):
            continue
        s = re.sub(r"\bhttps?://\S+", " ", s)
        s = re.sub(r"(?:~|\.)?/[\w .-]+(?:/[\w .-]+)+(?:[:]\d+)?", " ", s)
        s = re.sub(r"(?:\./|\../)?[\w.-]+(?:/[\w.-]+)+(?:[:]\d+)?", " ", s)
        s = re.sub(
            r"\b[\w.-]+\.(?:py|js|ts|tsx|jsx|json|toml|md|html|css|sh)(?::\d+)?\b",
            " ",
            s,
        )
        s = re.sub(r"\s+", " ", s).strip()
        if not s:
            continue
        clean_lines.append(s)
    text = " ".join(clean_lines)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"[*_#>\[\]()]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def first_sentence(text: str) -> str:
    match = re.search(r"(.{30,}?[.!?])\s", text + " ")
    if match:
        return match.group(1).strip()
    return text[: config["max_direct_chars"]].strip()


def condense_via_ollama(text: str, instruction: str | None = None) -> str | None:
    system_prompt = config["condense_prompt"]
    if instruction:
        system_prompt = f"{system_prompt} {instruction}"
    body = {
        "model": config.get("condense_ollama_model") or "qwen3.5:4b",
        "stream": True,
        "think": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
    }
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:11434/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        chunks = []
        with urllib.request.urlopen(
            req, timeout=config["condense_timeout_s"]
        ) as resp:
            for line in resp:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                chunks.append((data.get("message") or {}).get("content", ""))
                if data.get("done"):
                    break
        return strip_markdown("".join(chunks)) or None
    except Exception as exc:
        append_log(
            "condense_error",
            provider="ollama",
            detail=str(exc)[:300],
            error=type(exc).__name__,
        )
        return None


def condense(text: str, instruction: str | None = None) -> str | None:
    provider = config.get("condense_provider", "ollama")
    if provider == "none":
        return None
    if provider == "ollama":
        return condense_via_ollama(text, instruction)
    append_log("condense_error", provider=provider, detail="unsupported provider")
    return None


def prepare_speech(
    raw: str,
    intent: str = "update",
    diff_info: dict | None = None,
    kind: str = "reply",
    request_summary: str = "",
    verification: dict | None = None,
    temporal: str = "",
) -> tuple[str, dict]:
    tableless, meta = strip_markdown_tables(raw or "")
    text = strip_markdown(tableless)
    meta["_cleaned"] = text
    diff_info = diff_info or {}
    diff_summary = str(diff_info.get("summary") or "")
    budget = adaptive_word_budget(intent, kind, int(diff_info.get("count") or 0))
    meta["intent"] = intent
    meta["word_budget"] = budget
    meta["diff_files"] = int(diff_info.get("count") or 0)
    meta["git_verified"] = diff_info.get("verified")
    meta["git_reason"] = diff_info.get("reason")
    meta["git_branch_changed"] = bool(diff_info.get("branch_changed"))
    meta["git_oversized"] = bool(diff_info.get("oversized"))
    verification = verification or {}
    meta["request_intent"] = request_summary
    meta["verification"] = verification.get("verification", "unknown")
    meta["verification_tests"] = verification.get("tests", [])
    meta["semantic"] = diff_info.get("semantic") or []
    meta["temporal"] = temporal
    instruction = (
        f"Use at most {budget} words. Lead with the blocker or required action. "
        "Do not mention implementation chatter."
    )
    source = raw
    context = []
    if request_summary:
        context.append(f"Request intent: {request_summary}.")
    if verification.get("verification") != "unknown":
        tests = ", ".join(verification.get("tests") or ["checks"])
        context.append(f"Actual tool evidence: {tests} {verification['verification']}.")
    if temporal:
        context.append(f"Session arc: {temporal}")
    if diff_summary:
        context.append(f"Verified Git evidence: {diff_summary}")
        instruction += " Treat the verified Git evidence as factual and mention its material change."
    if context:
        source = "\n".join(context) + f"\n\nAgent reply:\n{raw}"

    def concise_fallback(value: str) -> str:
        generic = len(value.split()) <= 6 and re.search(
            r"\b(done|fixed|finished|completed|implemented|ready)\b", value, re.I
        )
        if diff_summary and generic:
            return trim_to_words(diff_summary, budget)
        return trim_to_words(value, budget)

    def finish(value: str) -> tuple[str, dict]:
        spoken, contract_meta = enforce_spoken_contract(
            value,
            source,
            intent,
            budget,
            diff_summary,
        )
        meta.update(contract_meta)
        return spoken, meta

    if meta["table_skipped"]:
        condensed = condense(source, instruction)
        if condensed is not None:
            meta["condense"] = "ok"
            meta["condense_source"] = "raw_table"
            return finish(strip_markdown(condensed))
        meta["condense"] = "failed"
        meta["condense_source"] = "raw_table"
        if text:
            return finish(concise_fallback(first_sentence(text)))
        meta["table_fallback"] = True
        return finish(TABLE_FALLBACK_TEXT)
    if not text:
        return "", meta
    adaptive_overflow = config.get("adaptive_brevity_enabled") and len(text.split()) > budget
    diff_wants_summary = bool(diff_summary) and len(text.split()) > 6
    temporal_wants_summary = bool(temporal)
    if len(text) > config["max_direct_chars"] or adaptive_overflow or diff_wants_summary or temporal_wants_summary:
        condensed = condense(source if context else text, instruction)
        if condensed is None:
            meta["condense"] = "failed"
            meta["condense_source"] = "cleaned"
            return finish(concise_fallback(first_sentence(text)))
        meta["condense"] = "ok"
        meta["condense_source"] = "cleaned"
        return finish(strip_markdown(condensed))
    meta["condense"] = "not_needed"
    return finish(concise_fallback(text))


def transcript_last_assistant(path_str: str) -> str:
    try:
        path = Path(path_str)
        if not path.exists():
            return ""
        tail = path.read_bytes()[-300_000:].decode("utf-8", "ignore")
        for line in reversed(tail.splitlines()):
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("type") != "assistant":
                continue
            message = obj.get("message") or {}
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content
            texts = [
                block.get("text", "")
                for block in content or []
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            joined = "\n".join(t for t in texts if t)
            if joined.strip():
                return joined
        return ""
    except Exception:
        return ""


def project_name(cwd: str | None) -> str | None:
    if not cwd:
        return None
    name = Path(cwd).name.strip()
    return name or None


def prefix_for_project(project: str | None) -> tuple[str, bool]:
    global prefix_counter
    if not project:
        return "", False

    cutoff = now() - config["project_window_s"]
    with state_lock:
        for name, seen_at in list(recent_projects.items()):
            if seen_at < cutoff:
                del recent_projects[name]
        recent_projects[project] = now()
        should_prefix = len(recent_projects) > 1
        if not should_prefix:
            return "", False
        template = PREFIX_TEMPLATES[prefix_counter % len(PREFIX_TEMPLATES)]
        prefix_counter += 1
    return template.format(project=project), True


def voice_for_job(job: dict) -> str:
    voices_for_engine = engine_by_name(job.get("engine")).voices()
    if job.get("voice"):
        voice = job["voice"]
        return voice if voice in voices_for_engine else voices_for_engine[0]
    voices = config["voices"]
    if job["kind"] in {"notification", "bulletin"}:
        voice = voices["notification"]
        return voice if voice in voices_for_engine else voices_for_engine[0]
    agent = job.get("agent") or "claude"
    voice = voices.get(agent, voices["claude"])
    return voice if voice in voices_for_engine else voices_for_engine[0]


def generation_for(key) -> int:
    return generations.get(key, 0)


def is_stale(key, gen: int) -> bool:
    return generation_for(key) != gen


def take_active_player():
    global active_player, active_cwd, active_cwds
    player = active_player
    active_player = None
    active_cwd = None
    active_cwds = set()
    return player


def downsample_peaks(peaks: list, target: int = PEAK_TARGET) -> list:
    if len(peaks) <= target:
        return [round(p, 3) for p in peaks]
    step = len(peaks) / target
    out = []
    for i in range(target):
        lo = int(i * step)
        hi = max(lo + 1, int((i + 1) * step))
        out.append(round(max(peaks[lo:hi]), 3))
    return out


def player_proc(path: Path):
    """Play a finished wav. Prefer ffplay for clean tempo (no pitch shift),
    fall back to afplay. atempo keeps pitch; afplay -r shifts it slightly."""
    rate = float(config["rate"])
    vol = float(config["volume"])
    if FFPLAY and config.get("stream_playback", True):
        af = f"atempo={min(2.0, max(0.5, rate))},volume={vol}"
        return subprocess.Popen(
            [
                FFPLAY,
                "-nodisp",
                "-autoexit",
                "-loglevel",
                "quiet",
                "-af",
                af,
                str(path),
            ],
            start_new_session=True,
        )
    args = ["afplay"]
    if abs(rate - 1.0) > 1e-6:
        args += ["-r", str(rate)]
    if vol < 0.999:
        args += ["-v", str(vol)]
    args.append(str(path))
    return subprocess.Popen(args, start_new_session=True)


def spotify_script(script: str) -> str | None:
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if out.returncode != 0:
            append_log("duck_error", detail=(out.stderr or out.stdout)[-300:])
            return None
        return out.stdout.strip()
    except Exception as exc:
        append_log("duck_error", detail=str(exc)[-300:])
        return None


def browser_duck_set(active: bool) -> int:
    with duck_lock:
        state = duck_state["browser_youtube"]
        state["active"] = bool(active)
        if active:
            state["generation"] += 1
        return int(state["generation"])


def browser_duck_snapshot() -> dict:
    with duck_lock:
        state = duck_state["browser_youtube"]
        return {
            "active": bool(state["active"]),
            "generation": int(state["generation"]),
            "target": int(config["browser_youtube_duck_target_volume"]) / 100,
        }


def spotify_running() -> bool:
    return spotify_script('application "Spotify" is running') == "true"


def spotify_volume() -> int | None:
    out = spotify_script('tell application "Spotify" to sound volume')
    try:
        return max(0, min(100, int(out))) if out is not None else None
    except ValueError:
        append_log("duck_error", detail=f"bad spotify volume: {out}")
        return None


def set_spotify_volume(volume: int) -> None:
    volume = max(0, min(100, int(volume)))
    spotify_script(f'tell application "Spotify" to set sound volume to {volume}')


def fade_spotify_volume(start: int, end: int, fade_ms: int) -> None:
    steps = max(1, min(8, int(fade_ms / 80) if fade_ms else 1))
    delay = max(0, fade_ms / steps / 1000) if fade_ms else 0
    for i in range(1, steps + 1):
        volume = round(start + (end - start) * (i / steps))
        set_spotify_volume(volume)
        if delay and i < steps:
            time.sleep(delay)


def spotify_duck_on() -> None:
    with duck_lock:
        duck_state["spotify"]["did_duck"] = False
        duck_state["spotify"]["saved_volume"] = None
        if not spotify_running():
            return
        current = spotify_volume()
        if current is None:
            return
        target = min(current, int(config["duck_target_volume"]))
        if target == current:
            return
        duck_state["spotify"]["saved_volume"] = current
        duck_state["spotify"]["did_duck"] = True
        duck_state["spotify"]["ducked_at"] = time.monotonic()
        duck_state["spotify"]["restoring"] = False
        try:
            DUCK_LEASE_PATH.write_text(
                json.dumps({"app": "Spotify", "saved_volume": current, "ts": time.time()}) + "\n"
            )
        except OSError:
            append_log("duck_error", detail="could not persist media restoration lease")
    fade_spotify_volume(current, target, int(config["duck_fade_ms"]))
    append_log("duck_on", app="Spotify", from_volume=current, to_volume=target)


def spotify_duck_off() -> None:
    with duck_lock:
        did_duck = bool(duck_state["spotify"]["did_duck"])
        saved = duck_state["spotify"]["saved_volume"]
        if duck_state["spotify"].get("restoring"):
            return
        if did_duck and saved is not None:
            duck_state["spotify"]["restoring"] = True
    if not did_duck or saved is None:
        return
    delay = int(config["duck_restore_delay_ms"]) / 1000
    if delay:
        time.sleep(delay)
    if not spotify_running():
        with duck_lock:
            duck_state["spotify"]["restoring"] = False
        return
    current = spotify_volume()
    if current is None:
        with duck_lock:
            duck_state["spotify"]["restoring"] = False
        return
    fade_spotify_volume(current, int(saved), int(config["duck_fade_ms"]))
    with duck_lock:
        duck_state["spotify"]["did_duck"] = False
        duck_state["spotify"]["saved_volume"] = None
        duck_state["spotify"]["ducked_at"] = None
        duck_state["spotify"]["restoring"] = False
    DUCK_LEASE_PATH.unlink(missing_ok=True)
    append_log("duck_off", app="Spotify", from_volume=current, to_volume=int(saved))


def recover_media_state() -> bool:
    if not DUCK_LEASE_PATH.exists():
        return False
    try:
        lease = json.loads(DUCK_LEASE_PATH.read_text())
        saved = int(lease["saved_volume"])
    except Exception:
        append_log("duck_error", detail="invalid media restoration lease")
        DUCK_LEASE_PATH.unlink(missing_ok=True)
        return False
    with duck_lock:
        duck_state["spotify"].update(
            {
                "did_duck": True,
                "saved_volume": saved,
                "ducked_at": time.monotonic() - 6,
                "restoring": False,
            }
        )
    if not spotify_running():
        return False
    current = spotify_volume()
    if current is None:
        return False
    fade_spotify_volume(current, saved, int(config["duck_fade_ms"]))
    with duck_lock:
        duck_state["spotify"].update(
            {
                "did_duck": False,
                "saved_volume": None,
                "ducked_at": None,
                "restoring": False,
            }
        )
    DUCK_LEASE_PATH.unlink(missing_ok=True)
    append_log("duck_recovered", app="Spotify", from_volume=current, to_volume=saved)
    return True


def media_restore_watchdog() -> None:
    while True:
        time.sleep(1)
        with duck_lock:
            did_duck = bool(duck_state["spotify"]["did_duck"])
            ducked_at = duck_state["spotify"].get("ducked_at")
        with state_lock:
            playing = bool(active_player and active_player.poll() is None)
        if did_duck and not playing and ducked_at and time.monotonic() - ducked_at > 5:
            append_log("duck_watchdog", action="restore")
            spotify_duck_off()


def browser_duck_on() -> None:
    if not config.get("browser_youtube_ducking_enabled"):
        return
    generation = browser_duck_set(True)
    append_log(
        "duck_on",
        app="Browser/YouTube",
        generation=generation,
        to_volume=int(config["browser_youtube_duck_target_volume"]),
    )


def browser_duck_off() -> None:
    generation = browser_duck_set(False)
    append_log("duck_off", app="Browser/YouTube", generation=generation)


def duck_on() -> None:
    if config.get("ducking_enabled"):
        spotify_duck_on()
    browser_duck_on()


def duck_off() -> None:
    spotify_duck_off()
    browser_duck_off()


def ring_put(clip_id: str, path: Path) -> None:
    clip_ring[clip_id] = path
    while len(clip_ring) > RING_SIZE:
        _, old = clip_ring.popitem(last=False)
        try:
            old.unlink()
        except OSError:
            pass


def write_wav(path: Path, pcm_frames: list, sample_rate: int | None = None) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate or active_engine().sample_rate)
        for pcm in pcm_frames:
            wav.writeframes(pcm)


def pcm_peak(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm)
    if samples.itemsize != 2:
        return 0.0
    return max(abs(s) for s in samples) / 32767


def media_should_duck(pcm_frames: list[bytes]) -> bool:
    return float(config.get("volume", 1.0)) > 0 and any(pcm_peak(frame) > 0 for frame in pcm_frames)


def speak(
    text: str,
    gen: int,
    key,
    cwd: str | None,
    voice: str,
    kind: str,
    engine_name: str | None = None,
    cue_pcm: bytes = b"",
    source_cwds: list[str] | None = None,
) -> str | None:
    """Synthesize fully, then play the finished clip the moment it is ready.
    Ships a waveform + duration for the Stage and keeps the clip for replay.
    Returns a clip id, or None if the job went stale or synthesis failed."""
    global active_player, active_cwd, active_cwds, last_spoken_ts, last_spoken_text, last_spoken_clip, now_speaking
    engine = engine_by_name(engine_name)

    pcm_frames = []
    peaks = []
    total_samples = 0
    if cue_pcm:
        pcm_frames.append(cue_pcm)
        peaks.append(pcm_peak(cue_pcm))
        total_samples += len(cue_pcm) // 2
    if text:
        try:
            for chunk in engine.synth(text, voice):
                with state_lock:
                    if is_stale(key, gen):
                        return None
                peaks.append(pcm_peak(chunk))
                total_samples += len(chunk) // 2
                pcm_frames.append(chunk)
        except Exception:
            log_error("synthesize")
            return None

    clip_id = str(int(time.time() * 1000))
    clip_path = RING_DIR / f"{clip_id}.wav"
    try:
        RING_DIR.mkdir(exist_ok=True)
        write_wav(clip_path, pcm_frames, engine.sample_rate)
        ring_put(clip_id, clip_path)
        last_spoken_clip = clip_id
        last_spoken_text = text
    except Exception:
        log_error("ring_write")

    duration = (
        total_samples / engine.sample_rate / min(2.0, max(0.5, float(config["rate"])))
    )

    with state_lock:
        if is_stale(key, gen):
            return None
    media_ducked = media_should_duck(pcm_frames)
    if media_ducked:
        duck_on()
    else:
        append_log("duck_skip", reason="inaudible")
    stale_after_duck = False
    try:
        with state_lock:
            if is_stale(key, gen):
                stale_after_duck = True
            else:
                proc = player_proc(clip_path)
                active_player = proc
                active_cwd = cwd
                active_cwds = set(source_cwds or ([cwd] if cwd else []))
                last_spoken_ts = time.time()
                now_speaking = {
                    "text": text[:240],
                    "voice": voice,
                    "kind": kind,
                    "engine": engine.name,
                    "project": project_name(cwd),
                    "ts": last_spoken_ts,
                }
    except Exception:
        log_error("player")
        _finish(None, media_ducked)
        return None
    if stale_after_duck:
        _finish(None, media_ducked)
        return None
    publish({"event": "now", "on": True, **now_speaking})
    publish(
        {
            "event": "wave",
            "peaks": downsample_peaks(peaks),
            "duration": round(duration, 2),
            "clip": clip_id,
        }
    )
    _finish(proc, media_ducked)
    return clip_id


def _finish(proc, restore_media: bool = True) -> None:
    global active_player, active_cwd, active_cwds, now_speaking
    try:
        if proc and proc.stdin:
            try:
                proc.stdin.close()
            except OSError:
                pass
        if proc:
            proc.wait()
    finally:
        if restore_media:
            duck_off()
        with state_lock:
            if active_player is proc:
                active_player = None
                active_cwd = None
                active_cwds = set()
            now_speaking = None
        publish({"event": "now", "on": False})


def replay(clip_id: str) -> bool:
    path = clip_ring.get(clip_id)
    if not path or not path.exists():
        return False
    player_proc(path)
    return True


def backchannel(command: str) -> tuple[bool, str]:
    command = str(command or "").strip().lower()
    if command in {"chill", "stop", "shut up"}:
        stop_speech()
        return True, "chilled"
    if command == "repeat":
        if last_spoken_clip and replay(last_spoken_clip):
            return True, "repeating"
        return False, "nothing to repeat"
    if command in {"slower", "faster"}:
        delta = -0.15 if command == "slower" else 0.15
        applied, errors = apply_config({"rate": float(config["rate"]) + delta})
        if errors:
            return False, "rate limit reached"
        if last_spoken_clip:
            replay(last_spoken_clip)
        return True, f"rate {applied['rate']:.2f}x"
    if command == "brief":
        applied, errors = apply_config({"adaptive_brevity_enabled": True})
        return (not errors), "brief mode on"
    if command in {"normal", "full"}:
        applied, errors = apply_config({"adaptive_brevity_enabled": False})
        return (not errors), "full mode on"
    if command == "recap":
        text = recap_text()
        enqueue_speak(text, None, kind="audition", prepared=True)
        return True, text
    return False, "unknown command"


def clone_voice(name: str, audio_b64: str) -> tuple[bool, str]:
    if not re.fullmatch(r"[a-z0-9_]{2,24}", name or ""):
        return False, "name must be 2-24 chars of a-z, 0-9, underscore"
    if name in CATALOG_VOICES:
        return False, "name collides with a catalog voice"
    try:
        raw = base64.b64decode(audio_b64, validate=True)
    except (binascii.Error, ValueError):
        return False, "audio was not valid base64"
    if len(raw) < 8000:
        return False, "recording too short"
    VOICES_DIR.mkdir(exist_ok=True)
    src = VOICES_DIR / f"{name}.src"
    dst = VOICES_DIR / f"{name}.wav"
    src.write_bytes(raw)
    try:
        if not FFMPEG:
            return False, "ffmpeg not found, cannot convert the recording"
        conv = subprocess.run(
            [
                FFMPEG,
                "-y",
                "-i",
                str(src),
                "-ac",
                "1",
                "-ar",
                str(SAMPLE_RATE),
                str(dst),
            ],
            capture_output=True,
            timeout=30,
        )
        if conv.returncode != 0 or not dst.exists():
            return False, "could not decode that audio file"
        try:
            ENGINES["pocket"].load().get_state_for_audio_prompt(str(dst))
        except Exception as exc:
            dst.unlink(missing_ok=True)
            msg = str(exc)
            if "voice cloning" in msg or "download the weights" in msg:
                return (
                    False,
                    "cloning locked: accept terms at huggingface.co/kyutai/pocket-tts and run hf auth login",
                )
            return False, "could not build a voice from that audio"
        ENGINES["pocket"].drop_voice(name)
        return True, name
    finally:
        src.unlink(missing_ok=True)


def delete_voice(name: str) -> bool:
    dst = VOICES_DIR / f"{name}.wav"
    if name in CATALOG_VOICES or not dst.exists():
        return False
    dst.unlink(missing_ok=True)
    ENGINES["pocket"].drop_voice(name)
    defaults = CONFIG_SPEC["voices"]["default"]
    for slot, voice in list(config["voices"].items()):
        if voice == name:
            config["voices"][slot] = defaults.get(slot, "alba")
    save_config()
    return True


def pick_job_locked() -> dict:
    for key, job in pending.items():
        if job["kind"] == "notification":
            return pending.pop(key)
    if config.get("radio_bulletins_enabled"):
        reply_items = [
            (key, job) for key, job in pending.items() if job["kind"] == "reply"
        ]
        projects = {job["cwd"] for _, job in reply_items if job.get("cwd")}
        if len(projects) >= 2:
            selected = reply_items[-4:]
            for key, _ in selected:
                pending.pop(key, None)
            jobs = [job for _, job in selected]
            gen = max(job["gen"] for job in jobs)
            key = (None, "bulletin")
            generations[key] = gen
            return {
                "gen": gen,
                "raw": "",
                "cwd": None,
                "kind": "bulletin",
                "agent": None,
                "voice": None,
                "engine": None,
                "prepared": False,
                "source_jobs": jobs,
                "source_cwds": [job["cwd"] for job in jobs if job.get("cwd")],
            }
    _, job = pending.popitem()
    return job


def bulletin_content(job: dict) -> tuple[str, dict, str, str | None]:
    sections = []
    fallback_parts = []
    intents = []
    signals = []
    redacted_count = 0
    pronunciation_count = 0
    projects = []
    diff_files = 0
    for source in job.get("source_jobs") or []:
        cwd = source.get("cwd")
        project = project_name(cwd) or "project"
        projects.append(project)
        raw = source.get("raw") or ""
        if config.get("privacy_sentinel_enabled"):
            raw, count = redact_sensitive(raw, cwd)
            redacted_count += count
        intent = classify_intent(raw, "reply")
        signal = detect_build_signal(raw)
        intents.append(intent)
        if signal:
            signals.append(signal)
        turn = take_turn(source)
        snapshot = turn.get("git")
        diff_info = {}
        if config.get("diff_narration_enabled") and snapshot:
            diff_info = git_change_summary(cwd, snapshot)
            diff_files += int(diff_info.get("count") or 0)
        elif turn and config.get("diff_narration_enabled"):
            diff_info = {"verified": False, "summary": "Git evidence unavailable for this turn.", "reason": turn.get("git_state", "unavailable")}
        verification = transcript_tool_evidence(
            source.get("transcript_path") or turn.get("transcript_path"),
            float(turn.get("started_at") or 0),
            source.get("turn_id") or turn.get("turn_id"),
        )
        if verification.get("verification") == "failed":
            signals.append("tests_failed")
        elif verification.get("verification") == "passed":
            signals.append("tests_passed")
        cleaned = strip_markdown(raw)
        evidence = str(diff_info.get("summary") or "")
        section = f"Project {project}."
        if evidence:
            section += f" Verified Git evidence: {evidence}"
        if turn.get("request_intent"):
            section += f" Request intent: {turn['request_intent']}."
        if verification.get("verification") != "unknown":
            tests = ", ".join(verification.get("tests") or ["checks"])
            section += f" Actual tool evidence: {tests} {verification['verification']}."
        section += f" Agent reply: {cleaned or raw}"
        sections.append(section)
        fallback = evidence or first_sentence(cleaned)
        fallback, count = apply_pronunciations(trim_to_words(fallback, 10), cwd)
        pronunciation_count += count
        fallback_parts.append(f"{project}: {fallback}")

    priority = ("blocker", "needs_input", "warning", "success", "update")
    intent = next((candidate for candidate in priority if candidate in intents), "update")
    signal_priority = ("tests_failed", "tests_passed", "deployed", "merged")
    build_signal = next((candidate for candidate in signal_priority if candidate in signals), None)
    budget = min(48, max(20, len(projects) * 12))
    instruction = (
        f"This is a radio bulletin for {len(projects)} coding projects. "
        f"Use at most {budget} words total. Give one short clause per project, name every project, "
        "and lead with any blocker or request for input."
    )
    source = "\n\n".join(sections)
    condensed = condense(source, instruction)
    fallback_text = "Bulletin. " + ". ".join(fallback_parts)
    candidate = trim_to_words(strip_markdown(condensed), budget) if condensed else fallback_text
    text, contract_meta = enforce_spoken_contract(candidate, source, intent, budget)
    if contract_meta.get("contract") == "fallback":
        text = trim_to_words(fallback_text, budget)
    for source in job.get("source_jobs") or []:
        text, count = apply_pronunciations(text, source.get("cwd"))
        pronunciation_count += count
    meta = {
        "intent": intent,
        "word_budget": budget,
        "radio_projects": projects,
        "radio_count": len(projects),
        "redacted": redacted_count,
        "pronunciations": pronunciation_count,
        "diff_files": diff_files,
        "build_signal": build_signal,
        "condense": "ok" if condensed else "failed",
        "condense_source": "radio_bulletin",
        **contract_meta,
    }
    return text, meta, intent, build_signal


def process_job(job: dict) -> None:
    global last_stages
    gen = job["gen"]
    raw = job["raw"]
    cwd = job["cwd"]
    kind = job["kind"]
    key = job_key(cwd, kind, job.get("session_id"), job.get("turn_id"))
    if kind == "bulletin":
        text, meta, intent, build_signal = bulletin_content(job)
        cleaned = ""
    else:
        redacted_count = 0
        if config.get("privacy_sentinel_enabled"):
            raw, redacted_count = redact_sensitive(raw, cwd)
        intent = classify_intent(raw, kind)
        build_signal = detect_build_signal(raw)
        turn = take_turn(job) if kind == "reply" else {}
        snapshot = turn.get("git")
        diff_info = {}
        if kind == "reply" and config.get("diff_narration_enabled") and snapshot:
            diff_info = git_change_summary(cwd, snapshot)
        elif kind == "reply" and config.get("diff_narration_enabled") and turn:
            diff_info = {"verified": False, "summary": "Git evidence unavailable for this turn.", "reason": turn.get("git_state", "unavailable")}
        if job.get("prepared"):
            text, meta = raw, {}
        else:
            evidence = transcript_tool_evidence(
                job.get("transcript_path") or turn.get("transcript_path"),
                float(turn.get("started_at") or 0),
                job.get("turn_id") or turn.get("turn_id"),
            )
            temporal = temporal_context(cwd, evidence.get("verification", "unknown"), intent)
            text, meta = prepare_speech(
                raw,
                intent,
                diff_info,
                kind,
                turn.get("request_intent", ""),
                evidence,
                temporal,
            )
        meta["redacted"] = redacted_count
        meta["build_signal"] = build_signal
        cleaned = meta.pop("_cleaned", "")
    if kind == "reply" and not job.get("prepared"):
        with state_lock:
            last_stages = {
                "raw": raw,
                "cleaned": cleaned,
                "spoken": text,
                "condense": meta.get("condense"),
                "table_skipped": meta.get("table_skipped"),
            }
    earcon_only = meta.get("contract") == "earcon_only"
    if not text and not earcon_only:
        append_log("skip_empty", cwd=cwd, **meta)
        return
    hold_for_inbox = should_hold_for_inbox(kind, bool(job.get("prepared")))
    if in_quiet_hours() and not hold_for_inbox:
        append_log("quiet_skip", cwd=cwd, kind=kind)
        return
    prefix, prefixed = "", False
    if kind == "reply" and text:
        prefix, prefixed = prefix_for_project(project_name(cwd))
        if prefixed:
            text = prefix + text
    if kind != "bulletin" and text:
        text, pronunciation_count = apply_pronunciations(text, cwd)
        meta["pronunciations"] = pronunciation_count
    if kind == "reply" and not job.get("prepared"):
        with state_lock:
            last_stages.update(
                {
                    "spoken": text,
                    "intent": meta.get("intent"),
                    "contract": meta.get("contract"),
                    "contract_reasons": meta.get("contract_reasons") or [],
                    "diff_files": meta.get("diff_files", 0),
                    "git_verified": meta.get("git_verified"),
                    "git_reason": meta.get("git_reason"),
                    "git_branch_changed": meta.get("git_branch_changed", False),
                    "git_oversized": meta.get("git_oversized", False),
                    "redacted": meta.get("redacted", 0),
                    "pronunciations": meta.get("pronunciations", 0),
                    "build_signal": meta.get("build_signal"),
                    "word_budget": meta.get("word_budget"),
                    "request_intent": meta.get("request_intent"),
                    "verification": meta.get("verification"),
                    "verification_tests": meta.get("verification_tests") or [],
                    "semantic": meta.get("semantic") or [],
                    "temporal": meta.get("temporal") or "",
                }
            )
    with state_lock:
        stale = is_stale(key, gen)
        current_generation = generation_for(key)
    if stale:
        append_log(
            "speak_stale",
            cwd=cwd,
            generation=gen,
            current_generation=current_generation,
        )
        return
    if kind == "reply" and not job.get("prepared"):
        remember_timeline(cwd, meta, intent, build_signal)
        if hold_for_inbox:
            inbox_add(cwd, text, meta, intent)
            append_log("inbox_hold", cwd=cwd, project=project_name(cwd), intent=intent)
            return
    voice = voice_for_job(job)
    engine = engine_by_name(job.get("engine"))
    cue, cue_meta = earcon_pcm(
        project_name(cwd),
        intent,
        build_signal,
        engine.sample_rate,
        repo_enabled=bool(config.get("repo_earcons_enabled")),
        intent_enabled=bool(config.get("intent_earcons_enabled")),
        build_enabled=bool(config.get("build_sonification_enabled")),
    )
    meta.update(cue_meta)
    if not text and not cue:
        append_log("skip_empty", cwd=cwd, **meta)
        return
    clip_id = speak(
        text,
        gen,
        key,
        cwd,
        voice,
        kind,
        job.get("engine"),
        cue,
        job.get("source_cwds"),
    )
    if clip_id is not None:
        if kind == "bulletin":
            append_log("radio_bulletin", projects=meta.get("radio_projects", []))
        append_log(
            "speak",
            cwd=cwd,
            project=project_name(cwd),
            kind=kind,
            engine=job.get("engine") or active_engine_name(),
            voice=voice,
            prefixed=prefixed,
            prefix=prefix.strip(),
            spoken_chars=len(text),
            clip=clip_id,
            **meta,
        )


def worker() -> None:
    global worker_running, working_cwd, working_cwds
    while True:
        with state_lock:
            if not pending:
                worker_running = False
                return
            should_batch_wait = (
                config.get("radio_bulletins_enabled")
                and not any(job["kind"] == "notification" for job in pending.values())
                and any(job["kind"] == "reply" for job in pending.values())
            )
        if should_batch_wait:
            time.sleep(int(config.get("radio_batch_window_ms", 450)) / 1000)
        with state_lock:
            if not pending:
                worker_running = False
                return
            job = pick_job_locked()
            working_cwd = job["cwd"]
            working_cwds = set(job.get("source_cwds") or ([job["cwd"]] if job["cwd"] else []))
        try:
            process_job(job)
        except Exception:
            log_error("worker")
        finally:
            with state_lock:
                if working_cwd == job["cwd"]:
                    working_cwd = None
                    working_cwds = set()


def enqueue_speak(
    raw: str,
    cwd: str | None = None,
    kind: str = "reply",
    agent: str | None = None,
    voice: str | None = None,
    engine: str | None = None,
    prepared: bool = False,
    session_id: str | None = None,
    turn_id: str | None = None,
    transcript_path: str | None = None,
) -> int:
    global worker_running, generation
    with state_lock:
        generation += 1
        gen = generation
        key = job_key(cwd, kind, session_id, turn_id)
        generations[key] = gen
        pending[key] = {
            "gen": gen,
            "raw": raw,
            "cwd": cwd,
            "kind": kind,
            "agent": agent,
            "voice": voice,
            "engine": engine,
            "prepared": prepared,
            "session_id": session_id,
            "turn_id": turn_id,
            "transcript_path": transcript_path,
        }
        if not worker_running:
            worker_running = True
            threading.Thread(target=worker, daemon=True).start()
        return gen


def stop_speech(cwd: str | None = None) -> int:
    global generation, working_cwd, working_cwds
    with state_lock:
        pending_cwds = sorted({key[0] or "" for key in pending})
        matches_pending = any(key[0] == cwd for key in pending)
        should_stop = (
            cwd is None
            or matches_pending
            or working_cwd == cwd
            or cwd in working_cwds
            or active_cwd == cwd
            or cwd in active_cwds
        )
        if not should_stop:
            append_log(
                "stop_ignored",
                cwd=cwd,
                active_cwd=active_cwd,
                pending_cwds=pending_cwds,
                working_cwd=working_cwd,
            )
            return generation
        generation += 1
        gen = generation
        if cwd is None:
            pending.clear()
            for key in list(generations):
                generations[key] = gen
        else:
            for key in [key for key in pending if key[0] == cwd]:
                pending.pop(key)
            for key in [key for key in generations if key[0] == cwd]:
                generations[key] = gen
        was_working_source = cwd is not None and cwd in working_cwds
        if cwd is None or working_cwd == cwd or was_working_source:
            working_cwd = None
            working_cwds = set()
        if was_working_source:
            generations[(None, "bulletin")] = gen
        player = (
            take_active_player()
            if cwd is None or active_cwd == cwd or cwd in active_cwds
            else None
        )
        append_log("stop", cwd=cwd, generation=gen)
    if player and player.poll() is None:
        try:
            os.killpg(player.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    return gen


def idle_watch() -> None:
    while True:
        time.sleep(5)
        with state_lock:
            idle = now() - last_activity
            busy = worker_running or (active_player and active_player.poll() is None)
        if idle > config["idle_exit_s"] and not busy:
            os._exit(0)


def log_tail(limit: int) -> list:
    entries = []
    try:
        if LOG_PATH.exists():
            for line in LOG_PATH.read_text().splitlines()[-limit:]:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return entries


def muted() -> bool:
    return KILL_PATH.exists()


def set_muted(value: bool) -> None:
    if value:
        KILL_PATH.touch()
    else:
        KILL_PATH.unlink(missing_ok=True)


def engine_status(engine: TTSEngine) -> dict:
    return {
        "name": engine.name,
        "installed": engine.installed(),
        "loaded": engine.loaded(),
        "rss_mb": rss_mb(),
        "install_command": None if engine.installed() else engine.install_command,
    }


def requested_engine(data: dict) -> tuple[str | None, str | None]:
    name = data.get("engine") or data.get("name")
    if name is None:
        return None, None
    name = str(name)
    engine = ENGINES.get(name)
    if not engine:
        return None, "unknown_engine"
    if not engine.installed():
        return None, engine.install_command
    return name, None


def synth_clip(engine_name: str, text: str, voice: str | None = None) -> dict:
    engine = ENGINES[engine_name]
    voices = engine.voices()
    if not voice or voice not in voices:
        voice = voices[0]

    pcm_frames = []
    total_samples = 0
    first_audio_s = None
    t0 = time.perf_counter()
    for chunk in engine.synth(text, voice):
        if first_audio_s is None:
            first_audio_s = time.perf_counter() - t0
        total_samples += len(chunk) // 2
        pcm_frames.append(chunk)
    synth_s = time.perf_counter() - t0
    duration_s = total_samples / engine.sample_rate if engine.sample_rate else 0
    clip_id = f"{engine_name}-{int(time.time() * 1000)}"
    clip_path = RING_DIR / f"{clip_id}.wav"
    RING_DIR.mkdir(exist_ok=True)
    write_wav(clip_path, pcm_frames, engine.sample_rate)
    ring_put(clip_id, clip_path)
    return {
        "engine": engine_name,
        "voice": voice,
        "clip_id": clip_id,
        "rtf": round(synth_s / duration_s, 3) if duration_s else None,
        "ttfa_s": round(first_audio_s if first_audio_s is not None else synth_s, 3),
        "synth_s": round(synth_s, 3),
        "duration_s": round(duration_s, 3),
        "rss_mb": rss_mb(),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        bump_activity()
        try:
            parsed = urllib.parse.urlparse(self.path)
            route = parsed.path
            if route == "/" or route == "/controller":
                try:
                    body = CONTROLLER_PATH.read_bytes()
                except OSError:
                    json_response(self, 404, {"error": "no_controller"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if route == "/health":
                git = git_runtime_state()
                provider = config.get("condense_provider", "none")
                json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "engine": active_engine_name(),
                        "model_loaded": active_engine().loaded(),
                        "uptime_s": round(now() - started_at, 1),
                        "last_spoken_ts": last_spoken_ts,
                        "last_spoken_clip": last_spoken_clip,
                        "now_speaking": now_speaking,
                        "git_rev": git.get("rev"),
                        "git_branch": git.get("branch"),
                        "git_dirty": git.get("dirty"),
                        "condense_provider": provider,
                        "condense_locality": "local",
                        "muted": muted(),
                    },
                )
                return
            if route == "/doctor":
                json_response(self, 200, doctor_snapshot())
                return
            if route == "/browser/duck":
                state = browser_duck_snapshot()
                state["enabled"] = bool(config.get("browser_youtube_ducking_enabled"))
                json_response(self, 200, {"ok": True, **state})
                return
            if route == "/events":
                sub = queue.Queue(maxsize=200)
                with sse_lock:
                    sse_subscribers.append(sub)
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    hello = {"event": "hello", "now": now_speaking}
                    self.wfile.write(
                        f"data: {json.dumps(hello, ensure_ascii=True)}\n\n".encode()
                    )
                    self.wfile.flush()
                    while True:
                        try:
                            ev = sub.get(timeout=15)
                            self.wfile.write(
                                f"data: {json.dumps(ev, ensure_ascii=True)}\n\n".encode()
                            )
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                except Exception:
                    pass
                finally:
                    with sse_lock:
                        if sub in sse_subscribers:
                            sse_subscribers.remove(sub)
                return
            if route == "/config":
                json_response(
                    self, 200, {"ok": True, "config": config, "catalog": all_voices()}
                )
                return
            if route == "/engines":
                json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "active": active_engine_name(),
                        "engines": [engine_status(e) for e in ENGINES.values()],
                    },
                )
                return
            if route == "/voices":
                json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "voices": all_voices(),
                        "custom": custom_voices(),
                        "assigned": {
                            slot: voice if voice in all_voices() else all_voices()[0]
                            for slot, voice in config["voices"].items()
                        },
                        "engine": active_engine_name(),
                        "clone_ready": bool(FFMPEG),
                    },
                )
                return
            if route == "/log/tail":
                query = urllib.parse.parse_qs(parsed.query)
                limit = min(int(query.get("limit", ["120"])[0]), 500)
                json_response(
                    self,
                    200,
                    {"ok": True, "entries": log_tail(limit), "stages": last_stages},
                )
                return
            if route == "/timeline":
                json_response(self, 200, {"ok": True, "events": timeline_events[-50:], "recap": recap_text()})
                return
            if route == "/inbox":
                json_response(
                    self,
                    200,
                    {"ok": True, "count": len(voice_inbox), "entries": voice_inbox[-50:]},
                )
                return
            json_response(self, 404, {"error": "not_found"})
        except Exception:
            log_error("do_GET")
            json_response(self, 500, {"ok": False})

    def do_POST(self):
        bump_activity()
        try:
            if self.path == "/stop":
                data = read_json(self)
                gen = stop_speech(data.get("cwd"))
                json_response(self, 202, {"ok": True, "generation": gen})
                return
            if self.path == "/backchannel":
                data = read_json(self)
                ok, result = backchannel(data.get("command", ""))
                json_response(self, 200 if ok else 400, {"ok": ok, "result": result})
                return
            if self.path == "/inbox/return":
                text, count = drain_voice_inbox()
                gen = enqueue_speak(text, None, kind="audition", prepared=True)
                json_response(
                    self,
                    202,
                    {"ok": True, "count": count, "text": text, "generation": gen},
                )
                return
            if self.path == "/inbox/clear":
                with state_lock:
                    voice_inbox.clear()
                    save_voice_inbox()
                publish({"event": "inbox", "count": 0})
                json_response(self, 200, {"ok": True, "count": 0})
                return
            if self.path == "/turn/start":
                data = read_json(self)
                cwd = data.get("cwd")
                gen = stop_speech(cwd)
                record = store_turn(data)
                json_response(
                    self,
                    202,
                    {"ok": True, "generation": gen, "git": record.get("git_state"), "intent": record.get("request_intent")},
                )
                return
            if self.path == "/restart":
                json_response(self, 200, {"ok": True, "restarting": True})
                cmd = f"sleep 0.3; exec {shlex.quote(sys.executable)} {shlex.quote(str(ROOT / 'daemon.py'))}"
                subprocess.Popen(
                    ["/bin/sh", "-c", cmd],
                    cwd=str(ROOT),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                append_log("restart_requested")
                os._exit(0)
            if self.path == "/mute":
                data = read_json(self)
                value = bool(data.get("muted"))
                set_muted(value)
                publish({"event": "mute", "muted": muted()})
                json_response(self, 200, {"ok": True, "muted": muted()})
                return
            if self.path == "/speak":
                data = read_json(self)
                engine_name, engine_error = requested_engine(data)
                if engine_error:
                    json_response(self, 400, {"ok": False, "error": engine_error})
                    return
                text = data.get("text") or data.get("last_assistant_message") or ""
                cwd = data.get("cwd")
                if not text.strip() and data.get("transcript_path"):
                    text = transcript_last_assistant(data["transcript_path"])
                    if text.strip():
                        append_log("transcript_fallback", cwd=cwd)
                if not text.strip():
                    json_response(self, 202, {"ok": True, "skipped": True})
                    return
                kind = (
                    "notification" if data.get("event") == "Notification" else "reply"
                )
                gen = enqueue_speak(
                    text,
                    cwd,
                    kind=kind,
                    agent=data.get("agent"),
                    engine=engine_name,
                    session_id=data.get("session_id"),
                    turn_id=data.get("turn_id"),
                    transcript_path=data.get("transcript_path"),
                )
                json_response(self, 202, {"ok": True, "generation": gen})
                return
            if self.path == "/engine":
                data = read_json(self)
                engine_name, engine_error = requested_engine(data)
                if not engine_name:
                    json_response(self, 400, {"ok": False, "error": engine_error})
                    return
                applied, errors = apply_config({"engine": engine_name})
                publish({"event": "config"})
                json_response(
                    self, 200, {"ok": not errors, "config": applied, "errors": errors}
                )
                return
            if self.path == "/bench":
                data = read_json(self)
                text = str(
                    data.get("text") or AUDITION_TEXT.format(voice="bench")
                ).strip()
                voice = data.get("voice")
                results = []
                for name, engine in ENGINES.items():
                    if not engine.installed():
                        continue
                    try:
                        results.append(synth_clip(name, text, voice))
                    except Exception:
                        log_error(f"bench_{name}")
                        results.append({"engine": name, "ok": False})
                json_response(self, 200, {"ok": True, "results": results})
                return
            if self.path == "/config":
                data = read_json(self)
                applied, errors = apply_config(data)
                publish({"event": "config"})
                json_response(
                    self, 200, {"ok": not errors, "config": applied, "errors": errors}
                )
                return
            if self.path == "/audition":
                data = read_json(self)
                engine_name, engine_error = requested_engine(data)
                if engine_error:
                    json_response(self, 400, {"ok": False, "error": engine_error})
                    return
                voice = data.get("voice")
                voices = engine_by_name(engine_name).voices()
                if voice not in voices:
                    json_response(self, 400, {"ok": False, "error": "unknown_voice"})
                    return
                text = data.get("text") or AUDITION_TEXT.format(
                    voice=voice.replace("_", " ")
                )
                gen = enqueue_speak(
                    text,
                    None,
                    kind="audition",
                    voice=voice,
                    engine=engine_name,
                    prepared=True,
                )
                json_response(self, 202, {"ok": True, "generation": gen})
                return
            if self.path == "/replay":
                data = read_json(self)
                ok = replay(str(data.get("clip", "")))
                json_response(self, 200 if ok else 404, {"ok": ok})
                return
            if self.path == "/clone":
                data = read_json(self)
                if data.get("delete"):
                    ok = delete_voice(data.get("name", ""))
                    if ok:
                        publish({"event": "config"})
                    json_response(self, 200 if ok else 404, {"ok": ok})
                    return
                ok, result = clone_voice(data.get("name", ""), data.get("audio", ""))
                if ok:
                    publish({"event": "config"})
                    json_response(self, 200, {"ok": True, "voice": result})
                else:
                    json_response(self, 400, {"ok": False, "error": result})
                return
            if self.path == "/recondense":
                if not last_stages or not last_stages.get("raw"):
                    json_response(self, 200, {"ok": False, "error": "no_last_reply"})
                    return
                tableless, meta = strip_markdown_tables(last_stages["raw"])
                cleaned = strip_markdown(tableless)
                source = last_stages["raw"] if meta["table_skipped"] else cleaned
                out = condense(source)
                if out is None:
                    if meta["table_skipped"] and not cleaned:
                        out = TABLE_FALLBACK_TEXT
                    else:
                        out = f"Summary failed, first line: {first_sentence(cleaned)}"
                out = strip_markdown(out)
                enqueue_speak(out, None, kind="audition", prepared=True)
                json_response(self, 200, {"ok": True, "text": out})
                return
            json_response(self, 404, {"error": "not_found"})
        except Exception:
            log_error("do_POST")
            json_response(self, 202, {"ok": False})


def main() -> int:
    load_config()
    load_turn_records()
    load_timeline()
    load_voice_inbox()
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        return 0
    recover_media_state()
    threading.Thread(target=idle_watch, daemon=True).start()
    threading.Thread(target=media_restore_watchdog, daemon=True).start()
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
