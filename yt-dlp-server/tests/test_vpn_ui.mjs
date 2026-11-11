// Run: node --test tests/test_vpn_ui.mjs
// This exercises the real browser script with a small DOM, no network or VPN.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const source = fs.readFileSync(
  new URL("../static/vpn-manager.js", import.meta.url), "utf8");

function node(tag = "div") {
  const listeners = {};
  const classes = new Set();
  const n = {
    tag, children: [], dataset: {}, textContent: "", className: "",
    disabled: false, hidden: false, value: "", files: [],
    classList: {
      add(x) { classes.add(x); },
      remove(x) { classes.delete(x); },
      contains(x) { return classes.has(x); },
      toggle(x, force) {
        const value = force === undefined ? !classes.has(x) : Boolean(force);
        if (value) classes.add(x); else classes.delete(x);
        return value;
      },
    },
    append(...parts) { this.children.push(...parts); },
    prepend(...parts) { this.children.unshift(...parts); },
    replaceChildren(...parts) { this.children = parts; },
    setAttribute() {},
    listeners,
    addEventListener(type, listener) { listeners[type] = listener; },
    click() { return listeners.click?.(); },
    querySelectorAll(selector) {
      const descendants = [];
      function visit(item) {
        for (const child of item.children) {
          const names = (child.className || "").split(" ");
          if (selector === "button" && child.tag === "button" ||
              selector === ".vpn-profile" && names.includes("vpn-profile")) {
            descendants.push(child);
          }
          visit(child);
        }
      }
      visit(this);
      return descendants;
    },
    querySelector(selector) {
      return this.querySelectorAll("button").find(child =>
        child.className.split(" ").includes("vpn-secondary")) || null;
    },
  };
  return n;
}
function fakeResponse(data, ok = true) {
  return {ok, status: ok ? 200 : 400, json: async () => data};
}
const flush = () => new Promise(resolve => setImmediate(resolve));

function harness() {
  const dom = new Map();
  const get = id => {
    if (!dom.has(id)) dom.set(id, node());
    return dom.get(id);
  };
  const panel = get("vpn-panel");
  for (const id of [
    "vpn-feedback", "vpn-public-keys", "vpn-profiles", "vpn-test-results", "vpn-test-progress",
    "vpn-key-setup", "vpn-private-key", "vpn-count", "vpn-connection",
    "vpn-upload-input", "vpn-test-button", "vpn-next-button", "vpn-direct-button",
    "vpn-upload",
  ]) panel.append(get(id));
  get("vpn-key-setup").hidden = true;
  let status = {
    profiles: [{id: "a".repeat(32), filename: "us-nyc.conf", country: "us-nyc",
      client_public_key: "A".repeat(43) + "="}],
    active: null, mode: "direct", kill_switch: false, tunnel_ready: false,
    test: {running: false, done: true, results: [], message: "Idle"},
  };
  let resolveConnect;
  let requests = [];
  async function fetch(url, args = {}) {
    requests.push({url, method: args.method || "GET"});
    if (url.endsWith("/profiles") && !args.body) {
      return fakeResponse(status);
    }
    if (url.includes("/activate/")) {
      return new Promise(resolve => {resolveConnect = resolve;});
    }
    if (url.endsWith("/direct") && args.body) {
      status = {...status, active: null, mode: "direct", kill_switch: false, tunnel_ready: false};
      return fakeResponse({ok: true, direct: true, mode: "direct"});
    }
    throw Error("Unexpected request: " + url);
  }
  const document = {getElementById: get, createElement: node};
  const timers=[];
  const context = {
    document, fetch, window: {
      confirm: () => true,
      setInterval: (callback) => {timers.push(callback);return 15}, clearInterval() {},
    },
    FormData: class { append() {} },
    console,
  };
  vm.runInNewContext(source, context, {timeout: 2000});
  const profile = () => get("vpn-profiles").children[0];
  const connect = () => profile().querySelector("button.vpn-secondary");
  return {
    get, requests, profile, connect,
    openPanel(){panel.open=true;panel.listeners.toggle?.()},
    setStatus(value) {status = value;},
    settleConnect(data, ok = false) {
      assert.equal(typeof resolveConnect, "function");
      resolveConnect(fakeResponse(data, ok));
    },
  };
}

test("Connect immediately shows an in-row state and stays locked during a status refresh", async () => {
  const ui = harness();
  await flush();
  assert.equal(ui.connect().textContent, "Connect");
  assert.equal(ui.connect().disabled, false);
  ui.connect().click();
  assert.equal(ui.connect().textContent, "Connecting…");
  assert.equal(ui.connect().disabled, true);
  assert.match(ui.profile().children.at(-1).textContent, /Contacting Surfshark/);
  // Simulate a background GET while POST is still unresolved.
  ui.openPanel();
  await flush();
  assert.equal(ui.connect().disabled, true);
  assert.equal(ui.connect().textContent, "Connecting…");
  ui.settleConnect({
    error: "WireGuard peer did not respond",
    phase: "handshake",
    diagnostics: {udp_packets_allowed: 3,
                  client_public_key: "A".repeat(43) + "="},
  });
  await flush();
  await flush();
  assert.equal(ui.connect().disabled, false);
  assert.equal(ui.connect().textContent, "Connect");
  const row = ui.profile();
  assert.ok(row.children.some(c =>
    c.className.includes("vpn-profile-result") &&
    c.textContent.includes("WireGuard peer did not respond")));
  assert.ok(row.children.some(c =>
    c.className.includes("vpn-profile-diagnostics")));
  assert.match(ui.get("vpn-feedback").textContent, /WireGuard peer did not respond/);
});

test("A successful Connect visibly reports the verified exit and remains in the row", async () => {
  const ui = harness();
  await flush();
  ui.connect().click();
  ui.setStatus({
    profiles: [{id: "a".repeat(32), filename: "us-nyc.conf", country: "us-nyc"}],
    active: "a".repeat(32), kill_switch: true, tunnel_ready: true,
    test: {running: false, done: true, results: []},
  });
  ui.settleConnect({ip: "198.51.100.1", country: "United States"}, true);
  await flush();
  await flush();
  assert.match(ui.get("vpn-connection").textContent, /WIREGUARD CONNECTED/);
  assert.ok(ui.profile().children.some(c =>
    c.className.includes("vpn-profile-result") &&
    c.textContent.includes("Verified VPN exit")));
});


test("The registered client public key appears in the normal VPN profile view", async () => {
  const ui = harness();
  await flush();
  const publicKeys = ui.get("vpn-public-keys");
  assert.ok(publicKeys.children.some(item =>
    item.textContent?.includes("All 1 countries use this same client key")));
  const field = publicKeys.children.find(item =>
    item.className === "vpn-public-key-input");
  assert.equal(field?.value, "A".repeat(43) + "=");
  assert.ok(ui.profile().children.some(item =>
    item.className === "vpn-profile-key"));
});

test("Direct is default and manual Disconnect VPN restores it", async () => {
  const ui = harness();
  await flush();
  assert.match(ui.get("vpn-connection").textContent, /DIRECT CONNECTION · DEFAULT/);
  assert.equal(ui.get("vpn-direct-button").disabled, true);
  ui.setStatus({
    profiles: [{id: "a".repeat(32), filename: "us-nyc.conf", country: "us-nyc"}],
    active: "a".repeat(32), mode: "vpn", kill_switch: true, tunnel_ready: true,
    test: {running: false, done: true, results: []},
  });
  ui.openPanel();
  await flush();
  assert.equal(ui.get("vpn-direct-button").disabled, false);
  ui.get("vpn-direct-button").click();
  await flush();
  await flush();
  assert.match(ui.get("vpn-connection").textContent, /DIRECT CONNECTION · DEFAULT/);
  assert.ok(ui.requests.some(r => r.url.endsWith("/direct") && r.method === "POST"));
});
