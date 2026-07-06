#!/bin/bash
# smoke.sh: end-to-end confidence check for let-there-be-voice.
# Exits 0 only if every check passes. Speaks briefly at configured volume.
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
PY="$ROOT/.venv/bin/python"
LOG="$ROOT/.voice.log"
KILL="$ROOT/.voice-disabled"
BASE="http://127.0.0.1:7333"
FAIL=0

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
cleanup() { [ "$HAD_KILL" = 1 ] && mv -f "$KILL.smoke" "$KILL" 2>/dev/null; }
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

# 2. hook end-to-end: fake Stop payload must produce a speak event
SINCE=$("$PY" -c "import time; print(time.time())")
printf '%s' '{"hook_event_name":"Stop","last_assistant_message":"Voice system online.","cwd":"/tmp/smokeA"}' | "$PY" "$ROOT/hook.py"
if wait_for_event speak "$SINCE" 60; then pass "hook to speech"; else fail "hook to speech"; fi

# 3. scoped stop: stop from unrelated project must be ignored
SINCE=$("$PY" -c "import time; print(time.time())")
curl -sf -m 2 -X POST "$BASE/speak" -d '{"text":"Testing scoped interrupt.","cwd":"/tmp/smokeA"}' >/dev/null
sleep 0.3
curl -sf -m 2 -X POST "$BASE/stop" -d '{"cwd":"/tmp/smokeUNRELATED"}' >/dev/null
if wait_for_event stop_ignored "$SINCE" 5; then pass "scoped stop ignored"; else fail "scoped stop ignored"; fi
curl -sf -m 2 -X POST "$BASE/stop" -d '{"cwd":"/tmp/smokeA"}' >/dev/null

# 4. overlap: two projects enqueued together must both speak
SINCE=$("$PY" -c "import time; print(time.time())")
curl -sf -m 2 -X POST "$BASE/speak" -d '{"text":"Project A channel ready.","cwd":"/tmp/smokeA"}' >/dev/null
curl -sf -m 2 -X POST "$BASE/speak" -d '{"text":"Project B channel ready.","cwd":"/tmp/smokeB"}' >/dev/null
OK=0
for _ in $(seq 1 90); do
  [ "$(log_count speak "$SINCE")" -ge 2 ] && OK=1 && break
  sleep 1
done
if [ "$OK" = 1 ]; then pass "two-project overlap"; else fail "two-project overlap"; fi

# 5. kill switch blocks the hook
touch "$KILL"
SINCE=$("$PY" -c "import time; print(time.time())")
printf '%s' '{"hook_event_name":"Stop","last_assistant_message":"Muted path check.","cwd":"/tmp/smokeA"}' | "$PY" "$ROOT/hook.py"
sleep 2
if [ "$(log_count speak "$SINCE")" -eq 0 ]; then pass "kill switch blocks"; else fail "kill switch blocks"; fi
rm -f "$KILL"

# 5b. replay: the last clip in the log must replay
CLIP=$("$PY" - <<'EOF'
import json
clip=""
try:
    for line in open(".voice.log"):
        try: d=json.loads(line)
        except Exception: continue
        if d.get("event")=="speak" and d.get("clip"): clip=d["clip"]
except FileNotFoundError: pass
print(clip)
EOF
)
if [ -n "$CLIP" ] && curl -sf -m 2 -X POST "$BASE/replay" -d "{\"clip\":\"$CLIP\"}" | grep -q '"ok": true'; then pass "replay clip"; else fail "replay clip"; fi

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
if [ "$ERRS" = "1" ] && curl -sf -m 2 "$BASE/health" >/dev/null; then pass "config armor"; else fail "config armor"; fi

if [ "$FAIL" = 0 ]; then echo "SMOKE PASS"; else echo "SMOKE FAIL"; fi
exit "$FAIL"
