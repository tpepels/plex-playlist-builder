(() => {
  "use strict";
  const button = document.getElementById("youtube-cookie-verify-button");
  const status = document.getElementById("youtube-cookie-verify-result");
  const token = document.querySelector("#youtube-cookie-upload-form [name='_cookie_token']");
  const state = document.getElementById("youtube-cookie-state");
  if (!button || !status || !token || !state) return;

  button.addEventListener("click", async () => {
    button.disabled = true;
    status.textContent = "Checking YouTube login and yt-dlp metadata…";
    status.classList.remove("vpn-error");
    try {
      const body = new URLSearchParams({_cookie_token: token.value});
      const response = await fetch("/youtube/cookies/verify", {
        method: "POST",
        headers: {"X-Requested-With": "XMLHttpRequest",
                  "Content-Type": "application/x-www-form-urlencoded"},
        body,
        cache: "no-store",
      });
      if (!response.ok && response.status !== 409) {
        throw Error("Cookie verification request failed.");
      }
      const result = await response.json();
      if (!["valid", "rejected", "inconclusive"].includes(result.status)) {
        throw Error("Unexpected verification result.");
      }
      state.textContent = result.status === "valid" ? "Verified"
        : result.status === "rejected" ? "Rejected" : "Inconclusive";
      status.textContent = result.message;
      status.classList.toggle("vpn-error", result.status === "rejected");
    } catch (_) {
      state.textContent = "Inconclusive";
      status.textContent = "Could not complete the verification request. Check the server connection.";
    } finally {
      button.disabled = false;
    }
  });
})();
