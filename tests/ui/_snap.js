// node snap.js <outdir> <page> [click selector...]
const { chromium } = require("playwright");
(async () => {
  const [out, path, ...clicks] = process.argv.slice(2), b = await chromium.launch();
  for (const [view, viewport, scheme] of [["phone", { width: 390, height: 844 }, "light"], ["desktop", { width: 1280, height: 800 }, "dark"]]) {
    const ctx = await b.newContext({ viewport, colorScheme: scheme, isMobile: view === "phone", hasTouch: view === "phone" });
    await ctx.route(/^https?:\/\/(?!127\.0\.0\.1)/, r => r.abort());
    const p = await ctx.newPage(); p.on("pageerror", e => console.log("ERR", view, e.message));
    await p.goto("http://127.0.0.1:5055/" + path); await p.waitForTimeout(900);
    for (const c of clicks) { await p.click(c).catch(e => console.log("click failed", c)); await p.waitForTimeout(300); }
    console.log(view, "sideways:", await p.evaluate(() => document.documentElement.scrollWidth - innerWidth));
    await p.screenshot({ path: `${out}/${view}-${(path || "home").replace(/[?=&]/g, "_")}.png`, fullPage: process.env.FULL !== "0" });
  }
  await b.close();
})();
