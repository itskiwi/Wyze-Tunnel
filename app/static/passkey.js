// Passkeys in the browser. The server speaks py_webauthn's JSON, where every byte string is base64url.
const passkey = (() => {
    const dec = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));
    const enc = (b) => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    const post = (url, body) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) })
        .then((resp) => resp.json().then((b) => resp.ok ? b : Promise.reject(new Error(b.error || resp.statusText))));
    const toJSON = (cred) => {
        const r = cred.response, response = {};
        for (const k of ["clientDataJSON", "attestationObject", "authenticatorData", "signature", "userHandle"]) if (r[k]) response[k] = enc(r[k]);
        if (r.getTransports) response.transports = r.getTransports();
        return { id: cred.id, rawId: enc(cred.rawId), type: cred.type, response, clientExtensionResults: cred.getClientExtensionResults() };
    };
    // A closed or cancelled prompt isn't an error worth a scary message.
    const friendly = (err) => err.name === "NotAllowedError" ? new Error("Cancelled. Try again, or use your password.") : err;
    return {
        supported: Boolean(window.PublicKeyCredential && window.isSecureContext),
        async add(base, name) {
            const o = await post(`${base}/register/options`);
            o.challenge = dec(o.challenge);
            o.user.id = dec(o.user.id);
            o.excludeCredentials = (o.excludeCredentials || []).map((c) => ({ ...c, id: dec(c.id) }));
            const cred = await navigator.credentials.create({ publicKey: o }).catch((e) => Promise.reject(friendly(e)));
            return post(`${base}/register`, { credential: toJSON(cred), name });
        },
        async signIn(base) {
            const o = await post(`${base}/options`);
            o.challenge = dec(o.challenge);
            o.allowCredentials = (o.allowCredentials || []).map((c) => ({ ...c, id: dec(c.id) }));
            const cred = await navigator.credentials.get({ publicKey: o }).catch((e) => Promise.reject(friendly(e)));
            return post(`${base}/finish`, toJSON(cred));
        },
    };
})();
