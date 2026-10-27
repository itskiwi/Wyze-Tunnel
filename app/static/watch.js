// Live page's monitoring view: one line on what's going on, the camera that saw something last shown big,
// one scrubber that rewinds every camera at once, the latest visit across cameras, and today's counts.
(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const cams = [...document.querySelectorAll(".watch-cam")].map((el) => ({ uri: el.dataset.cam, name: el.dataset.name, rec: el.dataset.rec === "1", img: el.dataset.img, el }));
  const byName = new Map(cams.map((c) => [c.name, c]));
  const NOTABLE = ["person", "package", "vehicle", "pet", "rang the doorbell"];
  const ORDER = [...NOTABLE, "motion"];
  const NAMES = { person: ["person", "people"], package: ["package", "packages"], vehicle: ["car", "cars"], pet: ["pet", "pets"],
    "rang the doorbell": ["doorbell ring", "doorbell rings"], motion: ["motion alert", "motion alerts"] };
  const VISIT_GAP = 180; // seconds between sightings on different cameras that still count as one visit
  let events = [], span = 7200, at = null; // at is the rewind time, null while live
  let pinned = null;
  try { pinned = localStorage.getItem("watchPin"); } catch { }

  const now = () => Date.now() / 1000;
  const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const ago = (ts) => { const m = Math.round((now() - ts) / 60); return m < 1 ? "just now" : m < 60 ? `${m} min ago` : `at ${clock(ts)}`; };
  const iso = (ts) => new Date(ts * 1000).toISOString();
  const midnight = () => new Date().setHours(0, 0, 0, 0) / 1000;
  const pic = (ev) => ev && ev.img ? `history/media/${ev.id}/img` : "";
  const sentence = (text) => String(text || "").replace(/\.$/, "");

  function story() {
    const today = events.filter((e) => e.ts >= midnight());
    const last = today.find((e) => NOTABLE.includes(e.type)); // events come newest first
    const h = $("#watchStory"), p = $("#watchDetail");
    if (!last) {
      h.textContent = "All quiet today";
      p.textContent = today.length ? `Only ${today.length} motion ${today.length === 1 ? "alert" : "alerts"}. No people, packages or cars.` : "Nothing has moved since midnight.";
    } else if (now() - last.ts < 180) {
      h.textContent = sentence(last.text);
      p.textContent = `${last.device}, ${ago(last.ts)}.`;
    } else {
      h.textContent = "Quiet now";
      p.textContent = `Last: ${sentence(last.text)}, ${ago(last.ts)}.`;
    }
  }

  function spotlight() {
    const recent = events.find((e) => now() - e.ts < 1800 && byName.has(e.device));
    const uri = cams.some((c) => c.uri === pinned) ? pinned : recent && byName.get(recent.device).uri;
    document.querySelectorAll(".cameras > .camera.spotlight").forEach((el) => el.classList.remove("spotlight"));
    if (uri && cams.length > 1) document.getElementById(uri)?.classList.add("spotlight");
    cams.forEach((c) => {
      c.el.setAttribute("aria-pressed", c.uri === pinned);
      c.el.classList.toggle("seen", events.some((e) => e.device === c.name && e.type === "person" && now() - e.ts < 900));
    });
  }

  cams.forEach((c) => c.el.addEventListener("click", () => {
    pinned = pinned === c.uri ? null : c.uri;
    try { pinned ? localStorage.setItem("watchPin", pinned) : localStorage.removeItem("watchPin"); } catch { }
    spotlight();
    document.getElementById(c.uri)?.scrollIntoView({ behavior: "smooth", block: "start" });
  }));

  // Rewind: one line for the whole house; the pictures above it show every camera at that moment.
  const lane = $("#rwLane"), ticks = $(".rw-ticks", lane), head = $(".rw-head", lane), film = $("#rwFilm");
  const x = (ts) => Math.min(100, Math.max(0, 100 - (now() - ts) / span * 100));

  function drawLane() {
    ticks.replaceChildren(...events.filter((e) => now() - e.ts <= span).map((e) => {
      const i = document.createElement("i");
      i.dataset.type = e.type;
      i.style.left = `${x(e.ts)}%`;
      i.title = `${sentence(e.text)}, ${clock(e.ts)}`;
      return i;
    }));
    $("#rwAxis").replaceChildren(...[4, 3, 2, 1, 0].map((k) => {
      const s = document.createElement("span");
      s.textContent = k ? clock(now() - span * k / 4) : "now";
      return s;
    }));
    moveHead();
  }

  function moveHead() {
    const left = x(at ?? now());
    head.style.left = `${left}%`;
    head.firstElementChild.style.transform = `translateX(${left > 88 ? -88 : left < 12 ? -12 : -50}%)`; // label stays inside the card
    head.firstElementChild.textContent = at ? clock(at) : "Live";
    lane.classList.toggle("at-live", !at);
    lane.setAttribute("aria-valuetext", at ? clock(at) : "Live");
  }

  function nearest(cam, t) { // that camera's saved event closest to t, within 5 minutes
    let best = null;
    for (const e of events) {
      if (e.device === cam.name && Math.abs(e.ts - t) <= 300 && (!best || Math.abs(e.ts - t) < Math.abs(best.ts - t))) best = e;
    }
    return best;
  }

  function drawFilm() {
    film.replaceChildren(...cams.map((c) => {
      const b = document.createElement("button"), label = document.createElement("span"), ev = at && nearest(c, at);
      b.type = "button";
      b.dataset.cam = c.uri;
      label.textContent = c.name;
      b.append(label);
      const empty = () => { b.classList.add("empty"); b.title = `Nothing saved from ${c.name} then`; };
      const fallback = pic(ev);
      const src = !at ? c.img : c.rec ? `recordings/${c.uri}/thumb?start=${encodeURIComponent(iso(at))}` : fallback;
      if (!src) { if (at) empty(); return b; }
      const im = new Image();
      im.alt = "";
      im.onerror = () => {
        if (fallback && !im.src.endsWith(fallback)) im.src = fallback;
        else { im.remove(); empty(); }
      };
      im.src = src;
      b.prepend(im);
      return b;
    }));
  }

  let filmTimer;
  const settle = () => { clearTimeout(filmTimer); filmTimer = setTimeout(drawFilm, 250); };
  const goTo = (t) => { at = t && now() - t > 30 ? Math.round(t) : null; moveHead(); settle(); };

  function fromPointer(e) {
    const r = lane.getBoundingClientRect(), f = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
    goTo(f > .985 ? null : now() - (1 - f) * span);
  }
  lane.addEventListener("pointerdown", (e) => { lane.setPointerCapture(e.pointerId); fromPointer(e); });
  lane.addEventListener("pointermove", (e) => lane.hasPointerCapture(e.pointerId) && fromPointer(e));
  lane.addEventListener("keydown", (e) => {
    const step = { ArrowLeft: -span / 60, ArrowRight: span / 60 }[e.key];
    if (step) { e.preventDefault(); goTo(Math.max(now() - span, (at ?? now()) + step)); }
    else if (e.key === "End") goTo(null);
  });

  document.querySelectorAll("#rewind [data-step]").forEach((b) => b.addEventListener("click", () => {
    const t = at ?? now(), times = events.filter((e) => NOTABLE.includes(e.type) && now() - e.ts <= span).map((e) => e.ts).sort((a, b) => a - b);
    goTo(+b.dataset.step < 0 ? times.filter((ts) => ts < t - 1).pop() ?? at : times.find((ts) => ts > t + 1) ?? null);
  }));
  $("#rwLive").addEventListener("click", () => goTo(null));
  document.querySelectorAll("#rewind [data-span]").forEach((b, _, all) => b.addEventListener("click", () => {
    span = +b.dataset.span;
    all.forEach((o) => o.setAttribute("aria-checked", o === b));
    if (at && now() - at > span) at = null;
    drawLane();
    settle();
  }));

  // Playback: a recording from the rewind time, or a visit's saved clips one after another.
  const dialog = $("#rwPlayer"), video = $("video", dialog);
  let queue = [];
  function playNext() {
    const item = queue.shift();
    if (!item) return;
    video.src = item.src;
    $("[data-label]", dialog).textContent = item.label;
    video.play().catch(() => { });
  }
  const play = (list) => { queue = list; playNext(); dialog.showModal(); };
  video.addEventListener("ended", playNext);
  $("[data-close]", dialog).addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => { queue = []; video.pause(); video.removeAttribute("src"); video.load(); });

  film.addEventListener("click", (e) => {
    const b = e.target.closest("button"), c = b && cams.find((c) => c.uri === b.dataset.cam);
    if (!c) return;
    if (!at) return c.el.click(); // live: show that camera big
    const ev = nearest(c, at);
    if (c.rec) play([{ src: `recordings/${c.uri}/play?start=${encodeURIComponent(iso(at - 5))}&duration=120`, label: `${c.name}, ${clock(at)}` }]);
    else if (ev && ev.clip) play([{ src: `history/media/${ev.id}/clip`, label: `${c.name}, ${clock(ev.ts)}` }]);
  });

  // shortcut: visits are grouped here from the last 200 events; move to the shared event_archive grouping when it lands.
  function visits() {
    const seen = events.filter((e) => (e.type === "person" || e.type === "rang the doorbell") && byName.has(e.device)).reverse();
    const out = [];
    let cur = [];
    for (const e of seen) {
      if (cur.length && e.ts - cur[cur.length - 1].ts > VISIT_GAP) { out.push(cur); cur = []; }
      cur.push(e);
    }
    if (cur.length) out.push(cur);
    return out.filter((v) => new Set(v.map((e) => e.device)).size > 1).reverse();
  }

  function drawVisit() {
    const box = $("#visit"), v = visits()[0];
    box.classList.toggle("hidden", !v || v[0].ts < midnight());
    if (box.classList.contains("hidden")) return;
    const steps = [];
    for (const e of v) {
      const last = steps[steps.length - 1];
      if (last && last.device === e.device) { last.end = e.ts; last.evs.push(e); }
      else steps.push({ device: e.device, start: e.ts, end: e.ts, evs: [e] });
    }
    const mins = Math.max(1, Math.round((v[v.length - 1].ts - v[0].ts) / 60));
    $(".visit-kicker", box).textContent = `One visit, ${new Set(v.map((e) => e.device)).size} cameras, ${ago(v[0].ts)}`;
    $("[data-title]", box).textContent = `From ${steps[0].device} to ${steps[steps.length - 1].device} in ${mins} min`;
    $(".visit-steps", box).replaceChildren(...steps.map((s) => {
      const li = document.createElement("li"), frame = document.createElement("span"), text = document.createElement("span"), time = document.createElement("time");
      frame.className = "pic";
      const src = pic(s.evs.find((e) => e.img));
      if (src) { const im = new Image(); im.alt = ""; im.src = src; frame.append(im); }
      const small = document.createElement("small");
      small.textContent = sentence(s.evs[0].text) + (s.end - s.start >= 20 ? `, there ${Math.round((s.end - s.start) / 60) || 1} min` : "");
      text.append(s.device, small);
      time.textContent = clock(s.start);
      li.append(frame, text, time);
      return li;
    }));
    const clips = v.filter((e) => e.clip).map((e) => ({ src: `history/media/${e.id}/clip`, label: `${e.device}, ${clock(e.ts)}` }));
    const btn = $("[data-play]", box);
    btn.classList.toggle("hidden", !clips.length);
    btn.onclick = () => play(clips);
  }

  function drawQuiet(counts) {
    const rows = Object.entries(counts).sort(([a], [b]) => (ORDER.indexOf(a) + 1 || 99) - (ORDER.indexOf(b) + 1 || 99));
    $("#quiet").classList.toggle("hidden", !rows.length);
    $(".quiet-list").replaceChildren(...rows.map(([type, n]) => {
      const li = document.createElement("li"), a = document.createElement("a"), dot = document.createElement("i"), text = document.createElement("span"), b = document.createElement("b");
      const last = events.find((e) => e.type === type && e.ts >= midnight());
      a.href = `history?type=${encodeURIComponent(type)}&since=${midnight()}`;
      dot.dataset.type = type;
      b.textContent = `${n} ${(NAMES[type] || [type, type])[n === 1 ? 0 : 1]}`;
      text.append(b, last ? `, last on ${last.device} ${ago(last.ts)}` : "");
      a.append(dot, text);
      li.append(a);
      return li;
    }));
  }

  const modes = [...document.querySelectorAll("#watchMode [data-mode]")], modeError = $("#watchModeError");
  modes.forEach((button) => button.addEventListener("click", () => {
    fetch("alerts/mode", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode: button.dataset.mode }) })
      .then((r) => r.ok ? r.json() : r.json().then((j) => Promise.reject(j.error)))
      .then(() => { modes.forEach((b) => b.setAttribute("aria-checked", b === button)); modeError.classList.add("hidden"); })
      .catch((error) => { modeError.textContent = typeof error === "string" ? error : "Couldn't change the alert mode."; modeError.classList.remove("hidden"); });
  }));

  function load() {
    // shortcut: the newest 200 events of the last day; a very busy day loses its oldest marks on the 24h view.
    Promise.all([
      fetch(`history/events?since=${Math.floor(now() - 86400)}&limit=200`).then((r) => r.ok ? r.json() : Promise.reject()),
      fetch(`history/counts?since=${midnight()}`).then((r) => r.ok ? r.json() : {}),
    ]).then(([data, counts]) => {
      events = data.events;
      story();
      spotlight();
      drawLane();
      if (!at) drawFilm();
      drawVisit();
      drawQuiet(counts);
    }).catch(() => {
      $("#watchStory").textContent = "Your cameras";
      $("#watchDetail").textContent = "Couldn't load today's events. Live video still works below.";
      drawLane();
      drawFilm();
    });
  }
  load();
  setInterval(load, 60 * 1000);
})();
