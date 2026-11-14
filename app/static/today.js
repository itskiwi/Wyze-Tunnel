// The one line on today that Home and Live both show, so the two pages can't disagree.
// events: newest first, each { ts (Unix seconds), kind ("person", "motion", ...), text, place }.
function daySummary(events, nowMs = Date.now()) {
    const now = nowMs / 1000, midnight = new Date(nowMs).setHours(0, 0, 0, 0) / 1000;
    const today = events.filter((e) => e.ts >= midnight);
    const last = today.find((e) => ["person", "package", "vehicle", "pet", "rang the doorbell"].includes(e.kind));
    const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    const mins = last && Math.round((now - last.ts) / 60);
    const ago = !last ? "" : mins < 1 ? "just now" : mins < 60 ? `${mins} min ago` : `at ${clock(last.ts)}`;
    const text = String(last?.text || "").replace(/\.$/, "");
    if (!last) return { title: "All quiet today", detail: today.length
        ? `Only ${today.length} motion ${today.length === 1 ? "alert" : "alerts"}. No people, packages or cars.` : "Nothing has moved since midnight." };
    if (now - last.ts < 180) return { title: text, detail: `${last.place}, ${ago}.` };
    return { title: "Quiet now", detail: `Last: ${text}, ${ago}.` };
}

if (typeof module !== "undefined") module.exports = { daySummary };
