// Self-check for push-to-talk in app/static/webrtc.js: node tests/js/talk.test.js
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const track = () => ({ stopped: false, stop() { this.stopped = true } });
let mics = [], pcs = [], sockets = [];
const ctx = {
  console, URL, setTimeout, clearTimeout,
  window: { location: { href: "http://localhost:5050/" }, setTimeout, clearTimeout },
  document: { querySelectorAll: () => [], getElementById: () => null },
  navigator: {
    mediaDevices: {
      getUserMedia: async () => { const t = track(); const mic = { t, getAudioTracks: () => [t], getTracks: () => [t] }; mics.push(mic); return mic },
    },
  },
  fetch: async () => ({ json: async () => ({ result: "ok", cam: "porch", signalingUrl: "wss://kvs", servers: [], ClientId: "viewer" }) }),
  WebSocket: class { constructor(url) { this.url = url; this.sent = []; sockets.push(this) } send(m) { this.sent.push(m) } close() { this.closed = true } },
  RTCPeerConnection: class {
    constructor() { this.transceivers = []; pcs.push(this) }
    addTransceiver(what, init) { this.transceivers.push([what, init.direction]) }
    createOffer() { return new Promise(() => { }) }
    close() { this.closed = true }
  },
};
vm.runInNewContext(fs.readFileSync(`${__dirname}/../../app/static/webrtc.js`, "utf8"), ctx);
const tick = () => new Promise((r) => setImmediate(r));

(async () => {
  // Held: a talk-only Kinesis session that receives video and sends the mic, like the Wyze app.
  const btn = { dataset: { cam: "porch" }, getAttribute: () => "Hold to talk" };
  await ctx.talk(btn, true);
  assert.strictEqual(btn.dataset.state, "connecting");
  sockets[0].onopen();
  assert.deepStrictEqual(pcs[0].transceivers, [["video", "recvonly"], [mics[0].t, "sendrecv"]]);
  btn.talker.pc.iceConnectionState = "connected";
  btn.talker.onConnectionStateChange();
  assert.strictEqual(btn.dataset.state, "live");

  // Released: the session and the mic both end, and nothing reconnects.
  ctx.talk(btn, false);
  assert.ok(pcs[0].closed && sockets[0].closed && mics[0].t.stopped);
  assert.strictEqual(btn.dataset.state, undefined);
  sockets[0].onclose();
  await new Promise((r) => setTimeout(r, 2100));
  assert.strictEqual(sockets.length, 1, "a released talk session doesn't restart");

  // Let go while the mic prompt is open: the mic is dropped and no session starts.
  const quick = { dataset: { cam: "porch" }, getAttribute: () => "Hold to talk" };
  const pending = ctx.talk(quick, true);
  ctx.talk(quick, false);
  await pending;
  await tick();
  assert.ok(mics[1].t.stopped);
  assert.strictEqual(sockets.length, 1);
  assert.strictEqual(quick.talker, null);

  // A refused signal (a guest, or a camera without WebRTC) shows on the button and frees the mic.
  ctx.fetch = async () => ({ json: async () => ({ result: "Guests can watch but not talk through cameras." }) });
  const refused = { dataset: { cam: "porch" }, getAttribute: () => "Hold to talk" };
  await ctx.talk(refused, true);
  assert.strictEqual(refused.dataset.state, "error");
  assert.ok(mics[2].t.stopped && refused.talker === null);
  assert.match(refused.title, /Guests can watch/);
  console.log("talk ok");
})().catch((e) => { console.error(e); process.exit(1) });
