// The camera page: live video full screen, controls that fade, and one sheet with rewind, settings and today's events.
(() => {
  const cp = document.getElementById("cp");
  if (!cp) { return }
  const $ = (id) => document.getElementById(id);
  const { cam, name } = cp.dataset;
  const live = $("cpLive"), past = $("cpPast"), note = $("cpNote"), sheet = $("cpSheet"), rail = $("cpRail"), track = $("cpTrack");
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const fmt = (d) => d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const say = (text, type = "primary") => typeof toast === "function" && toast(name, text, type);
  // An absolute URL: a relative one in a CSS variable would resolve against site.css.
  if (cp.dataset.poster) { cp.style.setProperty("--poster", `url("${new URL(cp.dataset.poster, location.href)}")`) }
  let loadSettings = () => { };  // replaced below when this viewer may change settings

  // Live video, the same way a Live tile plays it.
  const status = $("cpStatus");
  const setStatus = (text, state = "") => { status.dataset.state = state; status.querySelector("span").textContent = text; };
  function startLive() {
    if (live.classList.contains("webrtc")) {
      fetch(`signaling/${cam}?${cp.dataset.format || "webrtc"}`).then((r) => r.json()).then((data) => new Receiver(data))
        .catch(() => setStatus("Can't reach the camera", "error"));
      return;
    }
    fetch(`api/${cam}/start`).catch(() => { });
    if (window.Hls?.isSupported()) {
      const hls = new Hls({ liveDurationInfinity: true, maxBufferHole: 5, liveSyncDurationCount: 0, liveMaxLatencyDurationCount: 6 });
      hls.on(Hls.Events.ERROR, (evt, data) => { if (data.fatal) { hls.destroy(); setTimeout(startLive, 2000) } });
      hls.on(Hls.Events.MEDIA_ATTACHED, () => hls.loadSource(live.dataset.src));
      hls.attachMedia(live);
    } else { live.src = live.dataset.src }
  }
  live.addEventListener("playing", () => { if (past.hidden) { setStatus(["Live", cp.dataset.net].filter(Boolean).join(" · "), "live") } });
  live.addEventListener("waiting", () => { if (past.hidden) { setStatus("Connecting…") } });
  startLive();
  live.play().catch(() => { });

  // Controls on the picture
  const visible = () => (past.hidden ? live : past);
  const mute = $("cpMute");
  mute.addEventListener("click", () => {
    const v = visible();
    v.muted = !v.muted;
    mute.setAttribute("aria-label", v.muted ? "Turn sound on" : "Turn sound off");
    mute.querySelector("i").className = `fas ${v.muted ? "fa-volume-xmark" : "fa-volume-high"}`;
  });
  $("cpSnap").addEventListener("click", () => window.savePhoto?.(visible()));
  $("cpFull").addEventListener("click", () => document.fullscreenElement ? document.exitFullscreen() : cp.requestFullscreen?.().catch(() => { }));

  // Controls fade after a few quiet seconds; a tap on the picture brings them back.
  let calmTimer;
  function wake() {
    cp.classList.remove("calm");
    clearTimeout(calmTimer);
    calmTimer = setTimeout(() => { if (sheetState === "peek") { cp.classList.add("calm") } }, 4000);
  }
  cp.addEventListener("pointermove", (e) => { if (e.pointerType === "mouse") { wake() } });

  // Sheet: peek (rewind line), half (settings and today), full (every setting).
  const grab = $("cpGrab");
  let sheetState = "peek";
  const stops = () => {
    const peek = grab.offsetHeight + rail.parentElement.offsetHeight + 12;
    return { peek: sheet.offsetHeight - peek, half: sheet.offsetHeight * 0.45, full: 0 };
  };
  function setSheet(s) {
    sheetState = s;
    const y = stops()[s];
    sheet.style.setProperty("--y", `${y}px`);
    cp.style.setProperty("--sheet-top", `${cp.offsetHeight - sheet.offsetHeight + y}px`);
    if (s !== "peek") { loadSettings() }
    wake();
  }
  addEventListener("resize", () => setSheet(sheetState));
  let drag = null;
  grab.addEventListener("pointerdown", (e) => { drag = { y: e.clientY, start: stops()[sheetState], moved: false }; sheet.classList.add("dragging"); grab.setPointerCapture(e.pointerId); });
  grab.addEventListener("pointermove", (e) => {
    if (!drag) { return }
    const dy = e.clientY - drag.y;
    if (Math.abs(dy) > 4) { drag.moved = true }
    sheet.style.setProperty("--y", `${Math.max(0, Math.min(stops().peek, drag.start + dy))}px`);
  });
  grab.addEventListener("pointerup", (e) => {
    if (!drag) { return }
    sheet.classList.remove("dragging");
    const y = drag.start + (e.clientY - drag.y), at = stops();
    setSheet(!drag.moved ? { peek: "half", half: "full", full: "peek" }[sheetState]
      : Object.keys(at).reduce((a, k) => (Math.abs(at[k] - y) < Math.abs(at[a] - y) ? k : a), "peek"));
    drag = null;
  });
  document.querySelectorAll("[data-sheet]").forEach((b) => b.addEventListener("click", () => setSheet(b.dataset.sheet)));

  // Taps and swipes on the picture: tap shows the controls (or lowers the sheet), sideways goes to the next camera.
  let touch = null, lastTap = 0;
  cp.addEventListener("pointerdown", (e) => { if (!e.target.closest(".cp-sheet, button, a, input")) { touch = { x: e.clientX, y: e.clientY } } });
  cp.addEventListener("pointerup", (e) => {
    if (!touch) { return }
    const dx = e.clientX - touch.x, dy = e.clientY - touch.y;
    touch = null;
    if (Math.abs(dx) > 70 && Math.abs(dx) > Math.abs(dy) * 1.5) {
      const to = dx < 0 ? cp.dataset.next : cp.dataset.prev;
      if (to) { location.replace(`camera?cam=${encodeURIComponent(to)}`) }
    } else if (Math.abs(dx) < 10 && Math.abs(dy) < 10) {
      if (Date.now() - lastTap < 300) { cp.classList.toggle("fill") }  // double-tap: fill the screen or show the whole picture
      lastTap = Date.now();
      if (sheetState !== "peek") { setSheet("peek") } else { cp.classList.contains("calm") ? wake() : cp.classList.add("calm") }
    }
  });

  // Rewind: the line runs from midnight to now; the playhead in the middle is the moment shown.
  const PX = 1.6;  // pixels per minute
  const midnight = new Date(); midnight.setHours(0, 0, 0, 0);
  const minuteOf = (ts) => (ts * 1000 - midnight) / 60000;
  const dateOf = (m) => new Date(+midnight + m * 60000);
  const nowMin = () => minuteOf(Date.now() / 1000);
  let events = [], segments = [], end = nowMin(), quiet = false;
  const maxScroll = () => rail.scrollWidth - rail.clientWidth;
  const atMinute = () => end - (maxScroll() - rail.scrollLeft) / PX;
  const isLive = () => end - atMinute() < 1.5;
  function drawTrack() {
    end = nowMin();
    track.style.width = `${end * PX}px`;
    track.querySelectorAll(".cp-h, .cp-rec, .cp-mk").forEach((n) => n.remove());
    for (let h = 3; h * 60 < end - 30; h += 3) {
      const s = Object.assign(document.createElement("span"), { className: "cp-h", textContent: fmt(dateOf(h * 60)).replace(":00", "") });
      s.style.left = `${h * 60 * PX}px`;
      track.append(s);
    }
    segments.forEach((s) => {
      const a = Math.max(0, minuteOf(s.from)), b = Math.min(end, minuteOf(s.to));
      if (b <= 0 || a >= end) { return }
      const r = Object.assign(document.createElement("span"), { className: "cp-rec" });
      Object.assign(r.style, { left: `${a * PX}px`, width: `${Math.max(2, (b - a) * PX)}px` });
      track.append(r);
    });
    events.forEach((ev) => {
      const b = Object.assign(document.createElement("button"), { type: "button", className: `cp-mk ${ev.kind}` });
      b.style.left = `${minuteOf(ev.ts) * PX}px`;
      b.setAttribute("aria-label", `${ev.title} at ${fmt(new Date(ev.ts * 1000))}`);
      b.addEventListener("click", () => jump(minuteOf(ev.ts)));
      track.append(b);
    });
  }
  function readout() {
    const m = atMinute(), near = events.find((ev) => Math.abs(minuteOf(ev.ts) - m) < 2);
    $("cpClock").textContent = fmt(dateOf(isLive() ? end : m));
    $("cpWhat").textContent = isLive() ? "Live. Drag the line to go back." : near ? near.title : "Let go to watch this moment";
  }
  function jump(m) {
    quiet = false;
    rail.scrollTo({ left: maxScroll() - (end - m) * PX, behavior: still ? "auto" : "smooth" });
  }
  let settle;
  rail.addEventListener("scroll", () => {
    readout();
    if (quiet) { return }
    clearTimeout(settle);
    settle = setTimeout(() => (isLive() ? goLive() : showMoment(atMinute())), 350);
  }, { passive: true });
  let mouse = null;  // mouse drag on desktops; fingers already scroll
  rail.addEventListener("pointerdown", (e) => { if (e.pointerType === "mouse" && !e.target.closest(".cp-mk")) { mouse = { x: e.clientX, left: rail.scrollLeft }; rail.setPointerCapture(e.pointerId) } });
  rail.addEventListener("pointermove", (e) => { if (mouse) { rail.scrollLeft = mouse.left - (e.clientX - mouse.x) } });
  rail.addEventListener("pointerup", () => { mouse = null });

  const goLiveBtn = $("cpGoLive");
  function goLive() {
    past.pause(); past.removeAttribute("src"); past.removeAttribute("poster"); past.load(); past.hidden = true;
    note.hidden = true; goLiveBtn.hidden = true;
    setStatus(live.paused ? "Connecting…" : ["Live", cp.dataset.net].filter(Boolean).join(" · "), live.paused ? "" : "live");
    if (!isLive()) { quiet = true; rail.scrollLeft = maxScroll(); readout() }
  }
  goLiveBtn.addEventListener("click", goLive);
  function showMoment(m) {
    const when = dateOf(m), ts = when / 1000;
    const seg = segments.find((s) => s.from <= ts && ts < s.to);
    const near = events.filter((ev) => Math.abs(ev.ts - ts) < 120).sort((a, b) => Math.abs(a.ts - ts) - Math.abs(b.ts - ts))[0];
    past.removeAttribute("poster");
    note.hidden = true;
    if (seg) {
      past.src = `recordings/${cam}/play?start=${encodeURIComponent(when.toISOString())}&duration=${Math.ceil(Math.min(600, seg.to - ts))}`;
    } else if (near?.clip) {
      past.src = `history/media/${near.id}/clip`;
    } else if (near?.img) {
      past.removeAttribute("src");
      past.poster = `history/media/${near.id}/img`;
      note.textContent = `No video at ${fmt(when)}. This is the picture Wyze saved.`;
      note.hidden = false;
    } else {
      past.removeAttribute("src");
      note.textContent = `Nothing was recorded at ${fmt(when)}.` + (cp.dataset.playback ? "" : " Turn on recording for this camera in Recordings to watch any moment.");
      note.hidden = false;
    }
    past.load();
    past.hidden = false;
    past.muted = live.muted;
    if (past.getAttribute("src")) { past.play().catch(() => { }) }
    goLiveBtn.hidden = false;
    setStatus(`Rewound to ${fmt(when)}`, "past");
  }

  // Today's events from the archive, newest first. Runs of plain motion fold into one row.
  const KIND = { person: "person", package: "person", vehicle: "person", pet: "person", "rang the doorbell": "person" };
  const TITLE = { motion: "Motion", person: "Person", package: "Package", vehicle: "Vehicle", pet: "Pet", "rang the doorbell": "Doorbell" };
  const SOUNDS = ["crying", "barking", "meowing", "talking", "glass", "gunshot", "smoke", "co", "sound"];
  function loadEvents() {
    fetch(`history/events?device=${encodeURIComponent(name)}&since=${Math.floor(midnight / 1000)}&limit=200`).then((r) => r.json()).then((data) => {
      events = (data.events || []).map((ev) => ({ ...ev, kind: KIND[ev.type] || (SOUNDS.includes(ev.type) ? "sound" : "motion"),
        title: TITLE[ev.type] || ev.type[0].toUpperCase() + ev.type.slice(1) }));
      drawTrack(); readout(); feed();
    }).catch(() => { $("cpFeed").innerHTML = `<li class="cp-empty">Couldn't load today's events.</li>` });
  }
  function feed() {
    const list = $("cpFeed");
    list.replaceChildren();
    const rows = [];
    events.forEach((ev) => {
      const last = rows[rows.length - 1];
      if (ev.kind === "motion" && last?.[0].kind === "motion" && last[last.length - 1].ts - ev.ts < 600) { last.push(ev) } else { rows.push([ev]) }
    });
    const people = events.filter((e) => e.kind === "person").length;
    $("cpCount").textContent = events.length ? `${events.length} today${people ? `, ${people} with people` : ""}` : "";
    const mins = events.length ? Math.round((Date.now() / 1000 - events[0].ts) / 60) : 0;
    $("cpQuiet").textContent = !events.length ? "Nothing today" : mins < 2 ? "Something just happened" : mins < 90 ? `Quiet for ${mins} min` : `Quiet since ${fmt(new Date(events[0].ts * 1000))}`;
    if (!rows.length) { list.innerHTML = `<li class="cp-empty">Nothing has happened today.</li>`; return }
    rows.forEach((group) => {
      const ev = group[0], li = document.createElement("li"), b = document.createElement("button");
      b.type = "button";
      b.className = `cp-ev ${ev.kind === "person" ? "big" : ""}`;
      const pic = ev.img ? `<img src="history/media/${ev.id}/img" alt="" loading="lazy">` : `<span class="cp-noimg"><i class="fas fa-image"></i></span>`;
      const time = group.length > 1 ? `${fmt(new Date(group[group.length - 1].ts * 1000))} to ${fmt(new Date(ev.ts * 1000))}` : fmt(new Date(ev.ts * 1000));
      b.innerHTML = `${pic}<span class="cp-ev-t"><b></b><span class="cp-time">${time}</span></span><span class="cp-tag ${ev.kind}"></span>`;
      b.querySelector("b").textContent = group.length > 1 ? `${group.length} motions` : ev.title;
      b.querySelector(".cp-tag").textContent = ev.clip ? "Clip" : ev.title;
      b.addEventListener("click", () => { jump(minuteOf(ev.ts)); setSheet("peek") });
      li.append(b);
      list.append(li);
    });
  }
  function loadRecordings() {
    if (!cp.dataset.playback) { return Promise.resolve() }
    return fetch(`recordings/${cam}`).then((r) => (r.ok ? r.json() : [])).then((list) => {
      segments = list.map((s) => { const from = Date.parse(s.start) / 1000; return { from, to: from + s.duration } });
    }).catch(() => { });
  }
  loadRecordings().then(() => { drawTrack(); quiet = true; rail.scrollLeft = maxScroll(); readout(); setSheet("peek") });
  loadEvents();
  setInterval(() => {  // the line grows with the clock; new events show up without a reload
    const follow = isLive();
    drawTrack();
    if (follow) { quiet = true; rail.scrollLeft = maxScroll() }
    readout();
  }, 30000);
  setInterval(() => { loadEvents(); loadRecordings() }, 120000);

  // Settings: each one shows what the camera is set to now. Unknown shows nothing chosen.
  if (cp.dataset.control) {
    let values = {};
    const word = (v) => ({ on: "On", off: "Off", auto: "Auto" })[v] || "Not known";
    function render() {
      document.querySelectorAll(".cp-tile[data-set]").forEach((t) => {
        const v = values[t.dataset.set];
        t.dataset.state = v || "";
        t.querySelector("[data-value]").textContent = word(v);
      });
      document.querySelectorAll(".seg[data-set]").forEach((g) => {
        const v = values[g.dataset.set], buttons = [...g.querySelectorAll("button")];
        // Sensitivity is 1-100 on the camera, so the nearest of Low/Med/High is chosen.
        const pick = g.dataset.set.endsWith("_sensitivity") && v !== undefined
          ? buttons.reduce((a, b) => (Math.abs(b.dataset.v - v) < Math.abs(a.dataset.v - v) ? b : a))
          : buttons.find((b) => b.dataset.v === v);
        buttons.forEach((b) => b.setAttribute("aria-pressed", b === pick));
      });
      document.querySelectorAll("[data-exact]").forEach((s) => { const v = values[s.dataset.exact]; s.textContent = v === undefined ? "" : `The camera is at ${v} of 100` });
      document.querySelectorAll(".cp-switch[data-set]").forEach((s) => {
        const v = values[s.dataset.set];
        s.setAttribute("aria-checked", v === "on" ? "true" : v === "off" ? "false" : "mixed");
      });
    }
    let loaded = 0;
    loadSettings = () => {
      if (Date.now() - loaded < 30000) { return }
      loaded = Date.now();
      fetch(`camera-settings/${cam}/current`).then((r) => (r.ok ? r.json() : {})).then((v) => { values = { ...values, ...v }; render() }).catch(() => { });
    };
    function send(cmd, value, el) {
      el?.classList.add("is-loading");
      return fetch(`api/${cam}/${cmd}/${value}`).then((r) => r.json()).then((data) => {
        if (["error", false].includes(data.status) || data.error) { throw new Error(data.response || data.error || "") }
        values[cmd] = value;
        render();
        return true;
      }).catch((err) => { say(`That didn't work. ${err.message}`.trim(), "danger"); return false })
        .finally(() => el?.classList.remove("is-loading"));
    }
    document.querySelectorAll(".cp-tile[data-set]").forEach((t) => t.addEventListener("click", () => {
      const opts = t.dataset.cycle.split(","), v = values[t.dataset.set];
      send(t.dataset.set, opts[(opts.indexOf(v) + 1) % opts.length], t);
    }));
    document.querySelectorAll(".seg[data-set] button").forEach((b) => b.addEventListener("click", () => send(b.parentElement.dataset.set, b.dataset.v, b)));
    document.querySelectorAll(".cp-switch[data-set]").forEach((s) => s.addEventListener("click", () => send(s.dataset.set, values[s.dataset.set] === "on" ? "off" : "on", s)));
    loadSettings();

    // Siren: slide all the way to sound it, tap again to stop. Nothing goes off from a stray tap.
    const siren = $("cpSiren"), range = $("cpSirenRange"), knob = siren.querySelector(".cp-knob"), text = siren.querySelector("[data-text]");
    const travel = () => siren.clientWidth - knob.offsetWidth - 8;
    const reset = () => { knob.style.transform = ""; range.value = 0 };
    range.addEventListener("input", () => { if (!siren.classList.contains("loud")) { knob.style.transform = `translateX(${(range.value / 100) * travel()}px)` } });
    range.addEventListener("change", async () => {
      if (siren.classList.contains("loud")) {
        siren.classList.remove("loud"); text.textContent = "Slide to sound the siren"; reset();
        return send("alarm", "off");
      }
      if (+range.value < 92) { return reset() }
      siren.classList.add("loud"); text.textContent = "Siren on. Tap to stop."; knob.style.transform = `translateX(${travel()}px)`;
      if (!(await send("alarm", "on"))) { siren.classList.remove("loud"); text.textContent = "Slide to sound the siren"; reset() }
    });
  }
})();
