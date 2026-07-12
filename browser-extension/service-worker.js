const DAEMON_URL = "http://127.0.0.1:7333/browser/duck";

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.type !== "duck-state") return false;
  fetch(DAEMON_URL)
    .then((response) => response.json())
    .then(sendResponse)
    .catch(() => sendResponse({ ok: false, active: false }));
  return true;
});
