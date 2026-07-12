#!/bin/bash
# smoke.sh: end-to-end confidence check for let-there-be-voice.
# Exits 0 only if every check passes. Speaks one random line, mutes internal checks, then replays it.
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
PY="$ROOT/.venv/bin/python"
LOG="$ROOT/.voice.log"
KILL="$ROOT/.voice-disabled"
BASE="http://127.0.0.1:7333"
FAIL=0
ORIG_VOLUME=""
FIRST_CLIP=""

say_check() { printf '%-34s %s\n' "$1" "$2"; }
pass() { say_check "$1" "ok"; }
fail() { say_check "$1" "FAIL${2:+ ($2)}"; FAIL=1; }

log_count() { # log_count <event> <since-epoch>
  "$PY" - "$1" "$2" <<'EOF'
import json, sys
event, since = sys.argv[1], float(sys.argv[2])
n = 0
try:
    for line in open(".voice.log"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("event") == event and d.get("ts", 0) >= since:
            n += 1
except FileNotFoundError:
    pass
print(n)
EOF
}

latest_clip_since() { # latest_clip_since <since-epoch>
  "$PY" - "$1" <<'EOF'
import json, sys
since = float(sys.argv[1])
clip = ""
try:
    for line in open(".voice.log"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("event") == "speak" and d.get("ts", 0) >= since and d.get("clip"):
            clip = d["clip"]
except FileNotFoundError:
    pass
print(clip)
EOF
}

wait_for_event() { # wait_for_event <event> <since> <timeout-s>
  local i=0
  while [ "$i" -lt "$3" ]; do
    [ "$(cd "$ROOT" && log_count "$1" "$2")" -gt 0 ] && return 0
    sleep 1; i=$((i+1))
  done
  return 1
}

# restore kill switch state on exit
HAD_KILL=0
[ -f "$KILL" ] && HAD_KILL=1 && mv "$KILL" "$KILL.smoke"
restore_volume() {
  [ -n "$ORIG_VOLUME" ] && curl -sf -m 2 -X POST "$BASE/config" -d "{\"volume\": $ORIG_VOLUME}" >/dev/null 2>&1
}
cleanup() {
  restore_volume
  [ "$HAD_KILL" = 1 ] && mv -f "$KILL.smoke" "$KILL" 2>/dev/null
}
trap cleanup EXIT

cd "$ROOT"

# 1. daemon up (start if down)
if ! curl -sf -m 2 "$BASE/health" >/dev/null 2>&1; then
  nohup "$PY" "$ROOT/daemon.py" >/dev/null 2>&1 &
  for _ in $(seq 1 20); do
    curl -sf -m 1 "$BASE/health" >/dev/null 2>&1 && break
    sleep 0.5
  done
fi
if curl -sf -m 2 "$BASE/health" | grep -q '"ok": true'; then pass "daemon health"; else fail "daemon health"; fi
ORIG_VOLUME=$(curl -sf -m 2 "$BASE/config" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["config"]["volume"])')
SMOKE_LINE=$("$PY" - <<'EOF'
import random
lines = [
    "Can you hear me? I'm wired into the voice layer now.",
    "I'm on the wire. You should be able to hear me now.",
    "Let there be voice is live. I can talk back now.",
    "Hook path is live. If you can hear this, the loop works.",
    "Can you hear me? If yes, the hook path works.",
    "Voice channel is armed. Let there be voice.",
    "Signal is clean. The local speech loop is holding.",
    "Testing the voice path. Tell me if this reaches you.",
    "I'm speaking through the hook now. The daemon caught me.",
    "Can you hear me? If yes, the hook path works. Let there be voice.",
]
print(random.choice(lines))
EOF
)

# 2. hook end-to-end: fake Stop payload must produce a speak event
SINCE=$("$PY" -c "import time; print(time.time())")
PAYLOAD=$("$PY" - "$SMOKE_LINE" <<'EOF'
import json, sys
print(json.dumps({
    "hook_event_name": "Stop",
    "last_assistant_message": sys.argv[1],
    "cwd": "/tmp/ltbv-smoke",
}))
EOF
)
printf '%s' "$PAYLOAD" | "$PY" "$ROOT/hook.py"
if wait_for_event speak "$SINCE" 60; then pass "hook speaks"; else fail "hook speaks"; fi
FIRST_CLIP=$(latest_clip_since "$SINCE")
curl -sf -m 2 -X POST "$BASE/config" -d '{"volume": 0}' >/dev/null

# 3. scoped stop: stop from unrelated project must be ignored
SINCE=$("$PY" -c "import time; print(time.time())")
curl -sf -m 2 -X POST "$BASE/speak" -d '{"text":"smoke scoped stop","cwd":"/tmp/ltbv-smoke"}' >/dev/null
sleep 0.3
curl -sf -m 2 -X POST "$BASE/stop" -d '{"cwd":"/tmp/ltbv-other"}' >/dev/null
if wait_for_event stop_ignored "$SINCE" 5; then pass "other project ignored"; else fail "other project ignored"; fi
curl -sf -m 2 -X POST "$BASE/stop" -d '{"cwd":"/tmp/ltbv-smoke"}' >/dev/null

# 4. multi-project queue: two projects enqueued together must both speak
SINCE=$("$PY" -c "import time; print(time.time())")
curl -sf -m 2 -X POST "$BASE/speak" -d '{"text":"smoke queue one","cwd":"/tmp/ltbv-alpha"}' >/dev/null
curl -sf -m 2 -X POST "$BASE/speak" -d '{"text":"smoke queue two","cwd":"/tmp/ltbv-beta"}' >/dev/null
OK=0
for _ in $(seq 1 90); do
  [ "$(log_count speak "$SINCE")" -ge 2 ] && OK=1 && break
  sleep 1
done
if [ "$OK" = 1 ]; then pass "multi-project queue"; else fail "multi-project queue"; fi

# 5. SHUT UP blocks the hook
touch "$KILL"
SINCE=$("$PY" -c "import time; print(time.time())")
printf '%s' '{"hook_event_name":"Stop","last_assistant_message":"smoke muted path","cwd":"/tmp/ltbv-smoke"}' | "$PY" "$ROOT/hook.py"
sleep 2
if [ "$(log_count speak "$SINCE")" -eq 0 ]; then pass "shut up blocks"; else fail "shut up blocks"; fi
rm -f "$KILL"

# 5b. replay: the first smoke clip must replay after internal checks
restore_volume
if [ -n "$FIRST_CLIP" ] && curl -sf -m 2 -X POST "$BASE/replay" -d "{\"clip\":\"$FIRST_CLIP\"}" | grep -q '"ok": true'; then pass "replay first clip"; else fail "replay first clip"; fi

# 5c. clone rejects a bad name without touching the system
if curl -s -m 2 -X POST "$BASE/clone" -d '{"name":"BAD NAME","audio":""}' | grep -q '"ok": false'; then pass "clone guard"; else fail "clone guard"; fi

# 6. config roundtrip
REV=$(curl -sf -m 2 "$BASE/config" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["config"]["max_direct_chars"])')
curl -sf -m 2 -X POST "$BASE/config" -d '{"max_direct_chars": 401}' >/dev/null
GOT=$(curl -sf -m 2 "$BASE/config" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["config"]["max_direct_chars"])')
curl -sf -m 2 -X POST "$BASE/config" -d "{\"max_direct_chars\": $REV}" >/dev/null
if [ "$GOT" = "401" ]; then pass "config roundtrip"; else fail "config roundtrip" "$GOT"; fi

# 7. config armor: bad field rejected, system stays up
ERRS=$(curl -sf -m 2 -X POST "$BASE/config" -d '{"max_direct_chars": "garbage"}' | "$PY" -c 'import json,sys; print(len(json.load(sys.stdin)["errors"]))')
if [ "$ERRS" = "1" ] && curl -sf -m 2 "$BASE/health" >/dev/null; then pass "config rejects bad input"; else fail "config rejects bad input"; fi

# 8. condense provider validates as a closed config set
PROVIDER=$(curl -sf -m 2 "$BASE/config" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["config"]["condense_provider"])')
curl -sf -m 2 -X POST "$BASE/config" -d '{"condense_provider": "ollama"}' >/dev/null
GOT=$(curl -sf -m 2 "$BASE/config" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["config"]["condense_provider"])')
curl -sf -m 2 -X POST "$BASE/config" -d "{\"condense_provider\": \"$PROVIDER\"}" >/dev/null
BAD=$(curl -sf -m 2 -X POST "$BASE/config" -d '{"condense_provider": "ramble"}' | "$PY" -c 'import json,sys; print(len(json.load(sys.stdin)["errors"]))')
if [ "$GOT" = "ollama" ] && [ "$BAD" = "1" ]; then pass "condense provider config"; else fail "condense provider config"; fi

BROWSER_DUCK=$(curl -sf -m 2 "$BASE/config" | "$PY" -c 'import json,sys; print(str(json.load(sys.stdin)["config"]["browser_youtube_ducking_enabled"]).lower())')
curl -sf -m 2 -X POST "$BASE/config" -d '{"browser_youtube_ducking_enabled": true}' >/dev/null
BROWSER_GOT=$(curl -sf -m 2 "$BASE/config" | "$PY" -c 'import json,sys; print(str(json.load(sys.stdin)["config"]["browser_youtube_ducking_enabled"]).lower())')
curl -sf -m 2 -X POST "$BASE/config" -d "{\"browser_youtube_ducking_enabled\": $BROWSER_DUCK}" >/dev/null
if [ "$BROWSER_GOT" = "true" ]; then pass "browser duck config"; else fail "browser duck config"; fi

if [ "$FAIL" = 0 ]; then echo "SMOKE PASS"; else echo "SMOKE FAIL"; fi
exit "$FAIL"
