export const PORTAL_URL = "https://www.vattenfall.nl/service/mijn-vattenfall/vf/direct-regelen/contract";
export const CONTRACTS_URL = "https://api.vattenfall.nl/api/mvf/contracts/v1/contracts";

export function allowedHaUrl(value) {
  const url = new URL(value);
  return !url.username && !url.password && (url.protocol === "https:" ||
    (url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)));
}

export function sessionHeaders(details) {
  if (details.method !== "GET" || details.initiator !== "https://www.vattenfall.nl") return null;
  const url = new URL(details.url);
  if (url.origin + url.pathname !== CONTRACTS_URL || url.username || url.password) return null;
  const names = {authorization: "access_token", "ocp-apim-subscription-key": "subscription_key", "vf-context": "vf_context"};
  const result = {};
  for (const header of details.requestHeaders ?? []) {
    const key = Object.hasOwn(names, header.name.toLowerCase()) ? names[header.name.toLowerCase()] : null;
    if (!key) continue;
    if (key in result || typeof header.value !== "string" ||
        !header.value.trim() || header.value.length > 32768 || /[^\x20-\x7e]/.test(header.value)) {
      throw new Error("invalid_headers");
    }
    result[key] = header.value;
  }
  return Object.keys(result).length === 3 ? result : null;
}

// Self-contained functions run in the chosen HA tab's MAIN world.
// Authentication stays inside HA; no HA token leaves that page.
export async function connectInHa(origin) {
  if (location.origin !== origin) throw new Error("wrong_origin");
  const hass = document.querySelector("home-assistant")?.hass;
  if (!hass?.callApi) throw new Error("not_home_assistant");
  const codes = new Set();
  const scan = (root) => {
    for (const element of root.querySelectorAll("*")) {
      if (element.tagName === "CODE" && element.getClientRects().length &&
          /^VFEDGE\.[A-Za-z0-9_-]{16,64}\.[A-Za-z0-9_-]{43}$/.test(element.textContent.trim())) {
        codes.add(element.textContent.trim());
      }
      if (element.shadowRoot) scan(element.shadowRoot);
    }
  };
  scan(document);
  if (codes.size !== 1) throw new Error("open_browser_setup");
  const code = [...codes][0];
  const [, flowId, nonce] = code.split(".");
  const result = await hass.callApi("GET", `config/config_entries/flow/${flowId}`);
  if (result.handler !== "vattenfall" || result.type !== "form" ||
      result.step_id !== "browser" || result.description_placeholders?.pairing_code !== code) {
    throw new Error("open_browser_setup");
  }
  return {flowId, nonce};
}

export async function deliverInHa(origin, flowId, nonce, headers) {
  if (location.origin !== origin) throw new Error("wrong_origin");
  const hass = document.querySelector("home-assistant")?.hass;
  if (!hass?.callApi) throw new Error("not_home_assistant");
  const path = `config/config_entries/flow/${flowId}`;
  const current = await hass.callApi("GET", path);
  if (current.handler !== "vattenfall" || current.type !== "form" ||
      current.step_id !== "browser" ||
      current.description_placeholders?.pairing_code !== `VFEDGE.${flowId}.${nonce}`) {
    throw new Error("expired_setup");
  }
  const result = await hass.callApi("POST", path, {...headers, browser_session: nonce});
  // Return a receipt only, never the API response or credentials.
  return result.type === "form" && result.step_id === "browser_confirm";
}
