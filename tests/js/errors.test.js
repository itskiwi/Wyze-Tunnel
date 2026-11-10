// node tests/js/errors.test.js
const assert = require("assert");

globalThis.fetch = (url) => url === "down" ? Promise.reject(new TypeError("Failed to fetch"))
    : url === "stop" ? Promise.reject(Object.assign(new Error("stopped"), { name: "AbortError" }))
    : Promise.resolve(new Response(url === "ok" ? '{"a": 1}' : "<!doctype html>", { status: url === "ok" ? 200 : 500 }));
require("../../app/static/errors.js");

(async () => {
    assert.deepStrictEqual(await (await fetch("ok")).json(), { a: 1 });
    await assert.rejects(fetch("down"), /^Error: Couldn't reach Wyze Tunnel/);
    await assert.rejects(fetch("stop"), { name: "AbortError" });
    await assert.rejects((await fetch("crash")).json(), /^Error: Wyze Tunnel hit a problem/);
    console.log("errors ok");
})();
