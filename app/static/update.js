// Toasts: newest on top, at most 3, a repeat replaces the old one, and good news goes away fast.
const TOAST_MS = { success: 3000, primary: 3000, info: 3000, warning: 6000, danger: 10000 };
function toast(title, message, type = "primary") {
  const box = document.getElementById("toasts");
  if (!box) return;
  const key = `${title}\n${message}`;
  [...box.children].forEach((old) => old.dataset.key === key && old.remove());
  const t = Object.assign(document.createElement("div"), {
    className: `toast toast-${type} pointer-events-auto rounded-xl bg-zinc-900/95 px-4 py-3 text-sm shadow-2xl ring-1 ring-white/10 backdrop-blur`,
    title: "Tap to dismiss",
  });
  t.dataset.key = key;
  t.append(Object.assign(document.createElement("strong"), { textContent: title }), document.createElement("br"), message);
  t.addEventListener("click", () => t.remove());
  box.prepend(t);
  while (box.children.length > 3) box.lastElementChild.remove();
  setTimeout(() => t.remove(), TOAST_MS[type] || 5000);
}

// Updates: a banner when a release is out, a waiting screen while it installs, and a note once it's done.
document.addEventListener("DOMContentLoaded", () => {
  const checkAPI = document.getElementById("checkUpdate");
  const updateBanner = document.getElementById("updateBanner");
  const current = checkAPI.dataset.version;
  const newer = (a, b) => a.localeCompare(b, undefined, { numeric: true }) === 1;
  const saved = (key, value) => {
    try {
      if (value === undefined) return JSON.parse(localStorage.getItem(key));
      localStorage.setItem(key, JSON.stringify(value));
    } catch { return null; }
  };
  // GitHub's generated notes are "* Title by @user in https://…/pull/1"; keep the titles.
  const release = (path) => fetch(`https://api.github.com/repos/itskiwi/Wyze-Tunnel/releases/${path}`)
    .then((r) => {
      if (!r.ok) throw new Error(r.status === 404 ? "No releases published yet" : `GitHub returned ${r.status}`);
      return r.json();
    })
    .then((d) => ({
      version: d.tag_name.replace(/^v/, ""), url: d.html_url,
      notes: (d.body || "").split("\n").filter((l) => l.startsWith("* ")).map((l) => l.slice(2).replace(/ by @\S+.*$| in https:\S+$/, "").trim()),
    }));

  function showUpdateBanner(rel, done) {
    if (!updateBanner) return;
    const $ = (sel) => updateBanner.querySelector(sel);
    $("[data-icon]").className = `fas ${done ? "fa-circle-check" : "fa-gift"}`;
    $("[data-title]").textContent = done ? `Updated to v${rel.version}` : `Wyze Tunnel v${rel.version} is ready`;
    $("[data-text]").textContent = done
      ? (sessionStorage.getItem("wt-updating") ? "All done. Your cameras are back." : "Wyze Tunnel updated itself while you were away.")
      : (updateBanner.dataset.canUpdate ? "Installing takes about a minute. Cameras pause, then come back on their own."
        : "Update your Docker container or Home Assistant add-on to get it.");
    const list = $("[data-notes]");
    const shown = rel.notes.length > 6 ? 5 : 6;
    list.replaceChildren(...rel.notes.slice(0, shown).map((n) => Object.assign(document.createElement("li"), { textContent: n })));
    if (rel.notes.length > shown) list.append(Object.assign(document.createElement("li"), { textContent: `and ${rel.notes.length - shown} more` }));
    $("[data-notes-title]").classList.toggle("is-hidden", !rel.notes.length);
    $("[data-link]").href = rel.url;
    $("[data-update]").classList.toggle("is-hidden", done || !updateBanner.dataset.canUpdate);
    $("[data-update]").onclick = () => startUpdate(rel.version);
    $("[data-dismiss]").textContent = done ? "Got it" : "Later";
    $("[data-dismiss]").onclick = () => {
      updateBanner.classList.add("is-hidden");
      if (!done) saved("wt-update-later", rel.version);
    };
    updateBanner.classList.remove("is-hidden");
  }

  function checkForUpdate(manual) {
    const cached = saved("wt-latest");
    const fresh = !manual && cached && Date.now() - cached.at < 6 * 3600e3;
    return (fresh ? Promise.resolve(cached.rel) : release("latest").then((rel) => (saved("wt-latest", { at: Date.now(), rel }), rel)))
      .then((rel) => {
        if (newer(rel.version, current) && (manual || saved("wt-update-later") !== rel.version)) showUpdateBanner(rel, false);
        else if (manual) toast("You're up to date", `v${current} is the latest version.`, "success");
      });
  }

  // While the updater runs the bridge restarts, so wait for it to come back with a new version.
  function startUpdate(version) {
    const overlay = document.getElementById("updateOverlay");
    const $ = (sel) => overlay.querySelector(sel);
    const step = (title, text) => { $("[data-title]").textContent = title; $("[data-text]").textContent = text; };
    const fail = (title, text) => { step(title, text); $(".update-bar").classList.add("is-hidden"); $("[data-reload]").classList.remove("is-hidden"); };
    overlay.classList.remove("is-hidden");
    step(`Updating to v${version}`, "Downloading the new version. Keep this page open, it comes back by itself.");
    fetch("update", { method: "POST" })
      .then((r) => r.ok ? r.json() : Promise.reject(new Error(`The bridge said ${r.status}`)))
      .then(() => {
        sessionStorage.setItem("wt-updating", "1");
        const started = Date.now();
        let wentDown = false;
        const poll = () => fetch("update", { cache: "no-store" })
          .then((r) => r.json())
          .then((d) => {
            if (newer(d.version, current)) {
              step("Update finished", `Loading v${d.version}…`);
              return setTimeout(() => location.reload(), 1500);
            }
            if (wentDown || Date.now() - started > 4 * 60e3) {
              return fail("The update didn't install", "Wyze Tunnel is still running the old version. Try again later, or run \"wyze-tunnel update\" on the computer it runs on.");
            }
            setTimeout(poll, 3000);
          })
          .catch(() => {
            if (!wentDown) step(`Updating to v${version}`, "Restarting Wyze Tunnel. Cameras pause for a moment.");
            wentDown = true;
            if (Date.now() - started > 10 * 60e3) return fail("This is taking longer than usual", "Wyze Tunnel hasn't come back yet. Reload the page in a minute.");
            setTimeout(poll, 3000);
          });
        setTimeout(poll, 3000);
      })
      .catch((error) => fail("Couldn't start the update", error.message));
    $("[data-reload]").onclick = () => location.reload();
  }

  if (updateBanner) {
    const seen = saved("wt-version");
    saved("wt-version", current);
    if (seen && newer(current, seen)) {
      release(`tags/v${current}`).catch(() => ({ version: current, url: "https://github.com/itskiwi/Wyze-Tunnel/releases", notes: [] }))
        .then((rel) => { showUpdateBanner(rel, true); sessionStorage.removeItem("wt-updating"); });
    } else {
      checkForUpdate(false).catch(() => { });
    }
  }
  checkAPI.addEventListener("click", () => {
    const icon = checkAPI.querySelector(".fa-arrows-rotate").classList;
    icon.add("fa-spin");
    checkForUpdate(true)
      .catch((error) => toast("Update check failed", error.message, "danger"))
      .finally(() => icon.remove("fa-spin"));
  });
});
