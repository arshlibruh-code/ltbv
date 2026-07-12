#!/usr/bin/env python3
import hashlib
import json
import math
import re
import struct
import subprocess
from pathlib import Path


INTENT_PATTERNS = {
    "blocker": (
        r"\bblocked\b",
        r"\bfailed\b",
        r"\bfailure\b",
        r"\berror\b",
        r"\bcannot\b",
        r"\bcan't\b",
        r"\bunable\b",
        r"permission denied",
        r"smoke fail",
        r"merge conflict",
    ),
    "needs_input": (
        r"needs? your input",
        r"\bchoose\b",
        r"\bconfirm\b",
        r"\btell me\b",
        r"\bsend me\b",
        r"\bpaste\b",
        r"\bwhich one\b",
        r"\bdo you want\b",
    ),
    "warning": (
        r"\bwarning\b",
        r"\bcaveat\b",
        r"\buntested\b",
        r"not tested",
        r"\bremaining\b",
        r"\bmanual check\b",
    ),
    "success": (
        r"smoke pass",
        r"all checks? (?:have )?passed",
        r"\bsuccessfully\b",
        r"\bimplemented\b",
        r"\bcompleted\b",
        r"\bfixed\b",
        r"\bmerged\b",
        r"working tree clean",
        r"nothing to commit",
        r"\bdone\b",
    ),
}

BUILD_PATTERNS = (
    ("tests_failed", (r"smoke fail", r"tests? failed", r"build failed", r"check failed")),
    ("tests_passed", (r"smoke pass", r"tests? passed", r"all checks? (?:have )?passed")),
    ("deployed", (r"deployment (?:is )?(?:complete|successful)", r"deployed successfully", r"pages deployed")),
    ("merged", (r"pull request successfully merged", r"\bmerged\b.*\bmain\b")),
)


def _run_git(cwd: str | None, *args: str) -> str:
    if not cwd:
        return ""
    try:
        out = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def git_root(cwd: str | None) -> str | None:
    root = _run_git(cwd, "rev-parse", "--show-toplevel")
    return root or None


def git_snapshot(cwd: str | None) -> dict:
    root = git_root(cwd)
    if not root:
        return {}
    return {
        "root": root,
        "head": _run_git(root, "rev-parse", "HEAD"),
        "status": _run_git(root, "status", "--porcelain=v1", "--untracked-files=all"),
    }


def _status_paths(raw: str) -> list[tuple[str, str]]:
    found = []
    for line in raw.splitlines():
        if len(line) < 4:
            continue
        status = line[:2].strip() or "?"
        path = line[3:].split(" -> ")[-1].strip()
        if path:
            found.append((status, path))
    return found


def _diff_paths(raw: str) -> list[tuple[str, str]]:
    found = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0].strip() or "?"
        path = parts[-1].strip()
        if path:
            found.append((status, path))
    return found


def git_change_summary(cwd: str | None, before: dict | None = None) -> dict:
    root = git_root(cwd)
    if not root:
        return {"count": 0, "files": [], "summary": "", "verified": False}

    changes = []
    before = before or {}
    before_head = before.get("head") if before.get("root") == root else None
    if before_head:
        changes.extend(_diff_paths(_run_git(root, "diff", "--name-status", before_head, "--")))
    changes.extend(_status_paths(_run_git(root, "status", "--porcelain=v1", "--untracked-files=all")))

    deduped = {}
    for status, path in changes:
        deduped[path] = status
    files = list(deduped)
    if not files:
        return {"count": 0, "files": [], "summary": "", "verified": True}

    shown = files[:4]
    names = ", ".join(shown)
    extra = len(files) - len(shown)
    suffix = f", plus {extra} more" if extra else ""
    noun = "file" if len(files) == 1 else "files"
    return {
        "count": len(files),
        "files": files,
        "statuses": deduped,
        "summary": f"Changed {len(files)} {noun}: {names}{suffix}.",
        "verified": True,
    }


def classify_intent(text: str, kind: str = "reply") -> str:
    if kind == "notification":
        return "needs_input"
    lowered = (text or "").lower()
    for intent in ("blocker", "needs_input", "warning", "success"):
        if any(re.search(pattern, lowered) for pattern in INTENT_PATTERNS[intent]):
            return intent
    if "?" in lowered:
        return "needs_input"
    return "update"


def detect_build_signal(text: str) -> str | None:
    lowered = (text or "").lower()
    for signal, patterns in BUILD_PATTERNS:
        if any(re.search(pattern, lowered) for pattern in patterns):
            return signal
    return None


def adaptive_word_budget(intent: str, kind: str = "reply", diff_count: int = 0) -> int:
    if kind == "notification":
        return 9
    if kind == "bulletin":
        return 40
    base = {
        "success": 16,
        "needs_input": 20,
        "blocker": 28,
        "warning": 24,
        "update": 22,
    }.get(intent, 22)
    if diff_count > 4:
        base += 4
    return base


def trim_to_words(text: str, limit: int) -> str:
    words = (text or "").split()
    if len(words) <= limit:
        return " ".join(words)
    trimmed = " ".join(words[:limit]).rstrip(" ,;:")
    return trimmed + "."


def pronunciation_paths(cwd: str | None) -> list[Path]:
    paths = [Path.home() / ".config" / "ltbv" / "pronounce.json"]
    root = git_root(cwd) or cwd
    if root:
        paths.append(Path(root) / ".ltbv" / "pronounce.json")
    return paths


def load_pronunciations(cwd: str | None) -> dict[str, str]:
    merged = {}
    for path in pronunciation_paths(cwd):
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for key, value in list(data.items())[:256]:
            key, value = str(key).strip(), str(value).strip()
            if 1 <= len(key) <= 80 and 1 <= len(value) <= 120:
                merged[key] = value
    return merged


def apply_pronunciations(text: str, cwd: str | None) -> tuple[str, int]:
    count = 0
    for source, spoken in sorted(
        load_pronunciations(cwd).items(), key=lambda item: len(item[0]), reverse=True
    ):
        left = r"(?<![A-Za-z0-9])" if source[0].isalnum() else ""
        right = r"(?![A-Za-z0-9])" if source[-1].isalnum() else ""
        pattern = left + re.escape(source) + right
        text, replaced = re.subn(pattern, spoken, text, flags=re.IGNORECASE)
        count += replaced
    return text, count


def _env_secret_values(cwd: str | None) -> list[str]:
    root = git_root(cwd) or cwd
    if not root:
        return []
    values = []
    try:
        candidates = sorted(Path(root).glob(".env*"))[:12]
    except Exception:
        return []
    for path in candidates:
        if any(tag in path.name.lower() for tag in ("example", "sample", "template")):
            continue
        try:
            if not path.is_file() or path.stat().st_size > 131_072:
                continue
            lines = path.read_text(errors="ignore").splitlines()
        except Exception:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            value = line.split("=", 1)[1].strip().strip("\"'")
            if len(value) >= 8 and value.lower() not in {"changeme", "undefined", "password"}:
                values.append(value)
    return sorted(set(values), key=len, reverse=True)


def redact_sensitive(text: str, cwd: str | None = None) -> tuple[str, int]:
    redacted = text or ""
    count = 0
    for value in _env_secret_values(cwd):
        if value in redacted:
            replacements = redacted.count(value)
            redacted = redacted.replace(value, "[redacted secret]")
            count += replacements

    patterns = (
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
        r"\b(?:sk-[A-Za-z0-9_-]{16,}|github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b",
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}",
    )
    for pattern in patterns:
        redacted, replaced = re.subn(pattern, "[redacted secret]", redacted)
        count += replaced

    assignment = re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password|passwd)"
        r"(\s*[:=]\s*)[\"']?([^\s\"'`,;]{6,})[\"']?"
    )

    def replace_assignment(match: re.Match) -> str:
        nonlocal count
        count += 1
        return f"{match.group(1)}{match.group(2)}[redacted secret]"

    redacted = assignment.sub(replace_assignment, redacted)

    query_secret = re.compile(
        r"(?i)([?&](?:token|key|secret|password|signature)=)[^&#\s]+"
    )

    def replace_query(match: re.Match) -> str:
        nonlocal count
        count += 1
        return match.group(1) + "[redacted]"

    redacted = query_secret.sub(replace_query, redacted)
    return redacted, count


def _tone(freq: float, duration: float, sample_rate: int, amplitude: float = 0.14) -> bytes:
    frames = max(1, int(duration * sample_rate))
    fade = max(1, int(0.008 * sample_rate))
    out = bytearray()
    for i in range(frames):
        envelope = min(1.0, i / fade, (frames - i - 1) / fade)
        sample = amplitude * envelope * math.sin(2 * math.pi * freq * i / sample_rate)
        out.extend(struct.pack("<h", int(max(-1, min(1, sample)) * 32767)))
    return bytes(out)


def _silence(duration: float, sample_rate: int) -> bytes:
    return b"\x00\x00" * max(1, int(duration * sample_rate))


def earcon_pcm(
    project: str | None,
    intent: str,
    build_signal: str | None,
    sample_rate: int,
    repo_enabled: bool = True,
    intent_enabled: bool = True,
    build_enabled: bool = True,
) -> tuple[bytes, dict]:
    sequence = []
    meta = {"repo_earcon": False, "intent_earcon": False, "build_signal": None}

    if repo_enabled and project:
        digest = hashlib.sha256(project.encode("utf-8")).digest()
        scale = (220.0, 247.0, 294.0, 330.0, 392.0, 440.0)
        sequence.extend((scale[digest[i] % len(scale)], 0.045) for i in range(3))
        meta["repo_earcon"] = True

    intent_notes = {
        "success": ((523.25, 0.055), (659.25, 0.07)),
        "needs_input": ((440.0, 0.055), (554.37, 0.085)),
        "blocker": ((392.0, 0.07), (277.18, 0.11)),
        "warning": ((392.0, 0.06), (369.99, 0.09)),
        "update": ((440.0, 0.065),),
    }
    if intent_enabled:
        sequence.extend(intent_notes.get(intent, intent_notes["update"]))
        meta["intent_earcon"] = True

    build_notes = {
        "tests_passed": ((523.25, 0.04), (659.25, 0.04), (783.99, 0.075)),
        "tests_failed": ((185.0, 0.08), (174.61, 0.12)),
        "deployed": ((659.25, 0.04), (783.99, 0.04), (987.77, 0.075)),
        "merged": ((392.0, 0.04), (523.25, 0.04), (783.99, 0.075)),
    }
    if build_enabled and build_signal in build_notes:
        sequence.extend(build_notes[build_signal])
        meta["build_signal"] = build_signal

    if not sequence:
        return b"", meta
    pcm = bytearray()
    for index, (freq, duration) in enumerate(sequence):
        pcm.extend(_tone(freq, duration, sample_rate))
        if index != len(sequence) - 1:
            pcm.extend(_silence(0.012, sample_rate))
    pcm.extend(_silence(0.045, sample_rate))
    return bytes(pcm), meta
