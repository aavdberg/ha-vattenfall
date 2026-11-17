const messages = {
  paired: "Log in, choose the intended account on Contract, then click Transfer this selected account in that portal tab.",
  armed: "Reading the selected account's contracts request.",
  received: "Session received. Return to Home Assistant and submit the setup.",
  disconnected: "Disconnected. Cancel the HA setup to discard any already transferred session.",
  expired: "Pairing expired. Reopen browser setup in HA and connect again.",
  error: "Could not pair or transfer. Use HTTPS (or localhost), keep the HA browser setup open, and reconnect. If transfer already started, cancel that HA setup before retrying.",
};
async function render() {
  const {status} = await chrome.storage.session.get("status");
  document.getElementById("status").textContent =
    (messages[status?.state] ?? "Not connected.") + (status?.origin ? ` HA: ${status.origin}` : "");
}
for (const type of ["connect", "transfer", "disconnect"]) {
  document.getElementById(type).addEventListener("click", async () => {
    try {
      await chrome.runtime.sendMessage({type});
      await render();
    } catch {
      document.getElementById("status").textContent = "Extension connection failed. Reopen this popup and retry.";
    }
  });
}
chrome.storage.onChanged.addListener(() => { void render(); });
void render();
