# let-there-be-voice

Repo-local notes for Claude and Codex.

## What this is
- Local voice layer for Claude Code and Codex CLI.
- Final assistant replies are spoken through a local daemon.
- Hooks auto-start the daemon when needed.

## Current files
- `daemon.py`: localhost voice server on `127.0.0.1:7333`.
- `hook.py`: hook entrypoint. Reads CLI hook JSON, forwards to the daemon, exits fast.
- `bench_tts.py`: Pocket TTS bench script.
- `build-packet.html`: implementation packet and source of truth.
- `voice-field-guide.html`: narrative guide for how the system behaves.

## Current behavior
- Claude Code and Codex both use `Stop` and `UserPromptSubmit` hooks.
- `UserPromptSubmit` posts `/stop`.
- `Stop` posts the final message to `/speak`.
- The daemon uses Pocket TTS.
- Long text gets condensed in the daemon.
- Playback uses `afplay`.
- The daemon exits after idle time.

## Kill switch
- Create `.voice-disabled` in the repo root to block wake-up and speech.
- Remove `.voice-disabled` to re-enable the hook.

## Test
- Check daemon:
  - `./.venv/bin/python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:7333/health", timeout=2).read().decode())'`
- Test hook directly:
  - `printf '%s' '{"hook_event_name":"Stop","last_assistant_message":"voice test"}' | ./.venv/bin/python hook.py`
- Real Claude test:
  - `claude -p 'Reply with exactly: voice test'`
- Real Codex test:
  - `codex --dangerously-bypass-hook-trust exec 'Reply with exactly: voice test'`

## Notes
- Keep changes narrow.
- Do not reintroduce a PTY wrapper.
- Do not put logic back into `hook.py` unless absolutely necessary.
- If something is silent, check the daemon, the hook config, and the kill switch first.
