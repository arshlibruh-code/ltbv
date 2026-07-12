#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
SOURCE="$TMP/source"
HOME_DIR="$TMP/home"
INSTALL_ROOT="$HOME_DIR/.local/share/ltbv"
BIN_DIR="$HOME_DIR/.local/bin"
CLAUDE_SETTINGS="$HOME_DIR/.claude/settings.json"
CODEX_CONFIG="$HOME_DIR/.codex/config.toml"
PROFILE="$HOME_DIR/.zprofile"

git clone -q "$ROOT" "$SOURCE"
git -C "$SOURCE" switch -q -c fixture
cp "$ROOT/install.sh" "$SOURCE/install.sh"
git -C "$SOURCE" add install.sh
git -C "$SOURCE" -c user.name=ltbv-test -c user.email=test@localhost commit -qm "installer fixture"

mkdir -p "$(dirname "$CLAUDE_SETTINGS")" "$(dirname "$CODEX_CONFIG")"
printf '{"permissions":{"allow":["Bash(git status)"]}}\n' > "$CLAUDE_SETTINGS"
printf '[model]\nname = "local"\n' > "$CODEX_CONFIG"

run_installer() {
  HOME="$HOME_DIR" \
  LTBV_HOME="$INSTALL_ROOT" \
  LTBV_BIN_DIR="$BIN_DIR" \
  LTBV_CLAUDE_SETTINGS="$CLAUDE_SETTINGS" \
  LTBV_CODEX_CONFIG="$CODEX_CONFIG" \
  LTBV_SHELL_PROFILE="$PROFILE" \
  LTBV_REPO_URL="$SOURCE" \
  LTBV_REF=fixture \
  LTBV_SKIP_SYNC=1 \
  bash "$ROOT/install.sh" "$@"
}

run_installer check >/dev/null
run_installer install >/dev/null
run_installer install >/dev/null

[ -x "$BIN_DIR/ltbv" ]
[ "$(grep -c '# BEGIN LTBV' "$CODEX_CONFIG")" -eq 1 ]
[ "$(grep -c '# BEGIN LTBV PATH' "$PROFILE")" -eq 1 ]
grep -q 'Bash(git status)' "$CLAUDE_SETTINGS"
grep -q 'name = "local"' "$CODEX_CONFIG"

printf 'updated\n' > "$SOURCE/install-fixture-marker"
git -C "$SOURCE" add install-fixture-marker
git -C "$SOURCE" -c user.name=ltbv-test -c user.email=test@localhost commit -qm "update fixture"
HOME="$HOME_DIR" \
LTBV_HOME="$INSTALL_ROOT" \
LTBV_BIN_DIR="$BIN_DIR" \
LTBV_CLAUDE_SETTINGS="$CLAUDE_SETTINGS" \
LTBV_CODEX_CONFIG="$CODEX_CONFIG" \
LTBV_SHELL_PROFILE="$PROFILE" \
LTBV_REPO_URL="$SOURCE" \
LTBV_REF=fixture \
LTBV_SKIP_SYNC=1 \
"$BIN_DIR/ltbv" update >/dev/null
[ -f "$INSTALL_ROOT/install-fixture-marker" ]

HOME="$HOME_DIR" \
LTBV_HOME="$INSTALL_ROOT" \
LTBV_BIN_DIR="$BIN_DIR" \
LTBV_CLAUDE_SETTINGS="$CLAUDE_SETTINGS" \
LTBV_CODEX_CONFIG="$CODEX_CONFIG" \
LTBV_SHELL_PROFILE="$PROFILE" \
LTBV_REPO_URL="$SOURCE" \
LTBV_REF=fixture \
LTBV_SKIP_SYNC=1 \
bash "$INSTALL_ROOT/install.sh" uninstall >/dev/null

[ ! -e "$INSTALL_ROOT" ]
[ ! -e "$BIN_DIR/ltbv" ]
! grep -q '# BEGIN LTBV' "$CODEX_CONFIG"
! grep -q '# BEGIN LTBV PATH' "$PROFILE"
grep -q 'Bash(git status)' "$CLAUDE_SETTINGS"

echo "INSTALL PASS"
