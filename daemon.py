#!/usr/bin/env python3
import base64
import binascii
import json
import os
import queue
import re
import shutil
import signal
import subprocess
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

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 7333
DEEPSEEK_SETTINGS = Path.home() / ".claude" / "settings.deepseek.json"
TABLE_FALLBACK_TEXT = (
    "I made a table for this, but I won't read the whole thing out loud. "
    "Take a look and tell me what you think."
)
PREFIX_TEMPLATES = ("In {project}: ", "From {project}: ", "Update from {project}: ")
LOG_PATH = ROOT / ".voice.log"
CONFIG_PATH = ROOT / "config.json"
CONTROLLER_PATH = ROOT / "controller.html"
RING_DIR = ROOT / ".clips"
VOICES_DIR = ROOT / "voices"
AUDITION_TEXT = "This is {voice}, speaking for your coding agents."
FFPLAY = shutil.which("ffplay")
FFMPEG = shutil.which("ffmpeg")
RING_SIZE = 5
PEAK_TARGET = 160
SAMPLE_RATE = 24000

DEFAULT_CONDENSE_PROMPT = (
    "You are speaking for a coding agent after it finishes a turn. "
    "The user hears this out loud, so sound like a calm coding partner, not a report. "
    "Say what changed, what matters, or what the user should notice next. "
    "Use 2-3 short natural sentences. "
    "Do not read tables, code, diffs, file paths, URLs, markdown, bullets, or headings. "
    "If the reply mostly contains a table, say that a table was made and invite the user to inspect it."
)

CATALOG_VOICES = (
    "alba", "anna", "azelma", "bill_boerst", "caro_davy", "charles", "cosette",
    "eponine", "estelle", "eve", "fantine", "george", "giovanni", "jane",
    "javert", "jean", "juergen", "lola", "marius", "mary", "michael", "paul",
    "peter_yearsley", "rafael", "stuart_bell", "vera",
)


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
        yield from model.generate_audio_stream(
            model_state=voice_state, text_to_generate=text
        )

    def reset(self) -> None:
        self.model = None
        self.voice_states = {}

    def drop_voice(self, name: str) -> None:
        self.voice_states.pop(name, None)


ENGINES = {"pocket": PocketEngine()}


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
    "deepseek_timeout_s": {"default": 12, "kind": "int", "min": 2, "max": 60},
    "rate": {"default": 1.0, "kind": "float", "min": 0.5, "max": 3.0},
    "volume": {"default": 1.0, "kind": "float", "min": 0.0, "max": 1.0},
    "temperature": {"default": 0.7, "kind": "float", "min": 0.1, "max": 1.5},
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
            capture_output=True, text=True, timeout=2,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


GIT_REV = git_rev()

state_lock = threading.Lock()
pending = {}
worker_running = False
generation = 0
generations = {}
active_player = None
active_cwd = None
working_cwd = None
last_activity = time.monotonic()
started_at = time.monotonic()
last_spoken_ts = None
last_stages = None
now_speaking = None
sse_subscribers = []
sse_lock = threading.Lock()
recent_projects = {}
prefix_counter = 0
clip_ring = OrderedDict()

config = {key: spec["default"] for key, spec in CONFIG_SPEC.items()}

def active_engine_name() -> str:
    return config.get("engine", "pocket")


def active_engine() -> TTSEngine:
    return ENGINES[active_engine_name()]


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
        if value not in ENGINES:
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


def read_deepseek_env() -> dict:
    data = json.loads(DEEPSEEK_SETTINGS.read_text())
    return data.get("env", {})


def condense(text: str) -> str | None:
    try:
        env = read_deepseek_env()
        token = env.get("ANTHROPIC_AUTH_TOKEN")
        if not token:
            return None
        body = {
            "model": "deepseek-v4-flash",
            "max_tokens": 1000,
            "messages": [
                {"role": "system", "content": config["condense_prompt"]},
                {"role": "user", "content": text},
            ],
        }
        req = urllib.request.Request(
            "https://api.deepseek.com/anthropic/v1/messages",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": token,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=config["deepseek_timeout_s"]) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data.get("content") or []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return strip_markdown(block.get("text", "")) or None
    except Exception:
        return None
    return None


def prepare_speech(raw: str) -> tuple[str, dict]:
    tableless, meta = strip_markdown_tables(raw or "")
    text = strip_markdown(tableless)
    if not text:
        if meta["table_skipped"]:
            meta["table_fallback"] = True
            return TABLE_FALLBACK_TEXT, meta
        return "", meta
    meta["_cleaned"] = text
    if len(text) > config["max_direct_chars"]:
        condensed = condense(text)
        if condensed is None:
            meta["condense"] = "failed"
            return f"Summary failed, first line: {first_sentence(text)}", meta
        meta["condense"] = "ok"
        return strip_markdown(condensed), meta
    meta["condense"] = "not_needed"
    return text, meta


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
    if job.get("voice"):
        return job["voice"]
    voices = config["voices"]
    if job["kind"] == "notification":
        return voices["notification"]
    agent = job.get("agent") or "claude"
    return voices.get(agent, voices["claude"])


def generation_for(key) -> int:
    return generations.get(key, 0)


def is_stale(key, gen: int) -> bool:
    return generation_for(key) != gen


def take_active_player():
    global active_player, active_cwd
    player = active_player
    active_player = None
    active_cwd = None
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
            [FFPLAY, "-nodisp", "-autoexit", "-loglevel", "quiet", "-af", af, str(path)],
            start_new_session=True,
        )
    args = ["afplay"]
    if abs(rate - 1.0) > 1e-6:
        args += ["-r", str(rate)]
    if vol < 0.999:
        args += ["-v", str(vol)]
    args.append(str(path))
    return subprocess.Popen(args, start_new_session=True)


def ring_put(clip_id: str, path: Path) -> None:
    clip_ring[clip_id] = path
    while len(clip_ring) > RING_SIZE:
        _, old = clip_ring.popitem(last=False)
        try:
            old.unlink()
        except OSError:
            pass


def write_wav(path: Path, pcm_frames: list) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(active_engine().sample_rate)
        for pcm in pcm_frames:
            wav.writeframes(pcm)


def speak(text: str, gen: int, key, cwd: str | None, voice: str, kind: str) -> str | None:
    """Synthesize fully, then play the finished clip the moment it is ready.
    Ships a waveform + duration for the Stage and keeps the clip for replay.
    Returns a clip id, or None if the job went stale or synthesis failed."""
    global active_player, active_cwd, last_spoken_ts, now_speaking
    engine = active_engine()

    pcm_frames = []
    peaks = []
    total_samples = 0
    try:
        for chunk in engine.synth(text, voice):
            with state_lock:
                if is_stale(key, gen):
                    return None
            data = chunk.detach().cpu().clamp(-1, 1).numpy()
            peaks.append(float(abs(data).max()))
            total_samples += len(data)
            pcm_frames.append((data * 32767).astype("<i2").tobytes())
    except Exception:
        log_error("synthesize")
        return None

    clip_id = str(int(time.time() * 1000))
    clip_path = RING_DIR / f"{clip_id}.wav"
    try:
        RING_DIR.mkdir(exist_ok=True)
        write_wav(clip_path, pcm_frames)
        ring_put(clip_id, clip_path)
    except Exception:
        log_error("ring_write")

    duration = total_samples / engine.sample_rate / min(
        2.0, max(0.5, float(config["rate"]))
    )

    with state_lock:
        if is_stale(key, gen):
            return None
        proc = player_proc(clip_path)
        active_player = proc
        active_cwd = cwd
        last_spoken_ts = time.time()
        now_speaking = {
            "text": text[:240], "voice": voice, "kind": kind,
            "project": project_name(cwd), "ts": last_spoken_ts,
        }
    publish({"event": "now", "on": True, **now_speaking})
    publish({
        "event": "wave", "peaks": downsample_peaks(peaks),
        "duration": round(duration, 2), "clip": clip_id,
    })
    _finish(proc)
    return clip_id


def _finish(proc) -> None:
    global active_player, active_cwd, now_speaking
    try:
        if proc and proc.stdin:
            try:
                proc.stdin.close()
            except OSError:
                pass
        if proc:
            proc.wait()
    finally:
        with state_lock:
            if active_player is proc:
                active_player = None
                active_cwd = None
            now_speaking = None
        publish({"event": "now", "on": False})


def replay(clip_id: str) -> bool:
    path = clip_ring.get(clip_id)
    if not path or not path.exists():
        return False
    player_proc(path)
    return True


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
            [FFMPEG, "-y", "-i", str(src), "-ac", "1", "-ar", str(SAMPLE_RATE), str(dst)],
            capture_output=True, timeout=30,
        )
        if conv.returncode != 0 or not dst.exists():
            return False, "could not decode that audio file"
        try:
            ENGINES["pocket"].load().get_state_for_audio_prompt(str(dst))
        except Exception as exc:
            dst.unlink(missing_ok=True)
            msg = str(exc)
            if "voice cloning" in msg or "download the weights" in msg:
                return False, "cloning locked: accept terms at huggingface.co/kyutai/pocket-tts and run hf auth login"
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
    _, job = pending.popitem()
    return job


def process_job(job: dict) -> None:
    global last_stages
    gen = job["gen"]
    raw = job["raw"]
    cwd = job["cwd"]
    kind = job["kind"]
    key = (cwd, kind)
    if job.get("prepared"):
        text, meta = raw, {}
    else:
        text, meta = prepare_speech(raw)
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
    if not text:
        append_log("skip_empty", cwd=cwd, **meta)
        return
    if in_quiet_hours():
        append_log("quiet_skip", cwd=cwd, kind=kind)
        return
    prefix, prefixed = "", False
    if kind == "reply":
        prefix, prefixed = prefix_for_project(project_name(cwd))
        if prefixed:
            text = prefix + text
    with state_lock:
        stale = is_stale(key, gen)
        current_generation = generation_for(key)
    if stale:
        append_log(
            "speak_stale", cwd=cwd, generation=gen, current_generation=current_generation
        )
        return
    voice = voice_for_job(job)
    clip_id = speak(text, gen, key, cwd, voice, kind)
    if clip_id is not None:
        append_log(
            "speak",
            cwd=cwd,
            project=project_name(cwd),
            kind=kind,
            voice=voice,
            prefixed=prefixed,
            prefix=prefix.strip(),
            spoken_chars=len(text),
            clip=clip_id,
            **meta,
        )


def worker() -> None:
    global worker_running, working_cwd
    while True:
        with state_lock:
            if not pending:
                worker_running = False
                return
            job = pick_job_locked()
            working_cwd = job["cwd"]
        try:
            process_job(job)
        except Exception:
            log_error("worker")
        finally:
            with state_lock:
                if working_cwd == job["cwd"]:
                    working_cwd = None


def enqueue_speak(
    raw: str,
    cwd: str | None = None,
    kind: str = "reply",
    agent: str | None = None,
    voice: str | None = None,
    prepared: bool = False,
) -> int:
    global worker_running, generation
    with state_lock:
        generation += 1
        gen = generation
        key = (cwd, kind)
        generations[key] = gen
        pending[key] = {
            "gen": gen,
            "raw": raw,
            "cwd": cwd,
            "kind": kind,
            "agent": agent,
            "voice": voice,
            "prepared": prepared,
        }
        if not worker_running:
            worker_running = True
            threading.Thread(target=worker, daemon=True).start()
        return gen


def stop_speech(cwd: str | None = None) -> int:
    global generation, working_cwd
    with state_lock:
        pending_cwds = sorted({key[0] or "" for key in pending})
        matches_pending = any(key[0] == cwd for key in pending)
        should_stop = (
            cwd is None or matches_pending or working_cwd == cwd or active_cwd == cwd
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
        if cwd is None or working_cwd == cwd:
            working_cwd = None
        player = take_active_player() if cwd is None or active_cwd == cwd else None
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
                json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "engine": active_engine_name(),
                        "model_loaded": active_engine().loaded(),
                        "uptime_s": round(now() - started_at, 1),
                        "last_spoken_ts": last_spoken_ts,
                        "now_speaking": now_speaking,
                        "git_rev": GIT_REV,
                    },
                )
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
                    self.wfile.write(f"data: {json.dumps(hello, ensure_ascii=True)}\n\n".encode())
                    self.wfile.flush()
                    while True:
                        try:
                            ev = sub.get(timeout=15)
                            self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=True)}\n\n".encode())
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
            if route == "/voices":
                json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "voices": all_voices(),
                        "custom": custom_voices(),
                        "assigned": config["voices"],
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
            if self.path == "/speak":
                data = read_json(self)
                text = data.get("text") or data.get("last_assistant_message") or ""
                cwd = data.get("cwd")
                if not text.strip() and data.get("transcript_path"):
                    text = transcript_last_assistant(data["transcript_path"])
                    if text.strip():
                        append_log("transcript_fallback", cwd=cwd)
                if not text.strip():
                    json_response(self, 202, {"ok": True, "skipped": True})
                    return
                kind = "notification" if data.get("event") == "Notification" else "reply"
                gen = enqueue_speak(text, cwd, kind=kind, agent=data.get("agent"))
                json_response(self, 202, {"ok": True, "generation": gen})
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
                voice = data.get("voice")
                if voice not in all_voices():
                    json_response(self, 400, {"ok": False, "error": "unknown_voice"})
                    return
                text = data.get("text") or AUDITION_TEXT.format(voice=voice.replace("_", " "))
                gen = enqueue_speak(
                    text, None, kind="audition", voice=voice, prepared=True
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
                tableless, _ = strip_markdown_tables(last_stages["raw"])
                cleaned = strip_markdown(tableless)
                out = condense(cleaned)
                if out is None:
                    out = f"Summary failed, first line: {first_sentence(cleaned)}"
                enqueue_speak(out, None, kind="audition", prepared=True)
                json_response(self, 200, {"ok": True, "text": out})
                return
            json_response(self, 404, {"error": "not_found"})
        except Exception:
            log_error("do_POST")
            json_response(self, 202, {"ok": False})


def main() -> int:
    load_config()
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        return 0
    threading.Thread(target=idle_watch, daemon=True).start()
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
