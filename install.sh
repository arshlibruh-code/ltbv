#!/bin/bash
set -euo pipefail

ACTION="${1:-install}"
REPO_URL="${LTBV_REPO_URL:-https://github.com/arshlibruh-code/ltbv.git}"
REF="${LTBV_REF:-main}"
INSTALL_ROOT="${LTBV_HOME:-$HOME/.local/share/ltbv}"
BIN_DIR="${LTBV_BIN_DIR:-$HOME/.local/bin}"
CLAUDE_SETTINGS="${LTBV_CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
CODEX_CONFIG="${LTBV_CODEX_CONFIG:-$HOME/.codex/config.toml}"
SHELL_PROFILE="${LTBV_SHELL_PROFILE:-$HOME/.zprofile}"
STAMP=$(date +%Y%m%d%H%M%S)

say() { printf '[ltbv] %s\n' "$1"; }
fail() { say "$1"; exit 1; }
has() { command -v "$1" >/dev/null 2>&1; }

check_platform() {
  [ "$(uname -s)" = "Darwin" ] || fail "macOS is required."
  case "$(uname -m)" in arm64|x86_64) ;; *) fail "unsupported Mac architecture: $(uname -m)" ;; esac
  has git || fail "git is required. Install Xcode Command Line Tools with: xcode-select --install"
  has curl || fail "curl is required."
}

backup_file() {
  local path="$1"
  [ -f "$path" ] || return 0
  cp "$path" "$path.ltbv-backup-$STAMP"
  say "backed up $path"
}

configure_hooks() {
  local mode="$1" python_bin="$2"
  LTBV_INSTALL_ROOT="$INSTALL_ROOT" \
  LTBV_CLAUDE_SETTINGS="$CLAUDE_SETTINGS" \
  LTBV_CODEX_CONFIG="$CODEX_CONFIG" \
  LTBV_HOOK_MODE="$mode" \
  "$python_bin" - <<'PY'
import json
import os
import re
from pathlib import Path

root = Path(os.environ["LTBV_INSTALL_ROOT"])
mode = os.environ["LTBV_HOOK_MODE"]
command = f"{root}/.venv/bin/python {root}/hook.py"

claude_path = Path(os.environ["LTBV_CLAUDE_SETTINGS"])
if claude_path.exists():
    data = json.loads(claude_path.read_text())
else:
    data = {}
hooks = data.setdefault("hooks", {})
for event in ("Stop", "UserPromptSubmit", "Notification"):
    groups = hooks.setdefault(event, [])
    for group in groups:
        if not isinstance(group, dict):
            continue
        entries = group.get("hooks") or []
        group["hooks"] = [
            entry for entry in entries
            if not (isinstance(entry, dict) and (
                entry.get("command") == command
                or "let-there-be-voice" in str(entry.get("command", ""))
                or "/ltbv/hook.py" in str(entry.get("command", ""))
            ))
        ]
    groups[:] = [group for group in groups if not isinstance(group, dict) or group.get("hooks")]
    if mode == "install":
        groups.append({"hooks": [{"type": "command", "command": command}]})
    if not groups:
        hooks.pop(event, None)
if not hooks:
    data.pop("hooks", None)
if data or claude_path.exists():
    claude_path.parent.mkdir(parents=True, exist_ok=True)
    claude_path.write_text(json.dumps(data, indent=2) + "\n")

codex_path = Path(os.environ["LTBV_CODEX_CONFIG"])
text = codex_path.read_text() if codex_path.exists() else ""
text = re.sub(r"\n?# BEGIN LTBV\n.*?\n# END LTBV\n?", "\n", text, flags=re.S)
text = re.sub(r"\n?# let-there-be-voice hooks\n.*?(?=^\[hooks\.state\]|\Z)", "\n", text, flags=re.S | re.M)
if mode == "install":
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
    text = text.rstrip() + "\n\n" + block
if text.strip() or codex_path.exists():
    codex_path.parent.mkdir(parents=True, exist_ok=True)
    codex_path.write_text(text.strip() + ("\n" if text.strip() else ""))
PY
}

remove_path_block() {
  [ -f "$SHELL_PROFILE" ] || return 0
  perl -0pi -e 's/\n?# BEGIN LTBV PATH\n.*?\n# END LTBV PATH\n?/\n/sg' "$SHELL_PROFILE"
}

install_path_block() {
  case ":$PATH:" in *":$BIN_DIR:"*) return 0 ;; esac
  backup_file "$SHELL_PROFILE"
  mkdir -p "$(dirname "$SHELL_PROFILE")"
  remove_path_block
  {
    printf '\n# BEGIN LTBV PATH\n'
    printf 'export PATH="%s:$PATH"\n' "$BIN_DIR"
    printf '# END LTBV PATH\n'
  } >> "$SHELL_PROFILE"
  say "added $BIN_DIR to PATH in $SHELL_PROFILE"
}

uninstall_ltbv() {
  check_platform
  local python_bin="$INSTALL_ROOT/.venv/bin/python"
  if [ ! -x "$python_bin" ]; then
    has python3 || fail "python3 is required to remove the installed hooks."
    python_bin="$(command -v python3)"
  fi
  backup_file "$CLAUDE_SETTINGS"
  backup_file "$CODEX_CONFIG"
  configure_hooks uninstall "$python_bin"
  remove_path_block
  rm -f "$BIN_DIR/ltbv"
  if [ -d "$INSTALL_ROOT" ]; then
    rm -rf "$INSTALL_ROOT"
  fi
  say "uninstalled"
  say "settings backups were kept beside the original files"
}

case "$ACTION" in
  install|update) ;;
  check)
    check_platform
    say "platform: $(uname -s) $(uname -m)"
    say "git: $(git --version)"
    has uv && say "uv: $(uv --version)" || say "uv: missing, installer will add it"
    case ":$PATH:" in *":$BIN_DIR:"*) say "PATH: ready" ;; *) say "PATH: installer will add $BIN_DIR" ;; esac
    exit 0
    ;;
  uninstall) uninstall_ltbv; exit 0 ;;
  *) fail "usage: install.sh [install|update|check|uninstall]" ;;
esac

check_platform

if ! has uv && [ "${LTBV_SKIP_SYNC:-0}" != "1" ]; then
  [ "${LTBV_SKIP_UV_INSTALL:-0}" != "1" ] || fail "uv is missing and automatic installation is disabled."
  say "uv not found, installing it from Astral"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  has uv || fail "uv installation finished but uv is still unavailable. Open a new terminal and retry."
fi

mkdir -p "$(dirname "$INSTALL_ROOT")"
if [ -d "$INSTALL_ROOT/.git" ]; then
  [ -z "$(git -C "$INSTALL_ROOT" status --porcelain)" ] || fail "$INSTALL_ROOT has local changes. Commit or remove them before updating."
  say "updating $INSTALL_ROOT to $REF"
  git -C "$INSTALL_ROOT" fetch origin "$REF"
  git -C "$INSTALL_ROOT" checkout "$REF"
  git -C "$INSTALL_ROOT" merge --ff-only "origin/$REF"
else
  [ ! -e "$INSTALL_ROOT" ] || fail "$INSTALL_ROOT exists but is not an ltbv Git checkout. Move it aside and retry."
  say "cloning ltbv into $INSTALL_ROOT"
  git clone --branch "$REF" "$REPO_URL" "$INSTALL_ROOT"
fi

if [ "${LTBV_SKIP_SYNC:-0}" = "1" ]; then
  has python3 || fail "python3 is required when dependency sync is skipped."
  PYTHON_BIN="$(command -v python3)"
  say "dependency sync skipped"
else
  say "installing locked Python dependencies"
  uv sync --project "$INSTALL_ROOT" --locked
  PYTHON_BIN="$INSTALL_ROOT/.venv/bin/python"
  [ -x "$PYTHON_BIN" ] || fail "Python environment was not created. Run: uv sync --project $INSTALL_ROOT --locked"
fi

mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/ltbv" <<'EOF'
#!/bin/sh
set -u
ROOT="${LTBV_HOME:-$HOME/.local/share/ltbv}"

case "${1:-}" in
  open|controller)
    "$ROOT/voice" wake >/dev/null 2>&1
    open http://127.0.0.1:7333/
    ;;
  doctor)
    exec "$ROOT/voice" doctor
    ;;
  update)
    exec "$ROOT/install.sh" update
    ;;
  uninstall)
    exec "$ROOT/install.sh" uninstall
    ;;
  *)
    exec "$ROOT/voice" "$@"
    ;;
esac
EOF
chmod +x "$BIN_DIR/ltbv"

backup_file "$CLAUDE_SETTINGS"
backup_file "$CODEX_CONFIG"
configure_hooks install "$PYTHON_BIN"
install_path_block

say "verifying installation"
grep -q "# BEGIN LTBV" "$CODEX_CONFIG" || fail "Codex hook verification failed. Restore the latest backup and retry."
grep -q "hook.py" "$CLAUDE_SETTINGS" || fail "Claude hook verification failed. Restore the latest backup and retry."
[ -x "$BIN_DIR/ltbv" ] || fail "command installation failed."

say "installed"
say "open a new terminal, then run: ltbv doctor"
say "open the controller with: ltbv open"
say "update later with: ltbv update"
say "uninstall with: ltbv uninstall"
say "optional Browser/YouTube adapter: $INSTALL_ROOT/browser-extension"
