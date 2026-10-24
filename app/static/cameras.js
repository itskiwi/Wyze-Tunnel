// Show/hide camera checkboxes, shared by the setup wizard and the Cameras page.
// Fills `list` with one row per camera; returns false if the camera list isn't available yet.
async function renderCameraToggles(list, status, headers = {}) {
  const resp = await fetch("cameras/visibility", { headers }).catch(() => null);
  if (!resp?.ok) { return false; }
  const cams = await resp.json();
  if (!cams.length) { return false; }

  function save() {
    const hidden = [...list.querySelectorAll("input[type=checkbox]:not(:checked)")].map((box) => box.value);
    status.textContent = "Saving…";
    fetch("cameras/visibility", {
      method: "POST",
      headers: { ...headers, "Content-Type": "application/json" },
      body: JSON.stringify({ hidden }),
    })
      .then((r) => r.json().then((data) => r.ok ? data : Promise.reject(new Error(data.error))))
      .then(() => { status.textContent = "Saved. Cameras reconnect in a few seconds."; })
      .catch((err) => {
        status.textContent = err.message || "Couldn't save. Try again.";
        if (err.message?.startsWith("Keep at least")) { list.querySelector("input[type=checkbox]").checked = true; }
      });
  }

  function rename(input, mac) {
    status.textContent = "Saving…";
    fetch("cameras/name", {
      method: "POST",
      headers: { ...headers, "Content-Type": "application/json" },
      body: JSON.stringify({ mac, name: input.value }),
    })
      .then((r) => r.json().then((data) => r.ok ? data : Promise.reject(new Error(data.error))))
      .then(() => { status.textContent = input.value.trim() ? "Name saved." : "Name reset to the one in the Wyze app."; })
      .catch((err) => { status.textContent = err.message || "Couldn't save. Try again."; });
  }

  list.replaceChildren(...cams.map((cam) => {
    const row = document.createElement("div");
    row.className = "flex items-center gap-3 px-4 py-3";
    const box = Object.assign(document.createElement("input"), { type: "checkbox", value: cam.mac, checked: !cam.hidden });
    box.className = "h-4 w-4 shrink-0 cursor-pointer accent-accent";
    box.setAttribute("aria-label", `Show ${cam.nickname || cam.mac}`);
    box.addEventListener("change", save);
    const name = Object.assign(document.createElement("input"), { type: "text", value: cam.nickname || "", placeholder: cam.mac, maxLength: 64 });
    name.className = "field flex-1";
    name.setAttribute("aria-label", "Camera name");
    name.addEventListener("change", () => rename(name, cam.mac));
    name.addEventListener("keydown", (e) => { if (e.key === "Enter") { name.blur(); } });
    const model = document.createElement("span");
    model.className = "shrink-0 text-sm text-zinc-500";
    model.textContent = cam.model;
    row.append(box, name, model);
    return row;
  }));
  return true;
}
