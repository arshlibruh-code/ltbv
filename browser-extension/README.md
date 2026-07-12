# ltbv browser duck

Optional Chromium extension for YouTube page-volume ducking.

1. Open `arc://extensions/` in Arc or `chrome://extensions/` in Chrome.
2. Enable Developer mode.
3. Choose Load unpacked.
4. Select this directory.

The extension only polls while a YouTube media element is playing. It asks the local daemon for duck state at `127.0.0.1:7333`, saves the page media volume, and restores it after speech.
