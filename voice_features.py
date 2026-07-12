#!/usr/bin/env python3
import hashlib
import json
import math
import os
import re
import struct
import subprocess
import time
from datetime import datetime
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

META_SPEECH_PATTERNS = (
    r"\bno action needed\b",
    r"\bjust run git status\b",
    r"\bthe (?:assistant|response|reply|prompt|user)\b",
    r"\bthis (?:summary|response|reply)\b",
    r"\bi (?:summarized|condensed)\b",
    r"\bas an ai\b",
    r"\blet me know what you think\b",
)

TRIVIAL_SUCCESS = re.compile(
    r"^(?:done|fixed|complete|completed|ready|finished|all set|sorted)[.!]*$",
    re.IGNORECASE,
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
        return out.stdout.rstrip() if out.returncode == 0 else ""
    except Exception:
        return ""


def git_root(cwd: str | None) -> str | None:
    root = _run_git(cwd, "rev-parse", "--show-toplevel")
    return root or None


def git_snapshot(cwd: str | None) -> dict:
    root = git_root(cwd)
    if not root:
        return {}
    status = _run_git(root, "status", "--porcelain=v1", "--untracked-files=all")
    fingerprints = {}
    for _, relative in _status_paths(status)[:200]:
        path = Path(root) / relative
        try:
            if path.is_file() and path.stat().st_size <= 5_000_000:
                fingerprints[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            pass
    return {
        "root": root,
        "head": _run_git(root, "rev-parse", "HEAD"),
        "branch": _run_git(root, "branch", "--show-current"),
        "status": status,
        "fingerprints": fingerprints,
        "captured_at": time.time(),
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


def concept_for_path(path: str) -> str:
    lowered = path.lower()
    name = Path(path).name.lower()
    if "browser-extension" in lowered:
        return "browser extension"
    if name == "daemon.py":
        return "voice daemon"
    if name == "controller.html":
        return "controller"
    if name == "hook.py":
        return "agent hooks"
    if name == "install.sh":
        return "installer"
    if name == "voice":
        return "command-line controls"
    if name == "smoke.sh" or name.startswith("test_"):
        return "tests"
    if name in {"readme.md", "claude.md", "field-guide.html", "build-packet.html"}:
        return "documentation"
    stem = Path(path).stem.replace("_", " ").replace("-", " ").strip()
    return stem or "project files"


def _human_symbol(value: str) -> str:
    value = re.sub(r"^(?:test_|get_|set_|is_)", "", value or "")
    return value.replace("_", " ").replace("-", " ").strip()


def semantic_diff_facts(root: str, before: dict, files: list[str], current_head: str) -> list[str]:
    baseline_dirty = {path for _, path in _status_paths(before.get("status", ""))}
    attributable = [path for path in files if path not in baseline_dirty][:20]
    if not attributable:
        return []
    before_head = before.get("head")
    patches = []
    if before_head and current_head and before_head != current_head:
        patches.append(_run_git(root, "diff", "--unified=1", before_head, current_head, "--", *attributable))
    patches.append(_run_git(root, "diff", "--unified=1", "--", *attributable))
    tracked = set(_run_git(root, "ls-files").splitlines())
    for relative in attributable:
        path = Path(root) / relative
        try:
            if path.is_file() and relative not in tracked and path.stat().st_size <= 100_000:
                patches.append("\n".join("+" + line for line in path.read_text(errors="ignore").splitlines()))
        except OSError:
            pass
    patch = "\n".join(patches)[:80_000]
    if not patch:
        return []

    removed = {}
    added = {}
    tests = []
    symbols = []
    routes = []
    assignment = re.compile(r"^[+-]\s*[\"']?([A-Za-z][\w.-]*)[\"']?\s*[:=]\s*([^,}\n#]+)")
    for line in patch.splitlines():
        if not line.startswith(("+", "-")) or line.startswith(("+++", "---")):
            continue
        sign, body = line[0], line[1:].strip()
        match = assignment.match(line)
        if match:
            target = added if sign == "+" else removed
            target[match.group(1)] = match.group(2).strip().strip("\"'")[:60]
        symbol = re.match(r"(?:async\s+)?def\s+([A-Za-z_]\w*)|class\s+([A-Za-z_]\w*)", body)
        if sign == "+" and symbol:
            name = symbol.group(1) or symbol.group(2)
            human = _human_symbol(name)
            if name.startswith("test_"):
                tests.append(human)
            elif not name.startswith("_"):
                symbols.append(human)
        route = re.search(r"(?:self\.path|route)\s*==\s*[\"']([^\"']+)", body)
        if sign == "+" and route:
            routes.append(route.group(1))

    facts = []
    for key in sorted(set(removed) & set(added)):
        if removed[key] != added[key]:
            facts.append(f"{_human_symbol(key)} changed from {removed[key]} to {added[key]}")
    for route in routes[:2]:
        facts.append(f"added the {route} endpoint")
    for name in symbols[:2]:
        facts.append(f"added {name} behavior")
    for name in tests[:2]:
        facts.append(f"added coverage for {name}")
    return facts[:5]


def git_change_summary(cwd: str | None, before: dict | None = None) -> dict:
    root = git_root(cwd)
    if not root:
        return {"count": 0, "files": [], "summary": "Git evidence unavailable.", "verified": False, "reason": "not a Git repository"}

    changes = []
    before = before or {}
    if before and before.get("root") != root:
        return {"count": 0, "files": [], "summary": "Git evidence unavailable because the repository changed.", "verified": False, "reason": "repository changed"}
    before_head = before.get("head") if before.get("root") == root else None
    current_head = _run_git(root, "rev-parse", "HEAD")
    if before_head and current_head and before_head != current_head:
        changes.extend(_diff_paths(_run_git(root, "diff", "--name-status", before_head, current_head, "--")))
    current_status = _status_paths(_run_git(root, "status", "--porcelain=v1", "--untracked-files=all"))
    baseline = {path: status for status, path in _status_paths(before.get("status", ""))}
    fingerprints = before.get("fingerprints") or {}
    for status, path in current_status:
        changed = path not in baseline or baseline.get(path) != status
        if not changed and path in fingerprints:
            try:
                current = hashlib.sha256((Path(root) / path).read_bytes()).hexdigest()
                changed = current != fingerprints[path]
            except OSError:
                changed = True
        if changed:
            changes.append((status, path))

    deduped = {}
    for status, path in changes:
        deduped[path] = status
    files = list(deduped)
    old_branch = before.get("branch")
    branch = _run_git(root, "branch", "--show-current")
    branch_changed = bool(old_branch and branch and old_branch != branch)
    if not files:
        summary = f"Switched from {old_branch} to {branch}." if branch_changed else ""
        return {"count": 0, "files": [], "summary": summary, "verified": True, "branch_changed": branch_changed}

    concepts = []
    for path in files:
        concept = concept_for_path(path)
        if concept not in concepts:
            concepts.append(concept)
    shown = concepts[:4]
    if len(shown) == 1:
        phrase = shown[0]
    elif len(shown) == 2:
        phrase = " and ".join(shown)
    else:
        phrase = ", ".join(shown[:-1]) + f", and {shown[-1]}"
    extra = len(concepts) - len(shown)
    suffix = f", plus {extra} more area" + ("s" if extra != 1 else "") if extra else ""
    oversized = len(files) > 25
    summary = (
        f"A large change touched {len(files)} files across {len(concepts)} areas."
        if oversized
        else f"Updated the {phrase}{suffix}."
    )
    if branch_changed:
        summary = f"Switched from {old_branch} to {branch}. {summary}"
    semantic = semantic_diff_facts(root, before, files, current_head)
    if semantic and not oversized:
        summary = semantic[0].rstrip(".") + "."
        if len(semantic) > 1:
            summary += " " + semantic[1].rstrip(".") + "."
    return {
        "count": len(files),
        "files": files,
        "concepts": concepts,
        "statuses": deduped,
        "summary": summary,
        "verified": True,
        "branch_changed": branch_changed,
        "oversized": oversized,
        "semantic": semantic,
    }


def request_intent(text: str) -> str:
    lowered = (text or "").lower()
    tags = []
    actions = (
        ("fix", r"\b(?:fix|debug|repair)\b"),
        ("build", r"\b(?:build|add|implement|create|make)\b"),
        ("remove", r"\b(?:remove|delete|drop)\b"),
        ("test", r"\b(?:test|verify|check|smoke)\b"),
        ("review", r"\b(?:review|inspect|audit)\b"),
        ("research", r"\b(?:research|search|investigate)\b"),
        ("explain", r"\b(?:explain|tell me|what is|how does)\b"),
    )
    for tag, pattern in actions:
        if re.search(pattern, lowered) and tag not in tags:
            tags.append(tag)
    constraints = (
        ("do not commit", r"\b(?:do not|don't|dont) commit\b"),
        ("do not push", r"\b(?:(?:do not|don't|dont) push|(?:do not|don't|dont) commit\s+or\s+push)\b"),
        ("ask first", r"\b(?:ask me|confirm)\b.*\b(?:first|before|whenever)\b"),
        ("commit", r"\bcommit\b"),
        ("push", r"\bpush\b"),
        ("deploy", r"\bdeploy\b"),
    )
    for tag, pattern in constraints:
        if re.search(pattern, lowered) and tag not in tags:
            if tag == "commit" and "do not commit" in tags:
                continue
            if tag == "push" and "do not push" in tags:
                continue
            tags.append(tag)
    if not tags and "?" in text:
        tags.append("question")
    return " + ".join(tags[:5]) or "general request"


TEST_COMMANDS = (
    ("smoke", re.compile(r"(?:^|\s)(?:\./)?smoke\.sh(?:\s|$)")),
    ("unit tests", re.compile(r"\b(?:pytest|unittest|vitest|jest|cargo test)\b")),
    ("build", re.compile(r"\b(?:npm|pnpm|yarn)\s+(?:run\s+)?build\b|\bxcodebuild\b")),
)


def transcript_tool_evidence(path_str: str | None, started_at: float = 0, turn_id: str | None = None) -> dict:
    result = {"verification": "unknown", "tests": [], "commands_seen": 0}
    if not path_str:
        return result
    try:
        path = Path(path_str)
        if not path.is_file():
            return result
        lines = path.read_bytes()[-2_000_000:].decode("utf-8", "ignore").splitlines()
    except OSError:
        return result
    calls = {}
    outcomes = []

    def record(command, output="", failed=None, exit_code=None):
        command = str(command or "")
        labels = [label for label, pattern in TEST_COMMANDS if pattern.search(command)]
        if not labels:
            return
        result["commands_seen"] += 1
        for label in labels:
            if label not in result["tests"]:
                result["tests"].append(label)
        parsed = output
        if isinstance(output, str):
            try:
                candidate = json.loads(output)
                if isinstance(candidate, dict):
                    parsed = candidate
            except Exception:
                pass
        if isinstance(parsed, dict):
            exit_code = parsed.get("exit_code", parsed.get("returncode", parsed.get("exitCode", exit_code)))
            if failed is None and isinstance(parsed.get("success"), bool):
                failed = not parsed["success"]
            text = str(parsed.get("output") or parsed.get("stdout") or parsed.get("stderr") or "").lower()
        else:
            text = str(output or "").lower()
        if exit_code is not None:
            bad = int(exit_code) != 0
            good = not bad
        elif failed is not None:
            bad = bool(failed)
            good = not bad
        else:
            bad = bool(re.search(r"\b(?:fail(?:ed|ure)?|error)\b", text))
            good = bool(re.search(r"\b(?:pass(?:ed)?|ok|success)\b", text))
        outcomes.append("failed" if bad else "passed" if good else "unknown")

    active_turn = None
    for line in lines:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj
        if payload.get("type") == "turn_context":
            active_turn = payload.get("turn_id") or active_turn
        current_turn = payload.get("turn_id") or obj.get("turn_id")
        line_turn = current_turn or active_turn
        if turn_id and line_turn and str(line_turn) != str(turn_id):
            continue
        stamp = obj.get("timestamp", obj.get("ts", payload.get("timestamp", payload.get("ts"))))
        event_time = None
        if isinstance(stamp, (int, float)):
            event_time = float(stamp)
        elif isinstance(stamp, str):
            try:
                event_time = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
        if started_at and event_time is not None and event_time < started_at:
            continue
        if started_at and event_time is None and not line_turn:
            continue
        item = payload.get("message") if payload.get("type") in {"assistant", "user"} else payload
        content = item.get("content") if isinstance(item, dict) else None
        blocks = content if isinstance(content, list) else []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                inp = block.get("input") or {}
                calls[block.get("id")] = inp.get("command") or inp.get("cmd") or block.get("name", "")
            elif block.get("type") == "tool_result":
                command = calls.get(block.get("tool_use_id"), "")
                record(command, block.get("content", ""), True if block.get("is_error") else None)
        if payload.get("type") in {"function_call", "custom_tool_call"}:
            args = payload.get("arguments") or payload.get("input") or ""
            try:
                parsed = json.loads(args) if isinstance(args, str) else args
            except Exception:
                parsed = {}
            calls[payload.get("call_id")] = (parsed or {}).get("cmd") or (parsed or {}).get("command") or payload.get("name", "")
        if payload.get("type") in {"function_call_output", "custom_tool_call_output"}:
            record(calls.get(payload.get("call_id"), ""), payload.get("output", ""))
        tool_result = obj.get("toolUseResult") or payload.get("toolUseResult")
        if isinstance(tool_result, dict):
            command = tool_result.get("command") or tool_result.get("commandName") or ""
            output = tool_result.get("stdout") or tool_result.get("content") or ""
            failed = None if "success" not in tool_result else not bool(tool_result.get("success"))
            exit_code = tool_result.get("exit_code", tool_result.get("returncode"))
            record(command, output, failed, exit_code)
    if "failed" in outcomes:
        result["verification"] = "failed"
    elif "passed" in outcomes:
        result["verification"] = "passed"
    return result


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


def _sentences(text: str) -> list[str]:
    clean = re.sub(r"\s+", " ", text or "").strip()
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", clean) if part.strip()]


def verification_state(source: str) -> str:
    lowered = (source or "").lower()
    if re.search(r"smoke fail|tests? failed|check failed|build failed", lowered):
        return "failed"
    if re.search(r"not tested|untested|tests? (?:were )?not run|visual check (?:was )?not run", lowered):
        return "untested"
    if re.search(r"smoke pass|tests? passed|all checks? (?:have )?passed|verified successfully", lowered):
        return "passed"
    return "unknown"


def contract_fallback(
    source: str,
    intent: str,
    budget: int,
    diff_summary: str = "",
) -> str:
    sentences = _sentences(source)
    keywords = {
        "blocker": ("blocked", "failed", "error", "cannot", "unable", "conflict"),
        "needs_input": ("choose", "confirm", "which", "do you want", "need your input", "?"),
        "warning": ("warning", "untested", "not tested", "manual", "caveat", "remaining"),
        "success": ("passed", "fixed", "completed", "implemented", "merged", "done"),
        "update": ("updated", "changed", "added", "removed", "now"),
    }.get(intent, ())

    ranked = sorted(
        sentences,
        key=lambda sentence: sum(token in sentence.lower() for token in keywords),
        reverse=True,
    )
    chosen = ranked[0] if ranked else ""
    state = verification_state(source)
    if diff_summary and intent in {"success", "update"}:
        chosen = diff_summary
    if state == "failed":
        failed = next(
            (sentence for sentence in sentences if re.search(r"fail|error|blocked", sentence, re.I)),
            "Verification failed.",
        )
        chosen = failed
    elif state == "untested" and not re.search(r"untested|not tested|not run", chosen, re.I):
        chosen = (chosen.rstrip(". ") + ". It is not tested yet.").strip()
    elif state == "passed" and not re.search(r"pass|verified|green", chosen, re.I):
        chosen = (chosen.rstrip(". ") + ". Verification passed.").strip()
    return trim_to_words(chosen, budget)


def enforce_spoken_contract(
    candidate: str,
    source: str,
    intent: str,
    budget: int,
    diff_summary: str = "",
) -> tuple[str, dict]:
    candidate = re.sub(r"\s+", " ", candidate or "").strip()
    reasons = []
    if intent == "success" and TRIVIAL_SUCCESS.fullmatch(candidate):
        return "", {"contract": "earcon_only", "contract_reasons": ["trivial_success"]}
    for pattern in META_SPEECH_PATTERNS:
        if re.search(pattern, candidate, re.IGNORECASE):
            reasons.append("meta_speech")
            break
    state = verification_state(source)
    if state == "failed" and re.search(r"\b(pass|passes|passed|works|green|successful)\b", candidate, re.I):
        reasons.append("contradicts_failure")
    if state == "untested" and not re.search(r"untested|not tested|not run|needs? (?:a )?(?:manual|visual) check", candidate, re.I):
        reasons.append("hides_untested")
    if len(candidate.split()) < 3 and not TRIVIAL_SUCCESS.fullmatch(candidate):
        reasons.append("too_vague")
    if reasons:
        candidate = contract_fallback(source, intent, budget, diff_summary)
        mode = "fallback"
    else:
        candidate = trim_to_words(candidate, budget)
        mode = "accepted"
    return candidate, {"contract": mode, "contract_reasons": reasons}


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
