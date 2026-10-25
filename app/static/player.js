// Our own video controls, in place of each browser's built-in ones.
// Existing code keeps using video.controls = true/false; that now shows or hides this bar.
(() => {
  const ICON = (name) => `<i class="fas ${name}" aria-hidden="true"></i>`;
  const SPEEDS = [1, 2, 4, 8];
  const clock = (s) => {
    if (!isFinite(s)) { return "0:00" }
    s = Math.floor(s);
    const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, sec = String(s % 60).padStart(2, "0");
    return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
  };
  const button = (cls, label, icon) =>
    `<button type="button" class="vp-btn ${cls}" aria-label="${label}" title="${label}">${ICON(icon)}</button>`;

  function enhance(video) {
    if (video.vp) { return }
    // Live camera tiles are picture-first and have no timeline; recordings get the full bar.
    const live = !!video.dataset.cam;
    let box = video.parentElement;
    if (!live) {
      box = document.createElement("div");
      video.before(box);
      box.append(video);
    }
    box.classList.add("vp");
    const bar = document.createElement("div");
    bar.className = `vp-bar ${live ? "vp-live fs-display-none" : ""}`;
    bar.innerHTML = live
      ? button("vp-play", "Play", "fa-play") + button("vp-mute", "Unmute", "fa-volume-xmark") + button("vp-full", "Full screen", "fa-expand")
      : button("vp-play", "Play", "fa-play") +
        `<span class="vp-time">0:00</span>` +
        `<input class="vp-seek" type="range" min="0" max="1000" value="0" step="1" aria-label="Seek" />` +
        `<span class="vp-time vp-dur">0:00</span>` +
        `<button type="button" class="vp-btn vp-speed" aria-label="Playback speed" title="Playback speed">1\u00d7</button>` +
        button("vp-mute", "Mute", "fa-volume-high") +
        (document.pictureInPictureEnabled ? button("vp-pip", "Picture-in-picture", "fa-clone") : "") +
        button("vp-full", "Full screen", "fa-expand");
    box.append(bar);
    // Taps on the bar shouldn't reach the tile's own click handlers.
    bar.addEventListener("click", (e) => e.stopPropagation());
    const $ = (sel) => bar.querySelector(sel);

    // Take over the controls property so the browser never draws its own bar.
    let on = video.controls || video.hasAttribute("controls");
    video.removeAttribute("controls");
    Object.defineProperty(video, "controls", {
      configurable: true,
      get: () => on,
      set: (v) => { on = !!v; box.classList.toggle("vp-on", on) },
    });
    video.controls = on;
    video.vp = bar;

    const setIcon = (btn, icon, label) => {
      btn.innerHTML = ICON(icon);
      btn.setAttribute("aria-label", label);
      btn.title = label;
    };
    const sync = () => {
      box.classList.toggle("vp-paused", video.paused);
      setIcon($(".vp-play"), video.paused ? "fa-play" : "fa-pause", video.paused ? "Play" : "Pause");
      const quiet = video.muted || video.volume === 0;
      setIcon($(".vp-mute"), quiet ? "fa-volume-xmark" : "fa-volume-high", quiet ? "Unmute" : "Mute");
    };
    ["play", "pause", "volumechange", "emptied"].forEach((e) => video.addEventListener(e, sync));
    sync();

    const toggle = () => { video.paused ? video.play().catch(() => { }) : video.pause() };
    $(".vp-play").addEventListener("click", toggle);
    $(".vp-mute").addEventListener("click", () => { video.muted = !video.muted; if (!video.muted && !video.volume) { video.volume = 1 } });
    $(".vp-full").addEventListener("click", () => {
      if (document.fullscreenElement) { document.exitFullscreen(); return }
      // iPhone Safari can only full-screen the video itself, with its own controls.
      box.requestFullscreen ? box.requestFullscreen().catch(() => { }) : video.webkitEnterFullscreen?.();
    });
    document.addEventListener("fullscreenchange", () => {
      const full = document.fullscreenElement === box;
      setIcon($(".vp-full"), full ? "fa-compress" : "fa-expand", full ? "Exit full screen" : "Full screen");
    });

    // Touch screens have no hover: a tap shows the bar for a few seconds.
    let hideTimer;
    box.addEventListener("pointerdown", (e) => {
      if (e.pointerType === "mouse") { return }
      box.classList.add("vp-active");
      clearTimeout(hideTimer);
      hideTimer = setTimeout(() => box.classList.remove("vp-active"), 3000);
    });
    if (live) { return }

    video.addEventListener("click", toggle);
    const seek = $(".vp-seek");
    let dragging = false;
    const progress = () => {
      $(".vp-time").textContent = clock(video.currentTime);
      $(".vp-dur").textContent = clock(video.duration);
      if (!dragging) { seek.value = video.duration ? (video.currentTime / video.duration) * 1000 : 0 }
      seek.style.setProperty("--vp-fill", `${seek.value / 10}%`);
    };
    ["timeupdate", "durationchange", "loadedmetadata", "emptied"].forEach((e) => video.addEventListener(e, progress));
    seek.addEventListener("input", () => {
      dragging = true;
      if (video.duration) { video.currentTime = (seek.value / 1000) * video.duration }
      progress();
    });
    seek.addEventListener("change", () => { dragging = false });

    const speed = $(".vp-speed");
    speed.addEventListener("click", () => {
      video.playbackRate = SPEEDS[(SPEEDS.indexOf(video.playbackRate) + 1) % SPEEDS.length] || 1;
    });
    video.addEventListener("ratechange", () => { speed.textContent = `${video.playbackRate}\u00d7` });
    // A new clip keeps the chosen speed instead of snapping back to 1×.
    video.addEventListener("loadedmetadata", () => { video.playbackRate = parseFloat(speed.textContent) || 1 });

    $(".vp-pip")?.addEventListener("click", () => {
      document.pictureInPictureElement === video ? document.exitPictureInPicture() : video.requestPictureInPicture().catch(() => { });
    });

    // Space, k, m, f and arrow keys work once the player has focus.
    box.tabIndex = -1;
    box.addEventListener("keydown", (e) => {
      if (e.target.matches("input") && e.key.startsWith("Arrow")) { return }
      const keys = {
        " ": toggle, k: toggle, m: () => $(".vp-mute").click(), f: () => $(".vp-full").click(),
        ArrowLeft: () => { video.currentTime -= 5 }, ArrowRight: () => { video.currentTime += 5 },
      };
      if (keys[e.key] && !(e.key === " " && e.target.matches("button"))) { e.preventDefault(); e.stopPropagation(); keys[e.key]() }
    });
  }

  const run = () => document.querySelectorAll("video").forEach(enhance);
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", run) : run();
})();
