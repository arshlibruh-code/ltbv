#!/usr/bin/env python3
import argparse
import json
import resource
import subprocess
import time
import wave
from pathlib import Path


TEXT = "Codex finished the task and is ready for your next instruction."


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024


def write_wav(path: Path, sample_rate: int, chunks) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        for chunk in chunks:
            data = chunk.detach().cpu().clamp(-1, 1).numpy()
            pcm = (data * 32767).astype("<i2").tobytes()
            wav.writeframes(pcm)


def bench_pocket() -> dict:
    from pocket_tts.default_parameters import DEFAULT_FRAMES_AFTER_EOS, get_default_voice_for_language
    from pocket_tts.models.tts_model import TTSModel

    t0 = time.perf_counter()
    model = TTSModel.load_model(language="english")
    voice = get_default_voice_for_language("english")
    state = model.get_state_for_audio_prompt(voice)
    load_s = time.perf_counter() - t0

    chunks = []
    first_audio_s = None
    t1 = time.perf_counter()
    for chunk in model.generate_audio_stream(
        model_state=state,
        text_to_generate=TEXT,
        frames_after_eos=DEFAULT_FRAMES_AFTER_EOS,
        max_tokens=50,
    ):
        if first_audio_s is None:
            first_audio_s = time.perf_counter() - t1
        chunks.append(chunk)
    full_s = time.perf_counter() - t1
    write_wav(Path("bench/pocket.wav"), model.sample_rate, chunks)
    return {
        "engine": "pocket",
        "status": "ok",
        "load_s": round(load_s, 3),
        "warm_first_audio_s": round(first_audio_s or full_s, 3),
        "warm_full_s": round(full_s, 3),
        "rss_mb": round(rss_mb(), 1),
        "wav": "bench/pocket.wav",
    }


def bench_say() -> dict:
    path = Path("bench/say.wav")
    path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    subprocess.run(["say", "-v", "Samantha", "-r", "205", "-o", str(path), "--data-format=LEI16@22050", TEXT], check=True)
    return {
        "engine": "say",
        "status": "ok",
        "load_s": 0,
        "warm_first_audio_s": round(time.perf_counter() - t0, 3),
        "warm_full_s": round(time.perf_counter() - t0, 3),
        "rss_mb": round(rss_mb(), 1),
        "wav": "bench/say.wav",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("engine", choices=["pocket", "say"])
    args = parser.parse_args()
    if args.engine == "pocket":
        result = bench_pocket()
    else:
        result = bench_say()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
