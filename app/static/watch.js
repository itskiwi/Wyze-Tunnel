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
  let events = [], groups = [], span = 7200, at = null; // at is the rewind time, null while live
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

  // Playback: a recording or event clip from the rewind time.
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

  // The latest visit today (one person or car across cameras, grouped by event_archive) as the Events tab's card.
  function drawVisit() {
    const box = $("#visit"), v = groups.find((g) => Object.keys(g.cameras).length > 1 && g.first >= midnight());
    box.classList.toggle("hidden", !v);
    if (!v) return box.replaceChildren();
    const byId = Object.fromEntries(events.map((e) => [e.id, e]));
    box.replaceChildren(WTEvents.visit(v, byId, { owner: box.dataset.owner === "1" }));
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
    const tz = new Date().getTimezoneOffset(), get = (q) => fetch(`history/events?${q}&tz=${tz}`).then((r) => r.ok ? r.json() : Promise.reject());
    // shortcut: the newest 300 events of the last day; a very busy day loses its oldest marks on the 24h view.
    Promise.all([get(`since=${Math.floor(now() - 86400)}&limit=300`), get(`since=${midnight()}&limit=1`)]).then(([data, today]) => {
      events = data.events;
      groups = data.groups;
      const counts = today.counts;
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
