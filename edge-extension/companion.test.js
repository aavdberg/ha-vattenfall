import test from "node:test";
import assert from "node:assert/strict";
import {createCompanion} from "./background.js";
import {CONTRACTS_URL, PORTAL_URL, allowedHaUrl, sessionHeaders, connectInHa, deliverInHa} from "./bridge.js";

const headers = {access_token: "Bearer synthetic-token", subscription_key: "synthetic-key", vf_context: "synthetic-context"};
const request = (extra = {}) => ({
  url: CONTRACTS_URL, method: "GET", initiator: "https://www.vattenfall.nl", tabId: 2,
  requestHeaders: [
    {name: "Authorization", value: headers.access_token},
    {name: "Ocp-Apim-Subscription-Key", value: headers.subscription_key},
    {name: "Vf-Context", value: headers.vf_context},
    {name: "Cookie", value: "synthetic-do-not-forward"},
  ], ...extra,
});
const flowId = "synthetic-flow-id-12345";
const nonce = "A".repeat(43);

function fixture() {
  const saved = {};
  const calls = [];
  const api = {
    storage: {session: {
      async set(value) { Object.assign(saved, structuredClone(value)); },
      async get(key) { return structuredClone({[key]: saved[key]}); },
      async remove(key) { delete saved[key]; },
    }},
    alarms: {async create() {}, async clear() {}},
    action: {async setBadgeText() {}},
    tabs: {
      async query() { return [{id: 1, url: "http://localhost:8124/config/integrations"}]; },
      async create() { return {id: 2}; },
      async update() {},
    },
    scripting: {async executeScript(options) {
      calls.push(options);
      return [{result: options.func === connectInHa ? {flowId, nonce} : true}];
    }},
  };
  const companion = createCompanion(api, () => 1000);
  return {api, saved, calls, companion, async pair() {
    await companion.connect();
    const original = api.tabs.query;
    api.tabs.query = async () => [{id: 2, url: PORTAL_URL}];
    try { await companion.transfer(); } finally { api.tabs.query = original; }
  }};
}

test("exact portal scope, allowlisted headers and HTTPS/loopback destination", () => {
  assert.deepEqual(sessionHeaders(request()), headers);
  for (const extra of [
    {method: "POST"}, {initiator: "https://evil.example"}, {initiator: undefined},
    {url: CONTRACTS_URL + "/account"}, {url: CONTRACTS_URL.replace("api.vattenfall.nl", "evil.example")},
    {requestHeaders: []},
  ]) assert.equal(sessionHeaders(request(extra)), null);
  assert.throws(() => sessionHeaders(request({requestHeaders: [...request().requestHeaders, {name: "AUTHORIZATION", value: "duplicate"}]})), /invalid_headers/);
  assert.throws(() => sessionHeaders(request({requestHeaders: [{name:"Authorization",value:"bad\r\nheader"}]})), /invalid_headers/);
  for (const url of ["https://ha.example", "http://localhost:8124", "http://127.0.0.1:8124", "http://[::1]:8124"]) assert.equal(allowedHaUrl(url), true);
  for (const url of ["http://ha.example", "http://192.0.2.1:8123", "https://user:pass@ha.example", "file:///tmp/index.html"]) assert.equal(allowedHaUrl(url), false);
});

test("one-use capture never persists credentials and survives worker restart", async () => {
  const {api, saved, calls, pair} = fixture();
  await pair();
  const restarted = createCompanion(api, () => 2000);
  await restarted.capture(request({tabId: 99}));
  assert.equal(calls.length, 1);
  await restarted.capture(request());
  assert.equal(saved.pairing, undefined);
  assert.equal(saved.status.state, "received");
  assert.deepEqual(calls[1].args[3], headers);
  assert.equal(calls[1].target.tabId, 1);
  assert.equal(calls[1].world, "MAIN");
  assert.ok(!JSON.stringify(saved).includes("synthetic-token"));
  await restarted.capture(request());
  assert.equal(calls.length, 2);
});

test("expiry and disconnect stop capture", async () => {
  const {api, saved, calls, companion, pair} = fixture();
  await pair();
  await createCompanion(api, () => 601000).expire();
  assert.equal(saved.status.state, "expired");
  await companion.capture(request());
  assert.equal(calls.length, 1);
  await pair();
  await companion.disconnect();
  await companion.capture(request());
  assert.equal(saved.status.state, "disconnected");
  assert.equal(calls.length, 2);
});

test("late capture is rejected even if the alarm has not run", async () => {
  const {api, saved, calls, pair} = fixture();
  await pair();
  await createCompanion(api, () => 601000).capture(request());
  assert.equal(saved.pairing, undefined);
  assert.equal(saved.status.state, "expired");
  assert.equal(calls.length, 1);
});

test("disconnect invalidates an overlapping capture before delivery", async () => {
  const {calls, companion, pair} = fixture();
  await pair();
  const pending = companion.capture(request());
  await companion.disconnect();
  await pending;
  assert.equal(calls.filter(call => call.func === deliverInHa).length, 0);
});

test("duplicate concurrent requests transfer once", async () => {
  const {calls, companion, pair} = fixture();
  await pair();
  await Promise.all([companion.capture(request()), companion.capture(request())]);
  assert.equal(calls.filter(call => call.func === deliverInHa).length, 1);
});

test("an overlapping unauthenticated request does not lose the valid transfer", async () => {
  const {saved, companion, pair} = fixture();
  await pair();
  await Promise.all([
    companion.capture(request({requestHeaders: []})),
    companion.capture(request()),
  ]);
  assert.equal(saved.status.state, "received");
});

test("delivery failures disarm and expose only a generic error", async () => {
  const {api, saved, companion, pair} = fixture();
  await pair();
  api.scripting.executeScript = async () => { throw new Error("synthetic-token"); };
  await companion.capture(request());
  assert.equal(saved.status.state, "error");
  assert.equal(saved.pairing, undefined);
  assert.ok(!JSON.stringify(saved).includes("synthetic-token"));
});

test("initial/default-account requests are ignored until explicit portal confirmation", async () => {
  const {api, saved, calls, companion} = fixture();
  await companion.connect();
  await companion.capture(request());
  assert.equal(calls.length, 1);
  assert.equal(saved.pairing.armed, false);
  await assert.rejects(companion.transfer(), /choose_portal_account_first/);
  api.tabs.query = async () => [{id: 2, url: PORTAL_URL}];
  await companion.transfer();
  await companion.capture(request());
  assert.equal(calls.length, 2);
  assert.equal(saved.status.state, "received");
});

test("MAIN-world bridge validates the visible marker and flow before submitting", async (t) => {
  const oldDocument = globalThis.document;
  const oldLocation = globalThis.location;
  t.after(() => { globalThis.document = oldDocument; globalThis.location = oldLocation; });
  let step = "browser";
  let posted = 0;
  const code = `VFEDGE.${flowId}.${nonce}`;
  let visible = true;
  globalThis.location = {origin: "http://localhost:8124"};
  const hass = {async callApi(method, path, payload) {
    assert.equal(path, `config/config_entries/flow/${flowId}`);
    if (method === "POST") {
      posted++;
      assert.deepEqual(payload, {...headers, browser_session: nonce});
      step = "browser_confirm";
    }
    return {type: "form", handler: "vattenfall", step_id: step, description_placeholders: {pairing_code: code}};
  }};
  globalThis.document = {
    querySelector() { return {hass}; },
    querySelectorAll() { return [{tagName:"CODE",textContent:code,getClientRects:()=>visible ? [{}] : []}]; },
  };
  await assert.rejects(connectInHa("https://evil.example"), /wrong_origin/);
  visible = false;
  await assert.rejects(connectInHa(location.origin), /open_browser_setup/);
  visible = true;
  assert.deepEqual(await connectInHa(location.origin), {flowId, nonce});
  await assert.rejects(deliverInHa("https://evil.example", flowId, nonce, headers), /wrong_origin/);
  await assert.rejects(deliverInHa(location.origin, flowId, "B".repeat(43), headers), /expired_setup/);
  assert.equal(posted, 0);
  assert.equal(await deliverInHa(location.origin, flowId, nonce, headers), true);
  assert.equal(posted, 1);
  await assert.rejects(deliverInHa(location.origin, flowId, nonce, headers), /expired_setup/);
  assert.equal(posted, 1);
});
