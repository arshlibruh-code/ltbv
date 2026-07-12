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
| `./voice status` | Check daemon status |
| `./voice say "hello"` | Speak a line |
| `./voice chill` | Stop the current line |
| `./voice shutup` | Block future speech |
| `./voice unshutup` | Re-enable speech |
| `./smoke.sh` | Run full end-to-end verification |
