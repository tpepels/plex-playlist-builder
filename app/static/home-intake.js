/* One-step Home YAML intake: validate, merge, and sync only this file's changes. */
(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const fileInput = $("home-playlist-file");
  const dropZone = $("home-playlist-drop");
  const label = $("home-file-label");
  const status = $("home-import-status");
  const preview = $("home-import-preview");
  const summary = $("home-import-summary");
  const description = $("home-import-description");
  const progressPanel = $("home-import-progress-panel");
  const progress = $("home-import-progress");
  const current = $("home-import-current");
  const errors = $("home-import-errors");
  if (!fileInput || !dropZone) return;

  const MAX_BYTES = 2 * 1024 * 1024;
  let busy = false;

  function message(text, kind = "") {
    status.textContent = text;
    status.className = "home-form-status" + (kind ? " " + kind : "");
  }
  function setBusy(value) {
    busy = value;
    fileInput.disabled = value;
  }
  async function json(url, options = {}, timeoutMs = 20000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, {
        ...options, cache: "no-store", signal: controller.signal,
        headers: {Accept: "application/json", ...(options.headers || {})},
      });
      let body;
      try { body = await response.json(); }
      catch (_) { throw new Error("Invalid response from the music server"); }
      if (!response.ok) throw new Error(body.detail || body.error || "HTTP " + response.status);
      return body;
    } catch (error) {
      if (controller.signal.aborted) throw new Error("The music server did not respond in time");
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }
  const postJson = (url, data, timeoutMs) => json(url, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(data),
  }, timeoutMs);
  function metric(value, labelText) {
    const element = document.createElement("span");
    element.textContent = value + " " + labelText;
    return element;
  }

  async function inspectFile(file) {
    if (!file || busy) return;
    preview.hidden = true;
    progressPanel.hidden = true;
    summary.replaceChildren();
    errors.textContent = "";
    message("");
    if (!/\.ya?ml$/i.test(file.name)) {
      message("Choose a .yml or .yaml file.", "error");
      return;
    }
    if (!file.size || file.size > MAX_BYTES) {
      message("YAML file must be between 1 byte and 2 MB.", "error");
      return;
    }
    label.textContent = file.name;
    setBusy(true);
    message("Validating playlist definitions…");
    let imported = false;
    try {
      const text = await file.text();
      const comparison = await postJson("/api/inspect-config", {text}, 20000);
      const newNames = comparison.new || [];
      const updatedNames = comparison.updated || [];
      const retained = comparison.retained || [];
      const unchanged = comparison.unchanged || [];
      const changedCount = newNames.length + updatedNames.length;
      summary.replaceChildren(
        metric(newNames.length, "new"), metric(updatedNames.length, "updated"),
        metric(unchanged.length, "unchanged"), metric(retained.length, "retained")
      );
      description.textContent = changedCount
        ? "Validated. Only this file's new and changed playlists are being synced; absent definitions are retained."
        : "The collection is already loaded. No playlist changes are required.";
      preview.hidden = false;
      if (!changedCount) {
        message("YAML valid. No new or modified playlists.", "success");
        return;
      }

      progressPanel.hidden = false;
      current.textContent = "Importing validated YAML…";
      progress.max = changedCount;
      progress.value = 0;
      message("Saving playlists and updating Plex…");
      const data = await postJson("/api/import-config", {
        text, filename: file.name, review_token: comparison.review_token,
      }, 25000);
      imported = true;
      const changed = Array.isArray(data.changed) ? data.changed : [];
      const versionId = data.version?.id;
      if (changed.length && !versionId)
        throw new Error("Saved collection has no sync revision; open Playlists to inspect it");
      progress.max = Math.max(1, changed.length);
      const failures = [];
      let synced = 0, skipped = 0;
      for (let i = 0; i < changed.length; i++) {
        const name = changed[i];
        current.textContent = "Syncing " + (i + 1) + " / " + changed.length + ": " + name;
        try {
          const result = await postJson(
            "/api/sync-pending/" + encodeURIComponent(name),
            {version_id: versionId}, 90000
          );
          if (result.status === "synced") synced++;
          else if (result.status === "skipped") skipped++;
          else failures.push(name + ": " + (result.message || result.status || "Not synced"));
        } catch (error) {
          failures.push(name + ": " + error.message);
          if (/version changed|revision changed/i.test(error.message)) {
            failures.push("Playlist revision changed; remaining changes were not synced.");
            progress.value = i + 1;
            break;
          }
        }
        progress.value = i + 1;
      }
      errors.textContent = failures.join("\n");
      current.textContent = changed.length
        ? synced + " synced · " + skipped + " skipped · " + failures.length + " errors"
        : "No playlist changes to sync";
      message("Imported " + file.name + ". " +
        (failures.length
          ? "Some Plex updates failed; definitions were saved. Review pending items in Playlists."
          : "Playlist updates are complete."),
        failures.length ? "error" : "success");
      if (typeof window !== "undefined" && typeof window.dispatchEvent === "function")
        window.dispatchEvent(new Event("workspace:changed"));
    } catch (error) {
      message((imported
        ? "YAML was imported, but syncing did not finish: "
        : "Import was not saved: ") + error.message, "error");
      if (imported) {
        progressPanel.hidden = false;
        current.textContent = "Open Playlists to inspect and retry pending syncs.";
        if (typeof window !== "undefined" && typeof window.dispatchEvent === "function")
          window.dispatchEvent(new Event("workspace:changed"));
      } else {
        progressPanel.hidden = true;
      }
    } finally {
      setBusy(false);
      fileInput.value = "";
      label.textContent = "Choose a YAML file";
    }
  }

  fileInput.addEventListener("change", () => inspectFile(fileInput.files?.[0]));
  dropZone.addEventListener("dragover", event => {
    if (busy) return;
    event.preventDefault();
    dropZone.classList.add("dragging");
  });
  dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragging"));
  dropZone.addEventListener("drop", event => {
    event.preventDefault();
    dropZone.classList.remove("dragging");
    if (!busy) inspectFile(event.dataTransfer?.files?.[0]);
  });
})();
