![ltbv banner](assets/ltbv-banner.png)

# ltbv

*let there be voice*

A local voice layer for coding agents.

[📖 Field Guide](https://arshlibruh-code.github.io/ltbv/field-guide.html) · [📦 Build Packet](https://arshlibruh-code.github.io/ltbv/build-packet.html)

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/arshlibruh-code/ltbv/main/install.sh | bash
~/.local/bin/ltbv open
```

The installer configures Claude Code and Codex hooks automatically, backs up existing settings, and installs into `~/.local/share/ltbv`.

Optional Browser/YouTube ducking: load `~/.local/share/ltbv/browser-extension` as an unpacked extension in Arc or Chrome.

For a health check:

```bash
~/.local/bin/ltbv doctor
```

Everything stays local. Ollama condenses long replies; `none` keeps the deterministic fallback. Prompt text is redacted and ephemeral, while only compact intent and turn evidence survive daemon restarts.

### Pronunciation

Create `.ltbv/pronounce.json` in a repo when names need help:

```json
{
  "WebGPU": "Web G P U",
  "ltbv": "let there be voice"
}
```

Speech also gets short repo and intent sounds, adaptive brevity, and obvious-secret redaction before condensation.

## Controller

Wake the daemon and open the controller:

```bash
./voice say "Let there be voice is live. I can talk back now."
open http://127.0.0.1:7333/
```

The daemon serves the controller automatically at `http://127.0.0.1:7333/`.

## CLI

| Command | Action |
|---|---|
| `./voice wake` | Start the daemon quietly |
| `./voice status` | Check daemon status |
| `./voice doctor` | Check the complete local loop |
| `./voice say "hello"` | Speak a line |
| `./voice chill` | Stop the current line |
| `./voice repeat` | Repeat the last line |
| `./voice slower` / `faster` | Replay at a different rate |
| `./voice brief` / `normal` | Toggle adaptive brevity |
| `./voice shutup` | Block future speech |
| `./voice unshutup` | Re-enable speech |
| `./smoke.sh` | Run full end-to-end verification |
