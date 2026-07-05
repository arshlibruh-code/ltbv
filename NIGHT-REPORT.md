# Night Report · 2026-07-06

Codex ran out of usage mid-run (after committing the scoped-stop work and the
packet update, with Phase 5 patched but uncommitted). Claude took the shift
from there.

## What landed

- `406109b` phase 5 speech refine pass: Codex's patch, verified by logic tests
  before committing. All 11 tasks present and correct. Threshold 400,
  token-level stripping, per-project pending with per-cwd generations, scoped
  stop, first-sentence condense fallback, hook retry, Notification event, wav
  leak fix, dead code sweep, log cap, richer /health.
- `35ef222` phase 6 controller plus night shift daemon layer: config.json with
  per-field validation and hot apply, controller.html served at GET / (Mixing
  Desk with draggable threshold over a live histogram, Strip Inspector, Voice
  Lab, Ledger, Personality editor), /voices + /audition + /recondense +
  /log/tail endpoints, quiet hours, notification queue-jump, transcript
  fallback, error journal, temperature/rate/volume knobs, git rev in /health.
- `e7fd795` smoke suite: 7 checks, exits nonzero on any failure.
- `962b59a` voicectl: `./voice status|stop|mute|unmute|say`.
- CLAUDE.md rewritten to post-run reality. Codex config gained a
  `[[hooks.Notification]]` block (TOML validated).

## Verified

- Both smoke runs ended SMOKE PASS (health, hook to speech, scoped stop
  ignored, two-project overlap, kill switch, config roundtrip, config armor).
- Notification end-to-end: fake Notification payload spoke "smokeA needs your
  input" in the notification voice (eve).
- Multi-voice: auditioned eve alongside alba, voice states cache per name.
- Config armor rejects garbage types, out-of-range values, unknown voices,
  and invalid quiet hours ("25:99" caught after a validator fix).
- Transcript fallback parses the last assistant entry from a JSONL transcript.

## Found and fixed along the way

- My first quiet-hours validator accepted "25:99" (regex without range
  check). Caught by the logic tests, fixed.
- smoke.sh used second-granularity timestamps and one check bled into the
  next; switched to sub-second timestamps.

## Skipped or dormant, with reasons

- Voice cloning (drop-a-wav) is rendered but dormant: needs you to accept
  terms at huggingface.co/kyutai/pocket-tts and run `hf auth login`.
- Codex Notification could not be live-verified tonight (no interactive
  session); the event name exists in the codex 0.142.5 binary and the hook is
  registered. Verify with a real permission prompt.
- Claude Notification on a REAL permission prompt also pending a live session;
  the synthetic path works end to end.
- Temperature is a load-time parameter in Pocket, not per-generation; the
  knob works by dropping the model for a lazy ~1s reload. Noted in CLAUDE.md.

## Surprises in the logs

- Nothing alarming. `.voice.log` shows the expected config_error entries from
  armor tests and clean speak/stop_ignored/quiet_skip traffic. Error journal
  captured zero real errors overnight.
- Honesty note: the final smoke run played its four short test phrases at
  full volume (I restored volume before smoking instead of after).

## Morning checklist

1. `./voice unmute` (kill switch is ON right now, intentionally).
2. `claude -p 'Reply with exactly: voice test'` then the codex equivalent.
3. Open http://127.0.0.1:7333/ and drag the threshold line.
4. Trigger a real permission prompt in each CLI to verify Notification live.
5. Optional: the HF dance to wake the clone zone.
6. Codex speaks as "michael", notifications as "eve" by default; change in
   the Voice Lab if they annoy you.
