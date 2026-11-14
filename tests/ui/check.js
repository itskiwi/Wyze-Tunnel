// Opens every page of the web UI (tests/ui/serve.py) on a phone, a small phone and a desktop, in light and dark,
// and fails on what a person would call a glitch: a script error, a page that scrolls sideways, a server error,
// or the More sheet hidden under the page.
//   python tests/ui/serve.py &   then   node tests/ui/check.js
const { chromium } = require("playwright");

const BASE = "http://127.0.0.1:5055/";
const PAGES = ["", "live", "glance", "health", "camera?cam=front-door", "cameras", "devices", "alerts", "settings", "account", "account?s=backup", "recordings",
    "history", "search", "apps", "rules", "network", "map", "camera-settings", "street-watch", "phone-cams", "sd-sync"];
const VIEWS = { phone: { width: 390, height: 844 }, small: { width: 320, height: 568 }, desktop: { width: 1280, height: 800 } };

(async () => {
    const browser = await chromium.launch(), problems = [];
    for (const [view, viewport] of Object.entries(VIEWS)) for (const colorScheme of ["light", "dark"]) {
        const touch = view !== "desktop";
        const context = await browser.newContext({ viewport, colorScheme, isMobile: touch, hasTouch: touch });
        await context.route(/^https?:\/\/(?!127\.0\.0\.1)/, (route) => route.abort());  // no update check, nothing leaves the test
        for (const path of PAGES) {
            const page = await context.newPage(), where = `${view} ${colorScheme} /${path}`;
            page.on("pageerror", (err) => problems.push(`${where}: script error: ${err.message}`));
            const resp = await page.goto(BASE + path, { waitUntil: "load" });
            if (resp.status() >= 400) problems.push(`${where}: answered ${resp.status()}`);
            await page.waitForTimeout(500);
            const wide = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
            if (wide > 1) problems.push(`${where}: scrolls sideways by ${wide}px`);
            if (touch && await page.$(".tabbar [data-more]")) {
                // The More sheet must sit on top of the page (the layering bug fixed in #179).
                await page.click(".tabbar [data-more]");
                await page.waitForTimeout(400);
                const onTop = await page.evaluate(() => {
                    const menu = document.querySelector("#settings .menu"), r = menu.getBoundingClientRect();
                    return [0.25, 0.5, 0.75].every((f) => menu.contains(document.elementFromPoint(r.left + r.width / 2, r.top + r.height * f)));
                });
                if (!onTop) problems.push(`${where}: the More sheet is covered by the page`);
            }
            await page.close();
        }
        await context.close();
    }
    await browser.close();
    console.log(problems.length ? problems.join("\n") : `ui ok: ${PAGES.length} pages, ${Object.keys(VIEWS).length} sizes, light and dark`);
    process.exit(problems.length ? 1 : 0);
})();
