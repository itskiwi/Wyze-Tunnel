// Event rows and the event player, shared by the Events tab and any page that lists camera events.
// Rows come from history/events: events plus groups (repeats on one camera and visits across cameras, folded by event_archive.group).
(() => {
  const SOUNDS = ["crying", "barking", "meowing", "talking", "glass", "gunshot"];
  // type -> [one, many, icon, color class]; anything else is plain motion
  const KINDS = {
    person: ["Person", "People", "fa-person-walking", "k-person"],
    vehicle: ["Vehicle", "Vehicles", "fa-car", "k-vehicle"],
    pet: ["Pet", "Pets", "fa-paw", "k-pet"],
    package: ["Package", "Packages", "fa-box", "k-package"],
    "rang the doorbell": ["Doorbell", "Doorbell", "fa-bell", "k-ring"],
    smoke: ["Smoke alarm", "Smoke alarms", "fa-triangle-exclamation", "k-alarm"],
    co: ["CO alarm", "CO alarms", "fa-triangle-exclamation", "k-alarm"],
    contact: ["Door", "Doors", "fa-door-open", "k-door"],
    motion: ["Motion", "Motion", "fa-wave-square", "k-motion"],
  };
  for (const s of SOUNDS) KINDS[s] = [s[0].toUpperCase() + s.slice(1), s[0].toUpperCase() + s.slice(1), "fa-volume-high", "k-sound"];
  const kind = (type) => KINDS[type] || KINDS.motion;
  // what the recap leads with: things people act on before pets and plain movement
  const WEIGHT = { smoke: 9, co: 9, package: 8, "rang the doorbell": 7, person: 6, vehicle: 5, glass: 5, gunshot: 5, contact: 3 };
  const weight = (type) => WEIGHT[type] ?? (SOUNDS.includes(type) ? 4 : type === "pet" ? 2 : 1);

  const h = (tag, cls = "", text = "") => Object.assign(document.createElement(tag), { className: cls, textContent: text });
  const icon = (name) => { const i = h("i", `fas ${name}`); i.setAttribute("aria-hidden", "true"); return i; };
  const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  // "9:31–9:47 PM" rather than "9:31 PM–9:47 PM"
  const span = (a, b) => {
    const [x, y] = [clock(a), clock(b)], [, sx] = x.split(" "), [, sy] = y.split(" ");
    return `${sx && sx === sy ? x.slice(0, -sx.length - 1) : x}–${y}`;
  };
  const length = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  const media = (id, k) => `history/media/${id}/${k}`;

  function tag(type) {
    const [one, , ico, cls] = kind(type);
    const t = h("span", `ev-tag ${cls}`);
    t.append(icon(ico), one);
    return t;
  }

  function thumb(e, cls = "ev-thumb") {
    const box = h("div", cls);
    if (e.img) box.append(Object.assign(h("img"), { src: media(e.id, "img"), loading: "lazy", alt: "" }));
    else box.append(icon(kind(e.type)[2]));
    if (e.clip) {
      const p = h("span", "ev-len");
      p.append(icon("fa-play"), e.dur ? length(e.dur) : "");
      box.append(p);
    }
    return box;
  }

  // One row per group. seen: events newer than this get the "new" dot.
  function row(g, byId, seen = Infinity) {
    const lead = byId[g.ids[0]], cams = Object.keys(g.cameras);
    const r = h("button", "ev-row" + (g.count > 1 ? " ev-stack" : ""));
    r.type = "button";
    r.dataset.ids = g.ids.join(",");
    const meta = h("span", "ev-meta"), name = h("span", "ev-name"), tags = h("span", "ev-tags");
    if (g.last > seen) name.append(Object.assign(h("i", "ev-new"), { title: "New since you last looked" }));
    name.append(cams.join(" → "));
    if (g.ids.some((id) => byId[id]?.kept)) name.append(Object.assign(icon("fa-star ev-kept"), { title: "Kept" }));
    const types = [...new Set([lead.type, ...(lead.tags || [])])].filter((t) => t !== "motion" || lead.type === "motion");
    tags.append(...types.map(tag));
    if (g.count > 1) tags.append(h("span", "ev-more", `+${g.count - 1} more`));
    meta.append(name, tags);
    const when = h("span", "ev-time", g.count > 1 && g.last - g.first >= 60 ? span(g.first, g.last) : clock(g.last));
    r.append(thumb(lead), meta, when);
    r.setAttribute("aria-label", `${lead.text}, ${when.textContent}${g.count > 1 ? `, ${g.count} events` : ""}`);
    return r;
  }

  // Full screen player. list: events oldest to newest. opts.review shows Seen and Keep and moves on by itself; opts.auto just moves on.
  function play(list, start = 0, opts = {}) {
    let i = start, touchX = null;
    const owner = !!opts.owner;
    const box = h("div", "ev-player" + (opts.review ? " is-review" : ""));
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    box.setAttribute("aria-label", opts.review ? "Review new events" : "Event");
    const segs = h("div", "ev-segs");
    const top = h("div", "ev-ptop"), title = h("div", "ev-ptitle"), name = h("b"), sub = h("small");
    const live = Object.assign(h("a", "round-btn", ""), { title: "Watch live" });
    live.append(icon("fa-tower-broadcast"));
    const close = Object.assign(h("button", "round-btn"), { type: "button", title: "Close" });
    close.append(icon("fa-xmark"));
    title.append(name, sub);
    top.append(title, live, close);
    const stage = h("div", "ev-stage"), cap = h("div", "ev-cap"), said = h("p", "ev-said"), tags = h("div", "ev-tags");
    cap.append(said, tags);
    const nav = h("div", "ev-pnav");
    const btn = (ico, label, cls = "") => { const b = h("button", `ev-pbtn ${cls}`); b.type = "button"; b.append(icon(ico), label); return b; };
    const prev = btn("fa-backward", ""), next = btn("fa-forward", "");
    prev.title = "Earlier event"; next.title = "Later event";
    const seenBtn = btn("fa-check", "Seen"), keepBtn = btn("fa-star", "Keep", "ev-keep");
    const save = Object.assign(h("a", "ev-pbtn"), { title: "Save clip" });
    save.append(icon("fa-download"), "Save");
    const del = btn("fa-trash", "Delete", "ev-del");
    if (opts.review) nav.append(seenBtn, keepBtn);
    else nav.append(prev, ...(owner ? [keepBtn] : []), save, ...(owner ? [del] : []), next);
    box.append(segs, top, stage, cap, nav);
    if (opts.review) segs.append(...list.map(() => h("i")));

    function show() {
      const e = list[i];
      name.textContent = e.device;
      sub.textContent = `${new Date(e.ts * 1000).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })} · ${clock(e.ts)}` +
        (list.length > 1 ? ` · ${i + 1} of ${list.length}` : "");
      live.hidden = !e.uri;
      live.href = `live#${e.uri}`;
      said.textContent = e.text;
      tags.replaceChildren(...[...new Set([e.type, ...(e.tags || [])])].filter((t) => t !== "motion" || e.type === "motion").map(tag));
      stage.replaceChildren();
      if (e.clip) {
        const v = Object.assign(h("video"), { src: media(e.id, "clip"), controls: true, autoplay: true, playsInline: true, muted: !!opts.auto });
        if (e.img) v.poster = media(e.id, "img");
        v.onended = () => (opts.review || opts.auto) && step(1);
        stage.append(v);
      } else if (e.img) stage.append(Object.assign(h("img"), { src: media(e.id, "img"), alt: e.text }));
      else stage.append(icon(kind(e.type)[2]));
      save.hidden = !e.clip;
      save.href = media(e.id, "clip");
      save.download = `${e.device} ${new Date(e.ts * 1000).toISOString().slice(0, 16).replace("T", " ")}.mp4`;
      keepBtn.classList.toggle("is-on", !!e.kept);
      keepBtn.lastChild.textContent = !opts.review && e.kept ? "Kept" : "Keep";
      prev.disabled = i === 0;
      next.disabled = i === list.length - 1;
      [...segs.children].forEach((s, j) => (s.className = j < i ? "done" : j === i ? "cur" : ""));
    }
    function step(d) {
      if (i + d >= 0 && i + d < list.length) { i += d; show(); }
      else if (d > 0 && (opts.review || opts.auto)) done();
    }
    function done() {
      box.remove();
      document.removeEventListener("keydown", keys);
      document.body.classList.remove("ev-open");
      opts.onClose?.(list);
    }
    function keys(ev) {
      if (ev.key === "Escape") done();
      else if (ev.key === "ArrowRight" || ev.key === "ArrowDown") step(1);
      else if (ev.key === "ArrowLeft" || ev.key === "ArrowUp") step(-1);
      else return;
      ev.preventDefault();
    }
    async function setKept(kept) {
      const e = list[i];
      const resp = await fetch(`history/${e.id}/keep`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kept }) });
      if (resp.ok) { e.kept = kept; opts.onChange?.(e); }
      return resp.ok;
    }
    close.onclick = done;
    prev.onclick = () => step(-1);
    next.onclick = () => step(1);
    seenBtn.onclick = () => step(1);
    keepBtn.onclick = async () => {
      if (await setKept(opts.review || !list[i].kept)) opts.review ? step(1) : show();
    };
    let armed = false;
    del.onclick = async () => {
      if (!armed) { armed = true; del.lastChild.textContent = "Tap again"; setTimeout(() => { armed = false; del.lastChild.textContent = "Delete"; }, 3000); return; }
      armed = false;
      const e = list[i];
      if (!(await fetch(`history/${e.id}/delete`, { method: "POST" })).ok) return;
      opts.onDelete?.(e);
      list.splice(i, 1);
      if (!list.length) return done();
      i = Math.min(i, list.length - 1);
      del.lastChild.textContent = "Delete";
      show();
    };
    stage.addEventListener("touchstart", (ev) => (touchX = ev.touches[0].clientX), { passive: true });
    stage.addEventListener("touchend", (ev) => {
      const dx = ev.changedTouches[0].clientX - touchX;
      if (touchX !== null && Math.abs(dx) > 60) step(dx < 0 ? 1 : -1);
      touchX = null;
    });
    document.addEventListener("keydown", keys);
    document.body.classList.add("ev-open");
    document.body.append(box);
    show();
    close.focus();
    return { close: done };
  }

  window.WTEvents = { KINDS, kind, weight, tag, thumb, row, play, clock, span, length, h, icon };
})();
