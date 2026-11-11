(() => {
  "use strict";
  const panel = document.getElementById("vpn-panel");
  if (!panel) return;
  const $ = (id) => document.getElementById(id);
  const feedback = $("vpn-feedback");
  const list = $("vpn-profiles");
  const publicKeysPanel = $("vpn-public-keys");
  const testResults = $("vpn-test-results");
  const progress = $("vpn-test-progress");
  const keySetup = $("vpn-key-setup");
  const keyInput = $("vpn-private-key");
  let busy = false;
  let timer = null;
  let testStarting = false;
  let testing = false;
  let pendingProfile = null;
  let lastProfileResult = null;

  function message(text, problem = false) {
    feedback.textContent = text;
    feedback.classList.toggle("vpn-error", problem);
  }

  async function api(path, options = {}) {
    const headers = {"X-Requested-With": "XMLHttpRequest", ...(options.headers || {})};
    const prefix = document.querySelector?.('meta[name="downloader-base"]')?.content || "";
    const response = await fetch(prefix + "/api/vpn" + path, {
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
      const problem = Error(data.error || "VPN operation failed.");
      problem.needsPrivateKey = Boolean(data.needs_private_key);
      problem.diagnostics = data.diagnostics || null;
      throw problem;
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
    keyInput.disabled = value;
    panel.classList.toggle("vpn-working", value);
  }

  function showProfileResult(row, result) {
    if (!result) return;
    const status = element("div", result.message,
      result.error ? "vpn-profile-result vpn-error" : "vpn-profile-result");
    status.setAttribute("role", "status");
    row.append(status);
    if (result.diagnostics) {
      const diagnosticRow = element("div", undefined, "vpn-profile-diagnostics");
      showHandshakeDiagnostics(diagnosticRow, result.diagnostics);
      row.append(diagnosticRow);
    }
  }

  function showHandshakeDiagnostics(parent, diagnostics) {
    if (!diagnostics || Object.keys(diagnostics).length === 0) return;
    const details = element("details", undefined, "vpn-diagnostics");
    const summary = element("summary", "Connection diagnostics (no private keys)");
    details.append(summary);

    const udp = diagnostics.udp_packets_allowed;
    const info = udp == null
      ? "Outbound UDP counter unavailable; local packet transmission could not be verified."
      : udp === 0
        ? "0 UDP packets passed the local endpoint firewall rule. Check local WireGuard routing."
        : udp + " UDP packets passed the container firewall. A reply or valid handshake was not observed; check the registered Surfshark key and upstream UDP connectivity.";
    details.append(element("p", info, "vpn-muted"));

    const tx = diagnostics.wireguard_tx_bytes;
    const rx = diagnostics.wireguard_rx_bytes;
    if (tx != null && rx != null) {
      details.append(element("p",
        "WireGuard peer transfer: " + tx + " bytes sent · " + rx + " bytes received.",
        "vpn-muted"));
      details.append(element("p",
        tx === 0
          ? "No data-transfer bytes were counted. Handshake-initiation packets may not be counted here; use the UDP firewall counter and endpoint route as separate evidence."
          : rx === 0
            ? "WireGuard counted outgoing tunnel data but no incoming data. This cannot distinguish key rejection from an upstream UDP problem."
            : "WireGuard recorded incoming data, but the handshake check failed. Inspect the registered key and network state.",
        "vpn-muted"));
    }
    if (diagnostics.endpoint_route) {
      const route = diagnostics.endpoint_route;
      const description = route === "underlay"
        ? "Endpoint routing: via the Docker network (correct route for WireGuard UDP)."
        : route === "tunnel"
          ? "Endpoint routing error: WireGuard's own UDP packets are directed back into wg0."
          : "Endpoint routing could not be verified.";
      details.append(element("p", description, route === "tunnel" ? "vpn-error" : "vpn-muted"));
    }
    if (diagnostics.client_public_key) {
      details.append(element("p",
        "Your derived WireGuard CLIENT PUBLIC KEY (safe to compare with Surfshark's registered key):",
        "vpn-muted"));
      details.append(element("code", diagnostics.client_public_key, "vpn-public-key"));
      details.append(element("p",
        "In Surfshark → VPN → Manual setup → WireGuard, check that the key pair registered in your account matches this public key. Do not share your private key.",
        "vpn-muted"));
    }
    if (diagnostics.endpoint && diagnostics.port) {
      details.append(element("p",
        "Surfshark endpoint: " + diagnostics.endpoint + ":" + diagnostics.port,
        "vpn-muted"));
    }
    parent.append(details);
  }

  function showTest(data) {
    const test = data.test || {};
    testResults.replaceChildren();
    for (const entry of test.results || []) {
      const row = element("div", undefined, "vpn-test-row");
      row.append(element("span", entry.ok ? "PASS" : "FAIL", entry.ok ? "vpn-pass" : "vpn-fail"));
      if (!entry.ok && entry.phase) row.append(element("span", entry.phase.toUpperCase(), "vpn-fail"));
      const desc = entry.country + " → observed " +
        (entry.country_code || "unknown") + " · " +
        (entry.ip || "No verified IP") + " · " + (entry.message || "");
      row.append(element("span", desc));
      if (!entry.ok) showHandshakeDiagnostics(row, entry.diagnostics);
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
      // A normal status refresh must never release a pending Connect,
      // Remove or Upload request. Only a completed test may clear its lock.
      if (testing && !testStarting) {
        testing = false;
        setBusy(false);
      }
    }
  }

  function keyField(key) {
    const input = element("input", undefined, "vpn-public-key-input");
    input.type = "text";
    input.readOnly = true;
    input.value = key;
    input.setAttribute("aria-label", "Client public key for Surfshark");
    input.addEventListener("click", () => {
      if (typeof input.select === "function") input.select();
    });
    return input;
  }

  function showPublicKeys(profiles) {
    publicKeysPanel.replaceChildren();
    if (!profiles.length) return;
    const uniqueKeys = [...new Set(profiles.map(p => p.client_public_key).filter(Boolean))];
    const missing = profiles.filter(p => !p.client_public_key).length;
    publicKeysPanel.append(element("strong",
      "Client public key to register with Surfshark"));
    if (uniqueKeys.length === 1 && !missing) {
      publicKeysPanel.append(element("p",
        "All " + profiles.length + " countries use this same client key. Register it once.",
        "vpn-muted"));
      publicKeysPanel.append(keyField(uniqueKeys[0]));
    } else if (uniqueKeys.length > 1) {
      publicKeysPanel.append(element("p",
        "These profiles use different client keys. Register each distinct key in Surfshark and check the country-specific values below.",
        "vpn-key-warning"));
      for (const key of uniqueKeys) {
        const count = profiles.filter(p => p.client_public_key === key).length;
        publicKeysPanel.append(element("p",
          count + " location(s) use:", "vpn-muted"));
        publicKeysPanel.append(keyField(key));
      }
    } else if (uniqueKeys.length === 1) {
      publicKeysPanel.append(element("p",
        "Some keys could not be derived. Available key:", "vpn-key-warning"));
      publicKeysPanel.append(keyField(uniqueKeys[0]));
    } else {
      publicKeysPanel.append(element("p",
        "No client public keys available. Update the WireGuard gateway first.",
        "vpn-muted"));
    }
    publicKeysPanel.append(element("p",
      "Select the text to copy it. The server's [Peer] PublicKey in a .conf file is different and must not be registered.",
      "vpn-muted"));
  }

  function render(data) {
    // Update test/busy state BEFORE deriving buttons' disabled states.
    showTest(data);
    const profiles = data.profiles || [];
    showPublicKeys(profiles);
    $("vpn-count").textContent = profiles.length + " locations";
    const activeName = profiles.find((p) => p.id === data.active)?.country || "Unknown";
    const ready = Boolean(data.active && data.tunnel_ready);
    const blocked = Boolean(data.kill_switch && !ready);
    $("vpn-connection").textContent = ready
      ? "WIREGUARD CONNECTED · " + activeName + " (temporary)"
      : blocked ? "VPN NOT CONNECTED · DOWNLOADS BLOCKED"
        : "DIRECT CONNECTION · DEFAULT";
    $("vpn-connection").classList.toggle("vpn-disconnected", !ready);
    $("vpn-connection").title = blocked
      ? "The VPN kill switch prevents direct outbound downloads until a VPN profile successfully connects."
      : "";
    if (blocked && !data.test?.running && !feedback.classList.contains("vpn-error")) {
      message("No VPN handshake is active. Downloads are blocked by the kill switch. " +
        "Retry a profile and inspect the specific failure shown below.", true);
    }
    list.replaceChildren();
    for (const item of profiles) {
      const row = element("div", undefined, "vpn-profile");
      row.dataset.profileId = item.id;
      const description = element("div", undefined, "vpn-profile-description");
      description.append(element("strong", item.country));
      description.append(element("small", item.filename));
      row.append(description);
      if (item.client_public_key) {
        const details = element("details", undefined, "vpn-profile-key");
        details.append(element("summary", "Client public key"));
        details.append(keyField(item.client_public_key));
        row.append(details);
      }
      if (data.active === item.id) row.append(element("span", "CONNECTED", "vpn-active-tag"));
      const actions = element("div", undefined, "vpn-row-actions");
      if (item.id !== data.active) {
        const connecting = pendingProfile === item.id;
        const connect = element("button", connecting ? "Connecting…" : "Connect",
          connecting ? "vpn-secondary vpn-connecting" : "vpn-secondary");
        connect.type = "button";
        connect.disabled = busy || Boolean(data.test?.running);
        connect.addEventListener("click", () => act("/activate/" + item.id,
          "Verifying " + item.country + "…", item.id));
        actions.append(connect);
        const remove = element("button", "Remove", "vpn-danger");
        remove.type = "button";
        remove.disabled = busy || Boolean(data.test?.running);
        remove.addEventListener("click", async () => {
          if (!window.confirm("Remove WireGuard profile " + item.filename + "?")) return;
          await act("/delete/" + item.id, "Removing profile…", item.id);
        });
        actions.append(remove);
      }
      row.append(actions);
      if (pendingProfile === item.id) {
        showProfileResult(row, {message: "Contacting Surfshark and checking the handshake…"});
      } else if (lastProfileResult?.id === item.id) {
        showProfileResult(row, lastProfileResult);
      }
      list.append(row);
    }
    if (!profiles.length) {
      list.append(element("p",
        "No WireGuard profiles uploaded. Add at least two countries to test rotation.",
        "vpn-muted"));
    }
    $("vpn-test-button").disabled = busy || Boolean(data.test?.running) || profiles.length < 1;
    $("vpn-next-button").disabled = busy || Boolean(data.test?.running) || profiles.length < 1;
    $("vpn-direct-button").disabled = busy || Boolean(data.test?.running) || !data.kill_switch;
  }

  async function refresh() {
    try {
      const data = await api("/profiles");
      render(data);
    } catch (err) {
      if (testing && !testStarting) {
        // Do not silently unlock a rotation test when status polling fails.
        // Continue polling for an authoritative finished state.
        if (!timer) timer = window.setInterval(refresh, 1800);
      }
      message(err.message, true);
    }
  }

  async function act(path, description, profileId = null) {
    if (busy) return;
    pendingProfile = profileId;
    lastProfileResult = null;
    setBusy(true);
    // Display feedback on the clicked row immediately. The message above
    // the profile list may be scrolled out of view.
    if (profileId) {
      const row = [...list.querySelectorAll(".vpn-profile")].find(
        node => node.dataset.profileId === profileId);
      if (row) {
        const connect = row.querySelector("button.vpn-secondary");
        if (connect) connect.textContent =
          path.startsWith("/activate/") ? "Connecting…" : "Working…";
        showProfileResult(row, {message: "Contacting Surfshark and checking the handshake…"});
      }
    }
    message(description);
    let failedDiagnostics = null;
    try {
      const data = await api(path, {method: "POST", body: "{}",
        headers: {"Content-Type": "application/json"}});
      let text = "Profile updated.";
      let error = false;
      if (path === "/direct") {
        text = "VPN disconnected. Normal direct connection restored.";
      } else if (path === "/rotate" && data.changed_ip === false) {
        text = "The VPN profile switched, but its public IP did not change.";
        error = true;
      } else if (path === "/rotate" || path.startsWith("/activate/")) {
        text = "Verified VPN exit: " + (data.ip || "?") +
          " · " + (data.country || "Unknown") + ".";
      }
      lastProfileResult = profileId
        ? {id: profileId, message: text, error}
        : null;
      message(text, error);
    } catch (err) {
      lastProfileResult = profileId
        ? {id: profileId, message: err.message, error: true,
          diagnostics: err.diagnostics}
        : null;
      failedDiagnostics = err.diagnostics;
      message(err.message, true);
      if (profileId) {
        // Show the failure immediately; even if the follow-up GET fails the
        // user can see what the Connect request returned beside the button.
        const row = [...list.querySelectorAll(".vpn-profile")].find(
          node => node.dataset.profileId === profileId);
        if (row) {
          const status = [...row.children].find(
            child => child.className?.includes("vpn-profile-result"));
          if (status) {
            status.textContent = err.message;
            status.classList.add("vpn-error");
            if (err.diagnostics) {
              const box = element("div", undefined, "vpn-profile-diagnostics");
              showHandshakeDiagnostics(box, err.diagnostics);
              row.append(box);
            }
          } else {
            showProfileResult(row, lastProfileResult);
          }
        }
      }
    } finally {
      pendingProfile = null;
      setBusy(false);
      try {
        await refresh();
      } catch (_err) {
        // The per-profile result should remain visible if refresh fails.
      }
      if (failedDiagnostics && !profileId) {
        const item = element("div", undefined, "vpn-test-row");
        showHandshakeDiagnostics(item, failedDiagnostics);
        testResults.prepend(item);
      }
    }
  }

  async function checkTemplates() {
    const files = Array.from($("vpn-upload-input").files || []);
    if (!files.length) {
      keySetup.hidden = true;
      keyInput.value = "";
      return;
    }
    let requiresKey = false;
    try {
      for (const file of files) {
        if (file.size > 8192) continue;
        const content = await file.text();
        if (content.includes("<insert_your_private_key_here>")) {
          requiresKey = true;
          break;
        }
      }
    } catch (_) {
      // Server preflight will report missing keys if client inspection fails.
    }
    keySetup.hidden = !requiresKey;
    if (requiresKey) {
      message("These Surfshark files are templates. Enter your registered WireGuard private key once for all selected locations.", true);
    } else {
      keyInput.value = "";
      message(files.length + " configuration " + (files.length === 1 ? "file" : "files") +
        " selected. Ready to upload.");
    }
  }

  $("vpn-upload-input").addEventListener("change", checkTemplates);
  $("vpn-next-button").addEventListener("click", () =>
    act("/rotate", "Temporarily connecting to an alternate VPN exit…"));
  $("vpn-direct-button").addEventListener("click", () =>
    act("/direct", "Disconnecting WireGuard and restoring direct access…"));

  $("vpn-upload").addEventListener("click", async () => {
    const input = $("vpn-upload-input");
    if (!input.files || !input.files.length) {
      message("Select one or more Surfshark WireGuard .conf files.", true);
      return;
    }
    if (busy) return;
    const privateKey = keyInput.value.trim();
    if (!keySetup.hidden && !privateKey) {
      message("Your files lack a WireGuard client private key. Generate one in Surfshark's manual WireGuard setup and paste it above.", true);
      keyInput.focus();
      return;
    }
    const body = new FormData();
    if (privateKey) body.append("private_key", privateKey);
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
      message(count + " WireGuard " + (count === 1 ? "profile" : "profiles") +
        " uploaded. Normal direct connection remains the default.", false);
      input.value = "";
      keyInput.value = "";
      keySetup.hidden = true;
    } catch (err) {
      if (err.needsPrivateKey) {
        keySetup.hidden = false;
        keyInput.focus();
      }
      message(err.message, true);
    } finally {
      setBusy(false);
      await refresh();
    }
  });

  $("vpn-test-button").addEventListener("click", async () => {
    if (busy) return;
    if (!window.confirm("Test every VPN country? Downloads are paused during testing. Normal direct networking will be restored afterwards.")) return;
    testStarting = true;
    setBusy(true);
    message("Beginning VPN rotation test…");
    try {
      const result = await api("/test", {method: "POST", body: "{}",
        headers: {"Content-Type": "application/json"}});
      if (!result.started) throw Error("Gateway did not start the test.");
      testing = true;
      testStarting = false;
      await refresh();
    } catch (err) {
      testStarting = false;
      testing = false;
      setBusy(false);
      message(err.message, true);
    }
  });
  panel.addEventListener("toggle", () => { if (panel.open) refresh(); });
  // VPN fallback is automatic: an open panel must not show stale routing
  // state while the download worker temporarily switches endpoints.
  window.setInterval(() => {
    if (panel.open && !busy && !testing) refresh();
  }, 5000);
  refresh();
})();
