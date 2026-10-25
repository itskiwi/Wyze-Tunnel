// Self-check for the hiss gate in app/static/player.js: node tests/js/hiss_gate.test.js
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

// player.js is a browser IIFE; run it against a bare page so it only defines window.vpGate.
const window = {};
const document = { readyState: "complete", addEventListener() { }, querySelectorAll: () => [] };
vm.runInNewContext(fs.readFileSync(`${__dirname}/../../app/static/player.js`, "utf8"), { window, document });

const decide = window.vpGate();
let t = 0, gain;
const feed = (db, ms) => { for (const end = t + ms; t < end; t += 20) { gain = decide(db + Math.sin(t) * 2, t) } };  // ±2 dB of jitter

feed(-60, 5000);  // hiss
assert.strictEqual(gain, 0.18, "hiss alone keeps the gate low");
feed(-30, 300);  // a voice
assert.strictEqual(gain, 1, "a voice opens the gate");
feed(-60, 100);
assert.strictEqual(gain, 1, "the gate holds open just after the voice");
feed(-60, 200);
assert.strictEqual(gain, 0.18, "the gate closes once the hold is over");
const quiet = window.vpGate();
quiet(-Infinity, 0);
assert.strictEqual(quiet(-40, 20), 1, "a sound after digital silence still opens it");
console.log("hiss gate ok");
