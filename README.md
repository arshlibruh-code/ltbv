<div align="center">
  <h1>ltbv</h1>
  <p><strong>A local voice layer for coding agents.</strong></p>
  <p>
    <code>macOS</code>
    <code>Claude Code</code>
    <code>Codex</code>
    <code>localhost:7333</code>
    <code>Pocket TTS</code>
  </p>
  <p>
    <a href="#install"><kbd>INSTALL</kbd></a>
    <a href="#usage"><kbd>USAGE</kbd></a>
    <a href="#privacy"><kbd>PRIVACY</kbd></a>
    <a href="field-guide.html"><kbd>FIELD GUIDE</kbd></a>
    <a href="build-packet.html"><kbd>BUILD PACKET</kbd></a>
  </p>
</div>

```
┌──────────────────────────────────────────────────────────────┐
│ agent finishes -> hook forwards -> daemon speaks -> you chill │
└──────────────────────────────────────────────────────────────┘
```

`ltbv` speaks Claude Code and Codex replies out loud after a turn finishes. It runs as a tiny localhost daemon, keeps the hook fast, summarizes long or table-heavy replies, queues speech by project, and lets you interrupt without killing the agent.

## What It Does

| Surface | Behavior |
|---|---|
| Agent replies | Speaks final Claude Code and Codex turns |
| Project lanes | Queues by `cwd`, so repos do not trample each other |
| **CHILL** | Stops only the current speech |
| **SHUT UP** | Blocks future speech with `.voice-disabled` |
| Summaries | Condenses long replies and tables through a configurable summarizer |
| TTS | Pocket by default, Kokoro available for shootouts |
| Ducking | Optional Spotify fade-down while TTS is playing |

## Requirements

- macOS
- Python 3.11
- `uv`
- `ffmpeg` or `ffplay` recommended for better playback speed control

## Install

```bash
git clone https://github.com/arshlibruh-code/ltbv.git
cd ltbv
uv sync
./voice status
```

The daemon starts automatically from the hook when needed. To start it manually:

```bash
.venv/bin/python daemon.py
```

Then open:

```bash
open http://127.0.0.1:7333/
```

## Hook Setup

Replace `$REPO` with the absolute path to this repo.

Claude Code:

```json
{
  "hooks": {
    "Stop": [{ "hooks": [{
      "type": "command",
      "command": "$REPO/.venv/bin/python $REPO/hook.py"
    }]}],
    "UserPromptSubmit": [{ "hooks": [{
      "type": "command",
      "command": "$REPO/.venv/bin/python $REPO/hook.py"
    }]}]
  }
}
```

Codex:

```toml
[[hooks.Stop]]

[[hooks.Stop.hooks]]
type = "command"
command = "$REPO/.venv/bin/python $REPO/hook.py"

[[hooks.UserPromptSubmit]]

[[hooks.UserPromptSubmit.hooks]]
type = "command"
command = "$REPO/.venv/bin/python $REPO/hook.py"
```

## Usage

```bash
./voice status
./voice chill
./voice shutup
./voice unshutup
./voice say "Voice system online."
```

In the controller:

| Control | Meaning |
|---|---|
| **CHILL** | Stop talking now |
| **SHUT UP** | Disable future speech until re-enabled |
| **Space** | Toggle SHUT UP |

## Privacy

TTS synthesis and playback run locally. Long replies and table-heavy replies may be sent to the configured summarizer. The current default provider is DeepSeek, and `condense_provider: "none"` disables external summarization.

Spotify ducking is opt-in. Browser ducking is not included in v1 because reliable per-tab volume needs a browser extension.

## Test

```bash
./smoke.sh
```

The smoke suite starts the daemon if needed, verifies hook-to-speech, scoped interrupts, two project lanes, SHUT UP, replay, clone guards, and config validation.

## Docs

- `field-guide.html`: what the system feels like to use.
- `build-packet.html`: implementation packet and engineering appendix.

## Troubleshooting

| Symptom | Check |
|---|---|
| No speech | `./voice status`, `.voice-disabled`, quiet hours, `.voice.log` |
| First reply after idle is slow | The model lazy-loads after daemon idle exit |
| Spotify does not duck | Enable Spotify ducking in the controller |
| Long replies are not summarized | Check summarizer token or set `condense_provider` |
| Port busy | Another daemon is already listening on `127.0.0.1:7333` |
