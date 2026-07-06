<p align="center">
  <img src="assets/ltbv-banner.png" alt="ltbv seven segment technical banner" width="980">
</p>

<div align="center">
  <h1>ltbv</h1>
  <p><em>let there be voice</em></p>
  <p><strong>A local voice layer for coding agents.</strong></p>
  <p>
    <a href="https://arshlibruh-code.github.io/ltbv/field-guide.html"><kbd>FIELD GUIDE</kbd></a>
    <a href="https://arshlibruh-code.github.io/ltbv/build-packet.html"><kbd>BUILD PACKET</kbd></a>
    <a href="#install"><kbd>INSTALL</kbd></a>
    <a href="#controls"><kbd>CONTROLS</kbd></a>
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
