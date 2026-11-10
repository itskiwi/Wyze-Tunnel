// Pages show err.message when a request fails. The browser's own wording ("Failed to fetch", "Unexpected token '<'")
// helps nobody, so every failed request and unreadable answer gets one plain sentence instead.
(() => {
    const rawFetch = globalThis.fetch, rawJson = Response.prototype.json;
    globalThis.fetch = (...args) => rawFetch(...args).catch((err) => {
        if (err.name === "AbortError") throw err;  // a request the page itself cancelled
        throw new Error("Couldn't reach Wyze Tunnel. Check that its computer is on and that you're connected, then try again");
    });
    Response.prototype.json = function () {
        const status = this.status;
        return rawJson.call(this).catch(() => {
            throw new Error(status === 401 ? "You've been signed out. Reload the page to sign in again"
                : status >= 500 ? "Wyze Tunnel hit a problem. Try again in a moment"
                : "Wyze Tunnel sent an answer this page couldn't read. Reload the page and try again");
        });
    };
})();
