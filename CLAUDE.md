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
- `voice`: CLI helper. `./voice wake|status|doctor|chill|repeat|slower|faster|brief|normal|shutup|unshutup|say "text"`.
- `install.sh`: relocatable macOS installer for the daemon and Claude/Codex hooks.
- `test_install.sh`: isolated-home install, rerun, update, and uninstall verification.
- `install.html`: public installation and lifecycle guide.
- `bench_tts.py`: Pocket TTS bench script.
- `build-packet.html`: implementation packet and source of truth.
- `field-guide.html`: narrative guide for how the system behaves.

## Current behavior
- Hooks in both CLIs: `Stop` speaks the final reply, `UserPromptSubmit` stops stale speech and starts an asynchronous Git snapshot, `Notification` speaks "<project> needs your input".
- Prompt text is redacted and held in memory only. Restart recovery persists compact request intent, turn identity, transcript location, and the Git baseline in `.turns.json`.
- Spoken claims are checked against actual local test commands and outcomes found in Claude/Codex transcripts. Diagnostics shows intent and verification metadata, never prompt or transcript bodies.
- Attributable Git diffs are translated into behavioral facts. `.timeline.json` keeps only compact outcomes for session-arc narration and `./voice recap`; it stores no prompt, reply, or source text.
- Condensing is local-only through Ollama, with `none` as the deterministic fallback.
- Replies over `max_direct_chars` (default 400) get condensed by the configured summarizer; on failure it speaks the first sentence with a short note.
- Markdown-table replies send the raw full response to the summarizer first, so the voice gets a conversational table summary instead of row-by-row noise. If summarization fails, a mostly-table reply becomes a spoken invite to look.
- Path, URL, and filename tokens are stripped from sentences; diff and traceback lines are dropped whole.
- Session-aware pending queue: concurrent projects and parallel turns inside one repo remain distinct, notifications queue-jump.
- When 2+ projects spoke within `project_window_s`, replies get rotating "In X / From X" prefixes.
- TTS engines live behind the daemon registry. Pocket is the default. Kokoro-82M is installed as an optional shootout engine.
- Per-agent voices from the active engine catalog: `voices.claude`, `voices.codex`, `voices.notification` in config. Pocket uses 26 catalog voices; Kokoro exposes 54.
- Quiet hours (`quiet_hours` in config) accept speech but keep silent, logged as `quiet_skip`.
- Empty `last_assistant_message` falls back to parsing `transcript_path`.
- Playback via `afplay` honoring `rate` and `volume` config. The daemon exits after `idle_exit_s` idle.
- Optional ducking targets Spotify and Browser/YouTube independently. Browser/YouTube is controlled by the companion Chromium extension through `GET /browser/duck`.
- Errors land in `.voice.log` as `event: "error"`. The log self-caps around 500 lines.
- Repo earcons, intent cues, build-result sonification, adaptive brevity, pronunciation dictionaries, and privacy redaction are local defaults. A repo can override speech using `.ltbv/pronounce.json`.
- When multiple projects finish together, the daemon can speak one radio bulletin naming each project. Backchannel commands are available through `/backchannel` and the CLI.

## Endpoints
- `GET /` controller, `GET /health` (includes live Git state), `GET /doctor`, `GET /config`, `GET /voices`, `GET /engines`, `GET /log/tail?limit=N`.
- `POST /speak`, `POST /stop`, `POST /turn/start`, `POST /backchannel {command}`, `POST /config`, `POST /engine {name}`, `POST /bench {text, voice?}`, `POST /audition {voice}`, `POST /recondense`.
- Warm shootout on 2026-07-06, same sentence: Pocket RTF 0.160, TTFA 0.135s, synth 0.459s, duration 2.880s. Kokoro RTF 0.096, TTFA 0.420s, synth 0.420s, duration 4.375s.

## Shut Up vs Chill
- SHUT UP creates `.voice-disabled` in the repo root and blocks wake-up plus future speech (`./voice shutup`, Space in the controller).
- CHILL calls `/stop` and only interrupts whatever is talking right now (`./voice chill`).
- Remove `.voice-disabled` to re-enable (`./voice unshutup`).

## Test
- Full suite: `./smoke.sh` (temporarily lifts the kill switch, restores it after).
- Isolated installer lifecycle: `./test_install.sh`
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
