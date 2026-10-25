// Phone touches: the More tab, sheets you swipe down to close, pull to refresh,
// a full-screen camera view you swipe through, and the Add to Home Screen card.
(() => {
  const phone = matchMedia("(max-width: 767px)");
  const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone;
  const openSheet = () => document.querySelector("details.dropdown[open]");

  // Swipe tracking shared by sheets, pull to refresh and the camera view.
  function swipe(el, { start, move, end }) {
    let x0, y0, on = false;
    el.addEventListener("touchstart", (e) => {
      if (e.touches.length !== 1 || !phone.matches) { return }
      x0 = e.touches[0].clientX; y0 = e.touches[0].clientY;
      on = start ? start(e) !== false : true;
    }, { passive: true });
    el.addEventListener("touchmove", (e) => {
      if (on && move) { move(e.touches[0].clientX - x0, e.touches[0].clientY - y0, e) }
    }, { passive: false });
    el.addEventListener("touchend", (e) => {
      if (on) { on = false; end(e.changedTouches[0].clientX - x0, e.changedTouches[0].clientY - y0) }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    // More tab opens the settings menu as a sheet.
    const settings = document.getElementById("settings");
    document.querySelector(".tabbar [data-more]")?.addEventListener("click", (e) => {
      e.stopPropagation();
      settings.open = !settings.open;
    });

    // While a sheet is open, a tap outside only closes it, so it can't press a button underneath.
    document.addEventListener("click", (e) => {
      const d = openSheet();
      if (!phone.matches || !d || d.contains(e.target)) { return }
      e.preventDefault(); e.stopPropagation();
      d.open = false;
    }, true);

    // Drag a sheet down to close it.
    document.querySelectorAll("details.dropdown > .menu").forEach((sheet) => swipe(sheet, {
      start: () => sheet.scrollTop <= 0,
      move: (dx, dy, e) => {
        if (dy <= 0) { return }
        e.preventDefault();
        sheet.style.transition = "none";
        sheet.style.transform = `translateY(${dy}px)`;
      },
      end: (dx, dy) => {
        sheet.style.transition = "";
        sheet.style.transform = "";
        if (dy > 90) { sheet.parentElement.open = false }
      },
    }));

    // Pull to refresh. Browsers already do this; an app opened from the Home Screen doesn't.
    const ptr = document.getElementById("ptr");
    if (standalone && ptr) {
      swipe(document, {
        start: (e) => scrollY <= 0 && !openSheet() && !document.querySelector(".cam-focus") && !e.target.closest("[data-no-ptr], input, textarea, select, video"),
        move: (dx, dy) => {
          const pull = Math.max(0, Math.min(dy, 140));
          ptr.style.transform = `translate(-50%, ${pull * 0.6}px) rotate(${pull > 90 ? 180 : 0}deg)`;
          ptr.style.opacity = Math.min(1, pull / 80);
          ptr.classList.toggle("is-ready", pull > 90);
        },
        end: (dx, dy) => {
          if (dy > 90) { ptr.classList.add("is-loading"); location.reload(); return }
          ptr.style.transform = ""; ptr.style.opacity = "";
        },
      });
    }

    // Camera view: tap a picture to fill the screen, swipe sideways for the next camera, down to close.
    const bar = document.getElementById("camFocus");
    if (bar && document.querySelector(".cameras")) {
      const cams = () => [...document.querySelectorAll(".cameras .camera:not(.is-hidden)")];
      let current = null;
      const show = (cam) => {
        current?.classList.remove("cam-focus");
        current = cam;
        cam.classList.add("cam-focus");
        const list = cams();
        bar.querySelector("[data-count]").textContent = `${list.indexOf(cam) + 1} of ${list.length}`;
        bar.querySelector("[data-name]").textContent = cam.querySelector(".card-header-title .truncate")?.textContent || "";
      };
      const open = (cam) => {
        show(cam);
        bar.classList.remove("is-hidden");
        document.documentElement.classList.add("cam-focus-on");
        history.pushState({ camFocus: 1 }, "");
      };
      const close = () => {
        if (!current) { return }
        current.classList.remove("cam-focus");
        current = null;
        bar.classList.add("is-hidden");
        document.documentElement.classList.remove("cam-focus-on");
      };
      const step = (by) => {
        const list = cams();
        show(list[(list.indexOf(current) + by + list.length) % list.length]);
      };
      // Android's back button closes the view instead of leaving the page.
      addEventListener("popstate", close);
      bar.querySelector("[data-close]").addEventListener("click", () => history.back());
      bar.querySelector("[data-prev]").addEventListener("click", () => step(-1));
      bar.querySelector("[data-next]").addEventListener("click", () => step(1));
      document.querySelectorAll(".cameras .card-image").forEach((pic) => pic.addEventListener("click", (e) => {
        const cam = pic.closest(".camera");
        const video = pic.querySelector("video");
        // A video that hasn't started plays on the first tap, as before.
        if (!phone.matches || cam === current || pic.querySelector(".no-preview") || (video && video.paused && !video.played.length)) { return }
        if (e.target.closest("button, a, details, .vp-bar")) { return }
        open(cam);
      }));
      swipe(document, {
        start: () => !!current && !openSheet(),
        move: (dx, dy, e) => e.preventDefault(),
        end: (dx, dy) => {
          if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy)) { step(dx < 0 ? 1 : -1) }
          else if (dy > 90) { history.back() }
        },
      });
    }

    // Add to Home Screen card, until it's added or dismissed.
    const card = document.getElementById("installTip");
    let dismissed = true;
    try { dismissed = !!localStorage.getItem("installTip") } catch { }
    if (!card || standalone || dismissed || document.getElementById("pocketTip")) { return }
    const ios = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    const hide = () => { card.classList.add("is-hidden"); try { localStorage.setItem("installTip", "1") } catch { } };
    card.querySelector("[data-dismiss]").addEventListener("click", hide);
    if (ios) {
      card.querySelector("[data-ios]").classList.remove("is-hidden");
      card.classList.remove("is-hidden");
    }
    addEventListener("beforeinstallprompt", (e) => {
      e.preventDefault();
      const install = card.querySelector("[data-install]");
      install.classList.remove("is-hidden");
      card.querySelector("[data-android]").classList.remove("is-hidden");
      card.classList.remove("is-hidden");
      install.addEventListener("click", async () => { e.prompt(); if ((await e.userChoice).outcome === "accepted") { hide() } }, { once: true });
    });
    addEventListener("appinstalled", hide);
  });
})();
