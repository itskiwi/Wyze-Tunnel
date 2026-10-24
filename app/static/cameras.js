// Show/hide camera checkboxes, shared by the setup wizard and the Cameras page.
// Fills `list` with one row per camera; returns false if the camera list isn't available yet.
async function renderCameraToggles(list, status, headers = {}) {
  const resp = await fetch("cameras/visibility", { headers }).catch(() => null);
  if (!resp?.ok) { return false; }
  const cams = await resp.json();
  if (!cams.length) { return false; }

  function save() {
    const hidden = [...list.querySelectorAll("input:not(:checked)")].map((box) => box.value);
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
        if (err.message?.startsWith("Keep at least")) { list.querySelector("input").checked = true; }
      });
  }

  list.replaceChildren(...cams.map((cam) => {
    const row = document.createElement("label");
    row.className = "flex cursor-pointer items-center gap-3 px-4 py-3";
    const box = Object.assign(document.createElement("input"), { type: "checkbox", value: cam.mac, checked: !cam.hidden });
    box.className = "h-4 w-4 accent-emerald-500";
    box.addEventListener("change", save);
    const name = document.createElement("span");
    name.className = "flex-1";
    name.textContent = cam.nickname || cam.mac;
    const model = document.createElement("span");
    model.className = "text-sm text-zinc-500";
    model.textContent = cam.model;
    row.append(box, name, model);
    return row;
  }));
  return true;
}
