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
      ? button("vp-play", "Play", "fa-play") + button("vp-mute", "Unmute", "fa-volume-xmark") + button("vp-snap", "Save a photo", "fa-camera") + button("vp-full", "Full screen", "fa-expand")
      : button("vp-play", "Play", "fa-play") +
        `<span class="vp-time">0:00</span>` +
        `<input class="vp-seek" type="range" min="0" max="1000" value="0" step="1" aria-label="Seek" />` +
        `<span class="vp-time vp-dur">0:00</span>` +
        `<button type="button" class="vp-btn vp-speed" aria-label="Playback speed" title="Playback speed">1\u00d7</button>` +
        button("vp-mute", "Mute", "fa-volume-high") +
        (document.pictureInPictureEnabled ? button("vp-pip", "Picture-in-picture", "fa-clone") : "") +
        button("vp-full", "Full screen", "fa-expand");
    // Camera tiles keep their buttons in the bar under the picture.
    (live && box.closest(".camera")?.querySelector(".vp-slot") || box).append(bar);
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
      const quiet = !sound.on() && (video.muted || video.volume === 0);
      setIcon($(".vp-mute"), quiet ? "fa-volume-xmark" : "fa-volume-high", quiet ? "Unmute" : "Mute");
    };
    const sound = warmSound(video, () => sync());
    ["play", "pause", "volumechange", "emptied"].forEach((e) => video.addEventListener(e, sync));
    sync();

    const toggle = () => { video.paused ? video.play().catch(() => { }) : video.pause() };
    $(".vp-play").addEventListener("click", toggle);
    $(".vp-mute").addEventListener("click", sound.toggle);
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
    if (live) {
      freezeFrame(video);
      box.addEventListener("dblclick", () => $(".vp-full").click());
      $(".vp-snap").addEventListener("click", () => savePhoto(video));
      return;
    }

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
    // Touch drags tick the phone every 5% of travel, a firmer tick on each minute mark.
    let touch = false, tickAt = 0, minute = -1;
    seek.addEventListener("pointerdown", (e) => { touch = e.pointerType !== "mouse" && !!navigator.vibrate; tickAt = +seek.value; minute = -1 });
    seek.addEventListener("input", () => {
      dragging = true;
      if (touch) {
        const m = Math.floor(((seek.value / 1000) * video.duration) / 60) || 0;
        if (minute >= 0 && m !== minute) { navigator.vibrate(10); tickAt = +seek.value }
        else if (Math.abs(seek.value - tickAt) >= 50) { navigator.vibrate(4); tickAt = +seek.value }
        minute = m;
      }
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

  // Hiss gate decision: feed it RMS levels (dB) every 20 ms, it returns the gain to aim for.
  // The noise floor is the quietest level of the last ~5 s; 6 dB over it is a sound worth hearing.
  const gate = (size = 250, hold = 200) => {
    const ring = [];
    let loudAt = -Infinity;
    return (db, t) => {
      db = Math.max(db, -100);  // digital silence is -Infinity
      ring.push(db);
      if (ring.length > size) { ring.shift() }
      if (db >= Math.min(...ring) + 6) { loudAt = t }
      return t - loudAt < hold ? 1 : 0.18;  // -15 dB, not silence: hiss that drops out entirely sounds broken
    };
  };
  window.vpGate = gate;

  // Unmute fades in through an 80 Hz highpass and the hiss gate. Anything Web Audio can't take gets the plain muted toggle.
  let ctx;
  function warmSound(video, sync) {
    let g, on = false, tick, aim = 1;  // g: the audio graph once built, false once it failed
    const decide = gate();
    const source = () => {  // true when the sound goes through the graph
      if (g === false) { return false }
      try {
        ctx ||= new (window.AudioContext || window.webkitAudioContext)();
        ctx.resume();
        if (!g) {
          g = { hp: ctx.createBiquadFilter(), gate: ctx.createGain(), fade: ctx.createGain(), an: ctx.createAnalyser() };
          g.hp.type = "highpass"; g.hp.frequency.value = 80; g.fade.gain.value = 0;
          g.hp.connect(g.gate).connect(g.fade).connect(ctx.destination);
          g.hp.connect(g.an);
          g.buf = new Float32Array(g.an.fftSize);
        }
        if (g.el) { return true }  // an element can only be captured once, and it stays captured
        const s = video.srcObject;
        if (s instanceof MediaStream) {  // WebRTC: Chrome won't route srcObject through the element, so tap the stream
          if (!s.getAudioTracks().length) { return false }
          if (g.stream !== s) { g.src?.disconnect(); g.src = ctx.createMediaStreamSource(s); g.src.connect(g.hp); g.stream = s }
          return true;
        }
        // A cross-origin src without CORS would play as silence through the graph.
        const url = video.currentSrc && new URL(video.currentSrc, location.href);
        if (!url || (url.protocol !== "blob:" && url.origin !== location.origin)) { return false }
        g.src = ctx.createMediaElementSource(video); g.src.connect(g.hp); g.el = true;
        return true;
      } catch { g = false; return false }
    };
    const ramp = (to, secs) => {
      const t = ctx.currentTime, p = g.fade.gain;
      p.cancelScheduledValues(t); p.setValueAtTime(to ? 0 : p.value, t); p.linearRampToValueAtTime(to, t + secs);
    };
    const listen = () => {
      g.an.getFloatTimeDomainData(g.buf);
      let sum = 0;
      for (const x of g.buf) { sum += x * x }
      const want = decide(10 * Math.log10(sum / g.buf.length + 1e-12), performance.now());
      // Opens in ~10 ms so words keep their first sound; closes over ~300 ms.
      if (want !== aim) { aim = want; g.gate.gain.setTargetAtTime(want, ctx.currentTime, want === 1 ? 0.01 : 0.1) }
    };
    const fadeIn = () => { ramp(1, 0.5); clearInterval(tick); if (!video.paused) { tick = setInterval(listen, 20) } };
    const unmute = () => {
      const graph = source();
      if (!g?.stream) { video.muted = false; if (!video.volume) { video.volume = 1 } }  // a tapped stream keeps the element muted, or it plays twice
      if (graph) { on = true; fadeIn(); sync() }
    };
    const mute = () => {
      if (!on) { video.muted = true; return }
      on = false; clearInterval(tick); sync();
      ramp(0, 0.15);
      setTimeout(() => { if (!on) { video.muted = true } }, 150);
    };
    video.addEventListener("playing", () => { if (on) { source() ? fadeIn() : (on = false, sync()) } });  // a reconnect fades in too
    video.addEventListener("pause", () => clearInterval(tick));
    // Something else muted the element (a stream reload), so the sound is off.
    video.addEventListener("volumechange", () => { if (on && g.el && video.muted) { on = false; clearInterval(tick); sync() } });
    return {
      on: () => on,
      toggle: () => { on || !(video.muted || video.volume === 0) ? mute() : unmute() },
    };
  }

  // While a live camera plays, keep its picture every few seconds, to show instead of black when the feed drops.
  function freezeFrame(video) {
    const img = Object.assign(document.createElement("img"), { className: "vp-freeze", alt: "", hidden: true });
    video.after(img);
    const canvas = document.createElement("canvas");
    setInterval(() => {
      if (video.paused || video.readyState < 2 || !video.videoWidth || document.hidden) { return }
      try {
        canvas.width = Math.min(640, video.videoWidth);
        canvas.height = Math.round((canvas.width * video.videoHeight) / video.videoWidth);
        canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
        img.src = canvas.toDataURL("image/jpeg", 0.7);
      } catch { }  // a cross-origin stream taints the canvas: no frozen frame, as before
    }, 3000);
    ["emptied", "waiting", "abort"].forEach((e) => video.addEventListener(e, () => {
      if (!img.getAttribute("src")) { return }
      img.style.objectFit = getComputedStyle(video).objectFit;
      img.hidden = false;
    }));
    video.addEventListener("playing", () => { img.hidden = true });
  }

  // Downloads what the camera shows right now, named after the camera and the time.
  function savePhoto(video) {
    const name = video.closest(".camera")?.querySelector(".card-header-title .truncate")?.textContent || video.dataset.cam;
    const stamp = new Date().toLocaleString([], { dateStyle: "medium", timeStyle: "short" }).replace(/[\\/:*?"<>|]/g, ".");
    const save = (href) => Object.assign(document.createElement("a"), { href, download: `${name} ${stamp}.jpg` }).click();
    if (video.readyState < 2 || !video.videoWidth) {  // not playing yet: the latest snapshot is the best we have
      if (video.poster && !video.poster.endsWith("loading.svg")) { save(video.poster) }
      return;
    }
    const canvas = Object.assign(document.createElement("canvas"), { width: video.videoWidth, height: video.videoHeight });
    canvas.getContext("2d").drawImage(video, 0, 0);
    canvas.toBlob((blob) => { const url = URL.createObjectURL(blob); save(url); setTimeout(() => URL.revokeObjectURL(url), 1000) }, "image/jpeg", 0.92);
  }

  // Keep the screen awake while any live camera plays, so a tablet or laptop used as a monitor doesn't go dark.
  let wakeLock = null;
  const keepAwake = async () => {
    const playing = [...document.querySelectorAll("video[data-cam]")].some((v) => !v.paused);
    try {
      if (playing && !wakeLock && document.visibilityState === "visible" && navigator.wakeLock) {
        wakeLock = await navigator.wakeLock.request("screen");
        wakeLock.addEventListener("release", () => { wakeLock = null });
      } else if (!playing && wakeLock) { wakeLock.release() }
    } catch { }  // refused (battery saver, insecure page): the screen just sleeps as usual
  };
  ["play", "pause", "emptied"].forEach((e) => document.addEventListener(e, keepAwake, true));
  document.addEventListener("visibilitychange", keepAwake);  // the browser drops the lock when the tab is hidden

  const run = () => document.querySelectorAll("video").forEach(enhance);
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", run) : run();
})();
