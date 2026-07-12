const mediaState = new Map();
let pollTimer = null;

const isMedia = (node) => node instanceof HTMLMediaElement;
const isPlaying = (media) => !media.paused && !media.ended && media.readyState > 2;
const closeEnough = (a, b) => Math.abs(a - b) <= 0.001;

function remember(media) {
  if (!mediaState.has(media)) {
    mediaState.set(media, {
      volume: media.volume,
      muted: media.muted,
      lastSet: null,
      userChanged: false,
    });
  } else {
    const state = mediaState.get(media);
    if (state.lastSet === null) {
      state.volume = media.volume;
      state.muted = media.muted;
    }
  }
  return mediaState.get(media);
}

function duck(media, target) {
  if (!isMedia(media)) return;
  const state = remember(media);
  if (!state.userChanged && !closeEnough(media.volume, target)) {
    media.volume = target;
    state.lastSet = target;
  } else if (!state.userChanged) {
    state.lastSet = target;
  }
}

function restore() {
  for (const [media, state] of mediaState) {
    if (!media.isConnected) {
      mediaState.delete(media);
      continue;
    }
    if (!state.userChanged && state.lastSet !== null && closeEnough(media.volume, state.lastSet)) {
      media.volume = state.volume;
      media.muted = state.muted;
    }
    state.volume = media.volume;
    state.muted = media.muted;
    state.lastSet = null;
    state.userChanged = false;
  }
}

function observeVolume(event) {
  const media = event.target;
  const state = mediaState.get(media);
  if (state && state.lastSet !== null && !closeEnough(media.volume, state.lastSet)) {
    state.userChanged = true;
  }
}

async function poll() {
  const playing = [...mediaState.keys()].filter(isPlaying);
  if (!playing.length) {
    restore();
    stopPolling();
    return;
  }
  try {
    const state = await chrome.runtime.sendMessage({ type: "duck-state" });
    if (state && state.ok && state.enabled && state.active) {
      for (const media of playing) duck(media, Number(state.target) || 0.25);
    } else {
      restore();
    }
  } catch {
    restore();
  }
}

function startPolling() {
  if (pollTimer !== null) return;
  pollTimer = setInterval(poll, 250);
  poll();
}

function stopPolling() {
  if (pollTimer === null) return;
  clearInterval(pollTimer);
  pollTimer = null;
}

document.addEventListener("play", (event) => {
  if (!isMedia(event.target)) return;
  remember(event.target);
  startPolling();
}, true);

document.addEventListener("pause", (event) => {
  if (isMedia(event.target)) poll();
}, true);

document.addEventListener("volumechange", observeVolume, true);

for (const media of document.querySelectorAll("video, audio")) {
  remember(media);
  if (isPlaying(media)) startPolling();
}
