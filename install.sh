#!/bin/bash
set -euo pipefail

REPO_URL="${LTBV_REPO_URL:-https://github.com/arshlibruh-code/ltbv.git}"
INSTALL_ROOT="${LTBV_HOME:-$HOME/.local/share/ltbv}"
BIN_DIR="${LTBV_BIN_DIR:-$HOME/.local/bin}"
CLAUDE_SETTINGS="$HOME/.claude/settings.json"
CODEX_CONFIG="$HOME/.codex/config.toml"
STAMP=$(date +%Y%m%d%H%M%S)

say() { printf '[ltbv] %s\n' "$1"; }

if [ "$(uname -s)" != "Darwin" ]; then
  say "macOS is required."
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  say "git is required."
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  say "uv not found, installing it from Astral."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

mkdir -p "$(dirname "$INSTALL_ROOT")"
if [ -d "$INSTALL_ROOT/.git" ]; then
  say "updating $INSTALL_ROOT"
  git -C "$INSTALL_ROOT" fetch origin main
  git -C "$INSTALL_ROOT" checkout main
  git -C "$INSTALL_ROOT" pull --ff-only origin main
else
  say "cloning ltbv into $INSTALL_ROOT"
  git clone --branch main "$REPO_URL" "$INSTALL_ROOT"
fi

say "installing locked Python dependencies"
uv sync --project "$INSTALL_ROOT" --locked

mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/ltbv" <<'EOF'
#!/bin/sh
set -u
ROOT="${LTBV_HOME:-$HOME/.local/share/ltbv}"

case "${1:-}" in
  open|controller)
    "$ROOT/voice" say "Let there be voice is live. I can talk back now." >/dev/null 2>&1
    open http://127.0.0.1:7333/
    ;;
  doctor)
    "$ROOT/voice" status
    command -v uv >/dev/null 2>&1 && echo "uv: ok" || echo "uv: missing"
    curl -sf -m 1 http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && echo "ollama: reachable" || echo "ollama: not running (optional)"
    ;;
  *)
    exec "$ROOT/voice" "$@"
    ;;
esac
EOF
chmod +x "$BIN_DIR/ltbv"

backup_file() {
  local path="$1"
  [ -f "$path" ] || return 0
  cp "$path" "$path.ltbv-backup-$STAMP"
  say "backed up $path"
}

backup_file "$CLAUDE_SETTINGS"
backup_file "$CODEX_CONFIG"

LTBV_INSTALL_ROOT="$INSTALL_ROOT" \
LTBV_CLAUDE_SETTINGS="$CLAUDE_SETTINGS" \
LTBV_CODEX_CONFIG="$CODEX_CONFIG" \
"$INSTALL_ROOT/.venv/bin/python" - <<'PY'
import json
import os
import re
from pathlib import Path

root = Path(os.environ["LTBV_INSTALL_ROOT"])
command = f"{root}/.venv/bin/python {root}/hook.py"

claude_path = Path(os.environ["LTBV_CLAUDE_SETTINGS"])
if claude_path.exists():
    data = json.loads(claude_path.read_text())
else:
    data = {}

hooks = data.setdefault("hooks", {})
for event in ("Stop", "UserPromptSubmit", "Notification"):
    groups = hooks.setdefault(event, [])
    found = False
    for group in groups:
        if not isinstance(group, dict):
            continue
        entries = group.setdefault("hooks", [])
        kept = []
        for entry in entries:
            if isinstance(entry, dict) and "let-there-be-voice" in str(entry.get("command", "")):
                continue
            kept.append(entry)
        group["hooks"] = kept
        if any(entry.get("command") == command for entry in kept if isinstance(entry, dict)):
            found = True
    if not found:
        groups.append({"hooks": [{"type": "command", "command": command}]})
claude_path.parent.mkdir(parents=True, exist_ok=True)
claude_path.write_text(json.dumps(data, indent=2) + "\n")

codex_path = Path(os.environ["LTBV_CODEX_CONFIG"])
if codex_path.exists():
    text = codex_path.read_text()
else:
    text = ""

text = re.sub(r"\n?# BEGIN LTBV\n.*?\n# END LTBV\n?", "\n", text, flags=re.S)
text = re.sub(r"\n?# let-there-be-voice hooks\n.*?(?=^\[hooks\.state\]|\Z)", "\n", text, flags=re.S | re.M)
block = f'''# BEGIN LTBV
[[hooks.Stop]]
[[hooks.Stop.hooks]]
type = "command"
command = "{command}"

[[hooks.UserPromptSubmit]]
[[hooks.UserPromptSubmit.hooks]]
type = "command"
command = "{command}"

[[hooks.Notification]]
[[hooks.Notification.hooks]]
type = "command"
command = "{command}"
# END LTBV
'''
codex_path.parent.mkdir(parents=True, exist_ok=True)
codex_path.write_text(text.rstrip() + "\n\n" + block)
PY

say "installed"
say "Spotify ducking may ask for macOS Automation permission on first use."
say "optional Browser/YouTube adapter: $INSTALL_ROOT/browser-extension"
say "run: $BIN_DIR/ltbv open"
say "run: $BIN_DIR/ltbv doctor"
