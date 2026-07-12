![ltbv banner](assets/ltbv-banner.png)

# ltbv

*let there be voice*

A local voice layer for coding agents.

[📖 Field Guide](https://arshlibruh-code.github.io/ltbv/field-guide.html) · [📦 Build Packet](https://arshlibruh-code.github.io/ltbv/build-packet.html)

## Install

```bash
git clone https://github.com/arshlibruh-code/ltbv.git
cd ltbv
uv sync
```

Point the Claude Code or Codex hook at:

```bash
$REPO/.venv/bin/python $REPO/hook.py
```

The hook starts the daemon when needed.

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
