(() => {
  "use strict";
  const panel = document.getElementById("vpn-panel");
  if (!panel) return;
  const $ = (id) => document.getElementById(id);
  const feedback = $("vpn-feedback");
  const list = $("vpn-profiles");
  const testResults = $("vpn-test-results");
  const progress = $("vpn-test-progress");
  let busy = false;
  let timer = null;

  function message(text, problem = false) {
    feedback.textContent = text;
    feedback.classList.toggle("vpn-error", problem);
  }

  async function api(path, options = {}) {
    const headers = {"X-Requested-With": "XMLHttpRequest", ...(options.headers || {})};
    const response = await fetch("/api/vpn" + path, {
      method: options.method || "GET",
      headers,
      body: options.body,
      cache: "no-store",
    });
    let data;
    try {
      data = await response.json();
    } catch (_err) {
      throw Error("Invalid response from VPN service.");
    }
    if (!response.ok) {
      throw Error(data.error || "VPN operation failed.");
    }
    return data;
  }

  function element(tag, text, className) {
    const n = document.createElement(tag);
    if (text !== undefined) n.textContent = text;
    if (className) n.className = className;
    return n;
  }

  function setBusy(value) {
    busy = value;
    for (const b of panel.querySelectorAll("button")) {
      b.disabled = value;
    }
    $("vpn-upload-input").disabled = value;
    panel.classList.toggle("vpn-working", value);
  }

  function showTest(data) {
    const test = data.test || {};
    testResults.replaceChildren();
    for (const entry of test.results || []) {
      const row = element("div", undefined, "vpn-test-row");
      row.append(element("span", entry.ok ? "PASS" : "FAIL", entry.ok ? "vpn-pass" : "vpn-fail"));
      const desc = entry.country + " → observed " +
        (entry.country_code || "unknown") + " · " +
        (entry.ip || "No verified IP") + " · " + (entry.message || "");
      row.append(element("span", desc));
      testResults.append(row);
    }
    if (test.running) {
      progress.textContent = test.message || "Switching VPN endpoints…";
      if (!timer) timer = window.setInterval(refresh, 1800);
    } else {
      progress.textContent = test.done && (test.results || []).length
        ? "Test complete: " + test.results.filter((x) => x.ok).length +
          "/" + test.results.length + " profiles verified. " + (test.message || "")
        : "No rotation test in progress.";
      if (timer) window.clearInterval(timer);
      timer = null;
      if (busy) setBusy(false);
    }
  }

  function render(data) {
    // Update test/busy state BEFORE deriving buttons' disabled states.
    showTest(data);
    const profiles = data.profiles || [];
    $("vpn-count").textContent = profiles.length + " locations";
    $("vpn-connection").textContent = data.active
      ? "ACTIVE · " + (profiles.find((p) => p.id === data.active)?.country || "Unknown")
      : "DIRECT CONNECTION · VPN OFF";
    $("vpn-connection").classList.toggle("vpn-disconnected", !data.active);
    list.replaceChildren();
    for (const item of profiles) {
      const row = element("div", undefined, "vpn-profile");
      const description = element("div", undefined, "vpn-profile-description");
      description.append(element("strong", item.country));
      description.append(element("small", item.filename));
      row.append(description);
      if (data.active === item.id) row.append(element("span", "CONNECTED", "vpn-active-tag"));
      const actions = element("div", undefined, "vpn-row-actions");
      if (item.id !== data.active) {
        const connect = element("button", "Connect", "vpn-secondary");
        connect.type = "button";
        connect.disabled = busy;
        connect.addEventListener("click", () => act("/activate/" + item.id,
          "Verifying " + item.country + "…"));
        actions.append(connect);
        const remove = element("button", "Remove", "vpn-danger");
        remove.type = "button";
        remove.disabled = busy;
        remove.addEventListener("click", async () => {
          if (!window.confirm("Remove WireGuard profile " + item.filename + "?")) return;
          await act("/delete/" + item.id, "Removing profile…");
        });
        actions.append(remove);
      }
      row.append(actions);
      list.append(row);
    }
    if (!profiles.length) {
      list.append(element("p",
        "No WireGuard profiles uploaded. Add at least two countries to test rotation.",
        "vpn-muted"));
    }
    $("vpn-test-button").disabled = busy || profiles.length < 1;
    $("vpn-next-button").disabled = busy || profiles.length < 2;
  }

  async function refresh() {
    try {
      const data = await api("/profiles");
      render(data);
    } catch (err) {
      if (timer) {
        window.clearInterval(timer);
        timer = null;
        setBusy(false);
      }
      message(err.message, true);
    }
  }

  async function act(path, description) {
    if (busy) return;
    setBusy(true);
    message(description);
    try {
      const data = await api(path, {method: "POST", body: "{}",
        headers: {"Content-Type": "application/json"}});
      if (path === "/rotate" && data.changed_ip === false) {
        message("The profile switched, but the public IP did not change. " +
          "This is not a verified rotation.", true);
      } else if (path === "/rotate" || path.startsWith("/activate/")) {
        message("Verified VPN exit: " + (data.ip || "?") +
          " · " + (data.country || "Unknown") + ".");
      } else {
        message("Profile updated.");
      }
      await refresh();
    } catch (err) {
      message(err.message, true);
    } finally {
      setBusy(false);
      await refresh();
    }
  }

  $("vpn-refresh").addEventListener("click", refresh);
  $("vpn-next-button").addEventListener("click", () =>
    act("/rotate", "Changing to the next WireGuard country…"));

  $("vpn-upload").addEventListener("click", async () => {
    const input = $("vpn-upload-input");
    if (!input.files || !input.files.length) {
      message("Select one or more Surfshark WireGuard .conf files.", true);
      return;
    }
    if (busy) return;
    const body = new FormData();
    for (const file of input.files) {
      if (!file.name.endsWith(".conf")) {
        message("Only .conf files can be uploaded.", true);
        return;
      }
      body.append("configs", file);
    }
    setBusy(true);
    message("Validating WireGuard profiles…");
    try {
      const response = await api("/profiles", {method: "POST", body});
      const count = (response.uploaded || []).length;
      message(count + " WireGuard " + (count === 1 ? "profile" : "profiles") + " uploaded. " +
        (response.warning ? "Connection test failed: " + response.warning
          : response.connected ? "First location connected: " +
            (response.connected.country || "Unknown") + " · " + response.connected.ip
            : "Existing VPN connection retained."), Boolean(response.warning));
      input.value = "";
    } catch (err) {
      message(err.message, true);
    } finally {
      setBusy(false);
      await refresh();
    }
  });

  $("vpn-test-button").addEventListener("click", async () => {
    if (busy) return;
    if (!window.confirm("Temporarily switch through each VPN country to verify exit IPs? Downloads will be blocked during the test, and the original connection will be restored.")) return;
    setBusy(true);
    message("Beginning VPN rotation test…");
    try {
      const result = await api("/test", {method: "POST", body: "{}",
        headers: {"Content-Type": "application/json"}});
      if (!result.started) throw Error("Gateway did not start the test.");
      await refresh();
    } catch (err) {
      setBusy(false);
      message(err.message, true);
    }
  });
  panel.addEventListener("toggle", () => { if (panel.open) refresh(); });
  refresh();
})();
