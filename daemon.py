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


HOST = "127.0.0.1"
PORT = 7333
IDLE_EXIT_S = 600
MAX_DIRECT_CHARS = 350
ENGINE = "pocket"
DEEPSEEK_SETTINGS = Path.home() / ".claude" / "settings.deepseek.json"


state_lock = threading.Lock()
pending = None
worker_running = False
generation = 0
active_player = None
last_activity = time.monotonic()

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


def strip_markdown(text: str) -> str:
    text = re.sub(r"```[\s\S]*?```", " ", text)
    clean_lines = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith(("+++", "---", "@@", "diff ", "Traceback ", "File \"")):
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


def condense(text: str) -> str:
    try:
        env = read_deepseek_env()
        token = env.get("ANTHROPIC_AUTH_TOKEN")
        if not token:
            return first_sentence(text)
        body = {
            "model": "deepseek-v4-flash",
            "max_tokens": 80,
            "messages": [
                {
                    "role": "user",
                    "content": "Rewrite this as one natural spoken sentence under 32 words. No code, paths, URLs, bullets, or markdown.\n\n"
                    + text,
                }
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
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data.get("content") or []
        if content and isinstance(content[0], dict):
            return strip_markdown(content[0].get("text", "")) or first_sentence(text)
    except Exception:
        return first_sentence(text)
    return first_sentence(text)


def prepare_text(raw: str) -> str:
    text = strip_markdown(raw or "")
    if not text:
        return ""
    if len(text) > MAX_DIRECT_CHARS:
        text = condense(text)
    return strip_markdown(text)


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
    for chunk in model.generate_audio_stream(model_state=voice_state, text_to_generate=text):
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
    global active_player
    player = active_player
    active_player = None
    if player and player.poll() is None:
        try:
            os.killpg(player.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def play(path: Path, gen: int) -> None:
    global active_player
    with state_lock:
        if gen != generation:
            return
        active_player = subprocess.Popen(["afplay", str(path)], start_new_session=True)
        player = active_player
    try:
        player.wait()
    finally:
        with state_lock:
            if active_player is player:
                active_player = None
        try:
            path.unlink()
        except OSError:
            pass


def worker() -> None:
    global pending, worker_running
    while True:
        with state_lock:
            job = pending
            pending = None
            if job is None:
                worker_running = False
                return
        gen, raw = job
        text = prepare_text(raw)
        if not text:
            continue
        wav_path = synthesize(text, gen)
        if wav_path is not None:
            play(wav_path, gen)


def enqueue_speak(raw: str) -> int:
    global pending, worker_running, generation
    with state_lock:
        generation += 1
        gen = generation
        pending = (gen, raw)
        if not worker_running:
            worker_running = True
            threading.Thread(target=worker, daemon=True).start()
        return gen


def stop_speech() -> int:
    global generation, pending
    with state_lock:
        generation += 1
        gen = generation
        pending = None
        kill_player()
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
                gen = stop_speech()
                json_response(self, 202, {"ok": True, "generation": gen})
                return
            if self.path == "/speak":
                data = read_json(self)
                text = data.get("text") or data.get("last_assistant_message") or ""
                if not text.strip():
                    json_response(self, 202, {"ok": True, "skipped": True})
                    return
                gen = enqueue_speak(text)
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
