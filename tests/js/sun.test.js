// node tests/js/sun.test.js
const assert = require("assert");
const { sunTimes, sunPhase } = require("../../app/static/sun.js");

// Equinox at a made-up spot in the middle of the US: about 12 hours of day (a few minutes more from refraction).
const eq = sunTimes(new Date("2026-03-20T18:00:00Z"), 40, -100);
const hours = (eq.sunset - eq.sunrise) / 3600;
assert(hours > 12 && hours < 12.3, `day length ${hours}`);
const noonUtc = new Date(((eq.sunrise + eq.sunset) / 2) * 1000);
assert(Math.abs(noonUtc.getUTCHours() + noonUtc.getUTCMinutes() / 60 - 18.8) < 0.15, `solar noon ${noonUtc.toISOString()}`);

// Late evening still gives that day's times, not tomorrow's.
const late = sunTimes(new Date("2026-03-21T04:30:00Z"), 40, -100);  // 22:30 local solar time on the 20th
assert(Math.abs(late.sunrise - eq.sunrise) < 60);

// Polar night and midnight sun don't break.
const polar = sunTimes(new Date("2026-12-21T12:00:00Z"), 80, 10);
assert.strictEqual(polar.sunrise, polar.sunset);
const summer = sunTimes(new Date("2026-06-21T12:00:00Z"), 80, 10);
assert(summer.sunset - summer.sunrise > 86000);

// Phases
assert.deepStrictEqual(sunPhase(1000, 2000, 50000), { light: false, golden: false });
assert.deepStrictEqual(sunPhase(2100, 2000, 50000), { light: true, golden: true });
assert.deepStrictEqual(sunPhase(20000, 2000, 50000), { light: true, golden: false });
assert.deepStrictEqual(sunPhase(47000, 2000, 50000), { light: true, golden: true });
assert.deepStrictEqual(sunPhase(50000, 2000, 50000), { light: false, golden: false });
console.log("sun.js ok");
