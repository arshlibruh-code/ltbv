<p align="center">
  <img src="assets/ltbv-banner.png" alt="ltbv seven segment technical banner" width="980">
</p>

<div align="center">
  <h1>ltbv</h1>
  <p><em>let there be voice</em></p>
  <p><strong>A local voice layer for coding agents.</strong></p>
  <p>
    <a href="https://arshlibruh-code.github.io/ltbv/field-guide.html" style="display:inline-block;padding:10px 22px;margin:4px;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;text-decoration:none;font-family:Inter,ui-sans-serif,system-ui;font-size:13px;font-weight:600">📖 Field Guide</a>
    <a href="https://arshlibruh-code.github.io/ltbv/build-packet.html" style="display:inline-block;padding:10px 22px;margin:4px;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;text-decoration:none;font-family:Inter,ui-sans-serif,system-ui;font-size:13px;font-weight:600">📦 Build Packet</a>
  </p>
</div>

<p align="center">
  <kbd>STOP HOOK</kbd>
  <code>-></code>
  <kbd>QUEUE BY CWD</kbd>
  <code>-></code>
  <kbd>LOCAL TTS</kbd>
  <code>-></code>
  <kbd>CHILL / SHUT UP</kbd>
</p>

<p align="center">
  Agent turns become project-aware speech.
</p>

## Install

```bash
git clone https://github.com/arshlibruh-code/ltbv.git
cd ltbv
uv sync
./voice status
```

Wire Claude Code or Codex to:

```bash
$REPO/.venv/bin/python $REPO/hook.py
```

Open the controller after the daemon starts:

```bash
open http://127.0.0.1:7333/
```

## Controls

| Control | Meaning |
|---|---|
| `CHILL` | Stop the current speech |
| `SHUT UP` | Disable future speech with `.voice-disabled` |
| `Space` | Toggle SHUT UP |
| `./voice say "text"` | Speak a manual line |

## Notes

TTS runs locally. Long and table-heavy replies can use the configured summarizer. Spotify ducking is opt-in. Browser ducking is future extension work.

Run the gate before changing behavior:

```bash
./smoke.sh
```
