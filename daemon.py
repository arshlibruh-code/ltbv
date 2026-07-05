#!/usr/bin/env python3
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 7333
IDLE_EXIT_S = 600
MAX_DIRECT_CHARS = 1000
ENGINE = "pocket"
DEEPSEEK_SETTINGS = Path.home() / ".claude" / "settings.deepseek.json"
DEEPSEEK_FAILED_TEXT = "DeepSeek failed. I could not condense this long reply."
DEEPSEEK_TIMEOUT_S = 12
TABLE_FALLBACK_TEXT = (
    "I made a table for this, but I won't read the whole thing out loud. "
    "Take a look and tell me what you think."
)
PROJECT_WINDOW_S = 600
PREFIX_TEMPLATES = ("In {project}: ", "From {project}: ", "Update from {project}: ")
LOG_PATH = ROOT / ".voice.log"


state_lock = threading.Lock()
pending = None
worker_running = False
generation = 0
active_player = None
active_cwd = None
working_cwd = None
last_activity = time.monotonic()
recent_projects = {}
prefix_counter = 0

tts_model = None
tts_voice_state = None


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


def append_log(event: str, **fields) -> None:
    try:
        payload = {"ts": round(time.time(), 3), "event": event, **fields}
        with LOG_PATH.open("a") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
    except Exception:
        return


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
        if re.search(r"(^|/)[\w.-]+/[\w./-]+", s):
            continue
        if re.search(r"\bhttps?://\S+", s):
            continue
        if re.search(r"\.(py|js|ts|tsx|jsx|json|toml|md|html|css|sh)(:\d+)?\b", s):
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
    return text[:MAX_DIRECT_CHARS].strip()


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
                {
                    "role": "system",
                    "content": (
                        "You are speaking for a coding agent after it finishes a turn. "
                        "The user hears this out loud, so sound like a calm coding partner, not a report. "
                        "Say what changed, what matters, or what the user should notice next. "
                        "Use 2-3 short natural sentences. "
                        "Do not read tables, code, diffs, file paths, URLs, markdown, bullets, or headings. "
                        "If the reply mostly contains a table, say that a table was made and invite the user to inspect it."
                    ),
                },
                {
                    "role": "user",
                    "content": text,
                },
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
        with urllib.request.urlopen(req, timeout=DEEPSEEK_TIMEOUT_S) as resp:
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
    if len(text) > MAX_DIRECT_CHARS:
        condensed = condense(text)
        if condensed is None:
            meta["condense"] = "failed"
            return DEEPSEEK_FAILED_TEXT, meta
        meta["condense"] = "ok"
        return strip_markdown(condensed), meta
    meta["condense"] = "not_needed"
    return text, meta


def prepare_text(raw: str) -> str:
    return prepare_speech(raw)[0]


def project_name(cwd: str | None) -> str | None:
    if not cwd:
        return None
    name = Path(cwd).name.strip()
    return name or None


def prefix_for_project(project: str | None) -> tuple[str, bool]:
    global prefix_counter
    if not project:
        return "", False

    cutoff = now() - PROJECT_WINDOW_S
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


def load_pocket():
    global tts_model, tts_voice_state
    if tts_model is not None:
        return tts_model, tts_voice_state
    from pocket_tts.default_parameters import get_default_voice_for_language
    from pocket_tts.models.tts_model import TTSModel

    tts_model = TTSModel.load_model(language="english")
    voice = get_default_voice_for_language("english")
    tts_voice_state = tts_model.get_state_for_audio_prompt(voice)
    return tts_model, tts_voice_state


def synthesize(text: str, gen: int) -> Path | None:
    if ENGINE != "pocket":
        return None
    model, voice_state = load_pocket()
    chunks = []
    for chunk in model.generate_audio_stream(
        model_state=voice_state, text_to_generate=text
    ):
        with state_lock:
            if gen != generation:
                return None
        chunks.append(chunk)
    fd, name = tempfile.mkstemp(prefix="ltbv-", suffix=".wav")
    os.close(fd)
    path = Path(name)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(model.sample_rate)
        for chunk in chunks:
            data = chunk.detach().cpu().clamp(-1, 1).numpy()
            pcm = (data * 32767).astype("<i2").tobytes()
            wav.writeframes(pcm)
    return path


def kill_player() -> None:
    player = take_active_player()
    if player and player.poll() is None:
        try:
            os.killpg(player.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def take_active_player():
    global active_player, active_cwd
    player = active_player
    active_player = None
    active_cwd = None
    return player


def play(path: Path, gen: int, cwd: str | None) -> None:
    global active_player, active_cwd
    with state_lock:
        if gen != generation:
            return
        active_player = subprocess.Popen(["afplay", str(path)], start_new_session=True)
        active_cwd = cwd
        player = active_player
    try:
        player.wait()
    finally:
        with state_lock:
            if active_player is player:
                active_player = None
                active_cwd = None
        try:
            path.unlink()
        except OSError:
            pass


def worker() -> None:
    global pending, worker_running, working_cwd
    while True:
        with state_lock:
            job = pending
            pending = None
            if job is None:
                worker_running = False
                return
        gen, raw, cwd = job
        with state_lock:
            working_cwd = cwd
        text, meta = prepare_speech(raw)
        if not text:
            append_log("skip_empty", cwd=cwd, **meta)
            with state_lock:
                if working_cwd == cwd:
                    working_cwd = None
            continue
        project = project_name(cwd)
        prefix, prefixed = prefix_for_project(project)
        if prefixed:
            text = prefix + text
        with state_lock:
            stale = gen != generation
        if stale:
            with state_lock:
                if working_cwd == cwd:
                    working_cwd = None
            append_log(
                "speak_stale", cwd=cwd, generation=gen, current_generation=generation
            )
            continue
        wav_path = synthesize(text, gen)
        with state_lock:
            if working_cwd == cwd:
                working_cwd = None
        if wav_path is not None:
            append_log(
                "speak",
                cwd=cwd,
                project=project,
                prefixed=prefixed,
                prefix=prefix.strip(),
                spoken_chars=len(text),
                **meta,
            )
            play(wav_path, gen, cwd)


def enqueue_speak(raw: str, cwd: str | None = None) -> int:
    global pending, worker_running, generation
    with state_lock:
        generation += 1
        gen = generation
        pending = (gen, raw, cwd)
        if not worker_running:
            worker_running = True
            threading.Thread(target=worker, daemon=True).start()
        return gen


def stop_speech(cwd: str | None = None) -> int:
    global generation, pending, working_cwd
    with state_lock:
        pending_cwd = pending[2] if pending else None
        should_stop = (
            cwd is None or pending_cwd == cwd or working_cwd == cwd or active_cwd == cwd
        )
        if not should_stop:
            append_log(
                "stop_ignored",
                cwd=cwd,
                active_cwd=active_cwd,
                pending_cwd=pending_cwd,
                working_cwd=working_cwd,
            )
            return generation
        generation += 1
        gen = generation
        pending = None
        if cwd is None or working_cwd == cwd:
            working_cwd = None
        player = take_active_player()
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
        if idle > IDLE_EXIT_S and not busy:
            os._exit(0)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        bump_activity()
        if self.path == "/health":
            json_response(self, 200, {"ok": True, "engine": ENGINE})
        else:
            json_response(self, 404, {"error": "not_found"})

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
                if not text.strip():
                    json_response(self, 202, {"ok": True, "skipped": True})
                    return
                gen = enqueue_speak(text, cwd)
                json_response(self, 202, {"ok": True, "generation": gen})
                return
            json_response(self, 404, {"error": "not_found"})
        except Exception:
            json_response(self, 202, {"ok": False})


def main() -> int:
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        return 0
    threading.Thread(target=idle_watch, daemon=True).start()
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
