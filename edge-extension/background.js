import {PORTAL_URL, CONTRACTS_URL, allowedHaUrl, sessionHeaders, connectInHa, deliverInHa} from "./bridge.js";

export function createCompanion(api, now = Date.now) {
  let busy = false;
  let generation = 0;
  const status = async (state, origin = "") => {
    await api.storage.session.set({status: {state, origin}});
    await api.action.setBadgeText({text: ["paired", "armed"].includes(state) ? "ON" : state === "received" ? "OK" : state === "error" ? "!" : ""});
  };
  const clear = async () => {
    await api.storage.session.remove("pairing");
    await api.alarms.clear("pair-expiry");
  };
  const fail = async () => {
    await clear();
    await status("error");
  };
  return {
    async connect() {
      if (busy) throw new Error("transfer_in_progress");
      generation++;
      await clear();
      const [tab] = await api.tabs.query({active: true, currentWindow: true});
      if (!tab?.id || !tab.url || !allowedHaUrl(tab.url)) throw new Error("unsafe_ha");
      const origin = new URL(tab.url).origin;
      const [execution] = await api.scripting.executeScript({
        target: {tabId: tab.id}, world: "MAIN", func: connectInHa, args: [origin],
      });
      if (!execution?.result?.flowId || !execution.result.nonce) throw new Error("open_browser_setup");
      const portal = await api.tabs.create({url: "about:blank", active: true});
      const pairing = {haTabId: tab.id, origin, ...execution.result, portalTabId: portal.id, expiresAt: now() + 600000, armed: false};
      await api.storage.session.set({pairing});
      await api.alarms.create("pair-expiry", {when: pairing.expiresAt});
      await status("paired", origin);
      await api.tabs.update(portal.id, {url: PORTAL_URL});
    },
    async transfer() {
      const {pairing} = await api.storage.session.get("pairing");
      if (!pairing || pairing.expiresAt <= now()) throw new Error("expired_setup");
      const [tab] = await api.tabs.query({active: true, currentWindow: true});
      if (tab?.id !== pairing.portalTabId || !tab.url ||
          new URL(tab.url).origin + new URL(tab.url).pathname !== PORTAL_URL) {
        throw new Error("choose_portal_account_first");
      }
      await api.storage.session.set({pairing: {...pairing, armed: true}});
      await status("armed", pairing.origin);
      await api.tabs.update(pairing.portalTabId, {url: PORTAL_URL});
    },
    async capture(details) {
      if (busy) return;
      let claimed = false;
      const attempt = generation;
      try {
        const {pairing} = await api.storage.session.get("pairing");
        if (!pairing) return;
        if (pairing.expiresAt <= now()) {
          await clear();
          await status("expired");
          return;
        }
        if (!pairing.armed || details.tabId !== pairing.portalTabId) return;
        const headers = sessionHeaders(details);
        if (!headers) return;
        if (busy || attempt !== generation) return;
        busy = true;
        claimed = true;
        await clear();
        if (attempt !== generation) return;
        const [execution] = await api.scripting.executeScript({
          target: {tabId: pairing.haTabId}, world: "MAIN", func: deliverInHa,
          args: [pairing.origin, pairing.flowId, pairing.nonce, headers],
        });
        if (attempt !== generation) return;
        if (execution?.result !== true) throw new Error("transfer_failed");
        await status("received", pairing.origin);
      } catch {
        await fail();
      } finally {
        if (claimed) busy = false;
      }
    },
    async disconnect() {
      generation++;
      await clear();
      await status("disconnected");
    },
    async expire() {
      const {pairing} = await api.storage.session.get("pairing");
      if (pairing && pairing.expiresAt <= now()) {
        await clear();
        await status("expired");
      }
    },
    fail,
  };
}

if (typeof chrome !== "undefined" && chrome.runtime?.id) {
  const companion = createCompanion(chrome);
  chrome.runtime.onStartup.addListener(() => { void companion.disconnect(); });
  chrome.webRequest.onBeforeSendHeaders.addListener(
    (details) => { void companion.capture(details); },
    {urls: [CONTRACTS_URL + "*"], types: ["xmlhttprequest"]},
    ["requestHeaders", "extraHeaders"],
  );
  chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === "pair-expiry") void companion.expire();
  });
  chrome.tabs.onRemoved.addListener(async (tabId) => {
    const {pairing} = await chrome.storage.session.get("pairing");
    if (pairing && [pairing.haTabId, pairing.portalTabId].includes(tabId)) await companion.disconnect();
  });
  chrome.runtime.onMessage.addListener((message, sender, respond) => {
    if (sender.id !== chrome.runtime.id || sender.tab) return false;
    if (!["connect", "transfer", "disconnect"].includes(message?.type)) return false;
    void (async () => {
      try {
        await companion[message.type]();
        respond({ok: true});
      } catch {
        await companion.fail();
        respond({ok: false});
      }
    })();
    return true;
  });
}
