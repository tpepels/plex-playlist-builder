/* Home intake workflows: preview/merge/sync YAML; native download form submits directly.
   These operations are independent of the read-only overview and of each other. */
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
  const confirm = $("home-import-confirm");
  const cancel = $("home-import-cancel");
  const progressPanel = $("home-import-progress-panel");
  const progress = $("home-import-progress");
  const current = $("home-import-current");
  const errors = $("home-import-errors");
  if (!fileInput || !dropZone || !confirm || !cancel) return;

  const MAX_BYTES = 2 * 1024 * 1024;
  let selection = null;
  let previewVersion = 0;
  let busy = false;

  function message(text, kind = "") {
    status.textContent = text;
    status.className = "home-form-status" + (kind ? " " + kind : "");
  }
  function clearSelection() {
    selection = null;
    fileInput.value = "";
    label.textContent = "Choose a YAML file";
    preview.hidden = true;
    progressPanel.hidden = true;
    summary.replaceChildren();
    confirm.disabled = true;
  }
  function setBusy(value) {
    busy = value;
    fileInput.disabled = value;
    confirm.disabled = value || !selection || selection.changedCount === 0;
    cancel.disabled = value;
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
    const currentVersion = ++previewVersion;
    clearSelection();
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
    message("Validating playlist definitions…");
    try {
      const text = await file.text();
      if (currentVersion !== previewVersion) return;
      const comparison = await postJson("/api/inspect-config", {text}, 20000);
      if (currentVersion !== previewVersion) return;
      const newNames = comparison.new || [];
      const updatedNames = comparison.updated || [];
      const retained = comparison.retained || [];
      const unchanged = comparison.unchanged || [];
      selection = {
        fileName: file.name, text, comparison,
        changedCount: newNames.length + updatedNames.length,
      };
      summary.replaceChildren(
        metric(newNames.length, "new"), metric(updatedNames.length, "updated"),
        metric(unchanged.length, "unchanged"), metric(retained.length, "retained")
      );
      description.textContent = selection.changedCount
        ? "Only new and changed definitions will be synced. Existing definitions absent from this file are retained."
        : "This collection is already loaded. No playlist changes are required.";
      preview.hidden = false;
      setBusy(false);
      message(selection.changedCount
        ? "Review the changes below before importing."
        : "No new or modified playlists.", selection.changedCount ? "" : "success");
    } catch (error) {
      if (currentVersion !== previewVersion) return;
      clearSelection();
      message("Could not inspect YAML: " + error.message, "error");
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
  cancel.addEventListener("click", () => {
    if (busy) return;
    ++previewVersion;
    clearSelection();
    message("");
  });

  confirm.addEventListener("click", async () => {
    if (busy || !selection || !selection.changedCount) return;
    const importItem = selection;
    setBusy(true);
    preview.hidden = true;
    progressPanel.hidden = false;
    current.textContent = "Importing verified YAML…";
    errors.textContent = "";
    progress.value = 0;
    message("Saving the collection…");
    let imported = false;
    try {
      const data = await postJson("/api/import-config", {
        text: importItem.text, filename: importItem.fileName,
      }, 25000);
      imported = true;
      const changed = Array.isArray(data.changed) ? data.changed : [];
      const versionId = data.version?.id;
      progress.max = Math.max(1, changed.length);
      progress.value = 0;
      if (changed.length && !versionId)
        throw new Error("Saved collection lacks a sync version; open Playlists to inspect it");

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
          if (/version changed/i.test(error.message)) {
            failures.push("Saved collection changed during sync; remaining items were not attempted.");
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
      message("Imported " + importItem.fileName + ". " +
        (failures.length
          ? "Some Plex updates failed; definitions were saved. Review pending items in Playlists."
          : "Playlist updates are complete."),
        failures.length ? "error" : "success");
      ++previewVersion;
      selection = null;
      fileInput.value = "";
      label.textContent = "Choose a YAML file";
      $("refresh-home")?.click?.();
    } catch (error) {
      message((imported
        ? "YAML was imported, but syncing did not finish: "
        : "Import was not saved: ") + error.message, "error");
      current.textContent = imported
        ? "Open Playlists to review the saved collection and retry pending syncs."
        : "No changes saved.";
      if (imported) {
        ++previewVersion;
        selection = null;
        fileInput.value = "";
        label.textContent = "Choose a YAML file";
      }
    } finally {
      setBusy(false);
    }
  });
})();
