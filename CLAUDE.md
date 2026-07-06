# let-there-be-voice

Repo-local notes for Claude and Codex.

## What this is
- Local voice layer for Claude Code and Codex CLI.
- Final assistant replies are spoken through a local daemon.
- Hooks auto-start the daemon when needed.

## Current files
- `daemon.py`: localhost voice server on `127.0.0.1:7333`. Owns all behavior.
- `hook.py`: hook entrypoint. Reads CLI hook JSON, forwards to the daemon, exits fast.
- `controller.html`: tuning frontend, served by the daemon at `http://127.0.0.1:7333/`.
- `config.json`: runtime config, written by the controller, untracked. Delete it to reset to defaults.
- `smoke.sh`: end-to-end confidence suite. Run before committing daemon or hook changes.
- `voice`: CLI helper. `./voice status|stop|mute|unmute|say "text"`.
- `bench_tts.py`: Pocket TTS bench script.
- `build-packet.html`: implementation packet and source of truth.
- `field-guide.html`: narrative guide for how the system behaves.

## Current behavior
- Hooks in both CLIs: `Stop` speaks the final reply, `UserPromptSubmit` posts a cwd-scoped `/stop`, `Notification` speaks "<project> needs your input".
- Replies over `max_direct_chars` (default 400) get condensed via DeepSeek; on failure it speaks the first sentence with a short note.
- Markdown-table replies send the raw full response to DeepSeek first, so the voice gets a conversational table summary instead of row-by-row noise. If DeepSeek fails, a mostly-table reply becomes a spoken invite to look.
- Path, URL, and filename tokens are stripped from sentences; diff and traceback lines are dropped whole.
- Per-project pending queue: concurrent projects both get spoken, newest per project wins, notifications queue-jump.
- When 2+ projects spoke within `project_window_s`, replies get rotating "In X / From X" prefixes.
- TTS engines live behind the daemon registry. Pocket is the default. Kokoro-82M is installed as an optional shootout engine.
- Per-agent voices from the active engine catalog: `voices.claude`, `voices.codex`, `voices.notification` in config. Pocket uses 26 catalog voices; Kokoro exposes 54.
- Quiet hours (`quiet_hours` in config) accept speech but keep silent, logged as `quiet_skip`.
- Empty `last_assistant_message` falls back to parsing `transcript_path`.
- Playback via `afplay` honoring `rate` and `volume` config. The daemon exits after `idle_exit_s` idle.
- Errors land in `.voice.log` as `event: "error"`. The log self-caps around 500 lines.

## Endpoints
- `GET /` controller, `GET /health` (includes `git_rev`), `GET /config`, `GET /voices`, `GET /engines`, `GET /log/tail?limit=N`.
- `POST /speak`, `POST /stop`, `POST /config`, `POST /engine {name}`, `POST /bench {text, voice?}`, `POST /audition {voice}`, `POST /recondense`.
- Warm shootout on 2026-07-06, same sentence: Pocket RTF 0.160, TTFA 0.135s, synth 0.459s, duration 2.880s. Kokoro RTF 0.096, TTFA 0.420s, synth 0.420s, duration 4.375s.

## Kill switch
- Create `.voice-disabled` in the repo root to block wake-up and speech (`./voice mute`).
- Remove it to re-enable (`./voice unmute`).

## Test
- Full suite: `./smoke.sh` (temporarily lifts the kill switch, restores it after).
- Quick daemon check: `./voice status`
- Test hook directly:
  - `printf '%s' '{"hook_event_name":"Stop","last_assistant_message":"voice test"}' | ./.venv/bin/python hook.py`
- Real Claude test:
  - `claude -p 'Reply with exactly: voice test'`
- Real Codex test:
  - `codex --dangerously-bypass-hook-trust exec 'Reply with exactly: voice test'`

## Notes
- Keep changes narrow.
- After committing in a session (whether one commit or many), check whether `build-packet.html` and `field-guide.html` still match reality and update them if it moved. The packet tracks decisions and phases; the guide tracks behavior. Docs lag silently, so this is a deliberate step, not an afterthought.
- Do not reintroduce a PTY wrapper.
- Do not put logic back into `hook.py` unless absolutely necessary (agent detection, retry, and Notification text are the sanctioned exceptions).
- Temperature changes drop the loaded model; it lazily reloads with the new value on the next speak.
- Voice cloning (drop-a-wav) is dormant until the gated HF weights are set up: accept terms at huggingface.co/kyutai/pocket-tts, then `hf auth login`.
- If something is silent, check `./voice status`, the kill switch, quiet hours in config.json, and `.voice.log` first.
