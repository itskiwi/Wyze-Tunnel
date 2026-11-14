// node tests/js/today.test.js
const assert = require("assert");
const { daySummary } = require("../../app/static/today.js");

const noon = new Date(2026, 0, 5, 12, 0).getTime(), at = (minsAgo) => noon / 1000 - minsAgo * 60;
assert.deepStrictEqual(daySummary([], noon), { title: "All quiet today", detail: "Nothing has moved since midnight." });
assert.deepStrictEqual(daySummary([{ ts: at(24 * 60), kind: "person", text: "Person at Front Door" }], noon).title, "All quiet today");  // yesterday
assert.strictEqual(daySummary([{ ts: at(5), kind: "motion" }], noon).detail, "Only 1 motion alert. No people, packages or cars.");
assert.deepStrictEqual(daySummary([{ ts: at(1), kind: "person", text: "Person at Front Door.", place: "Front Door" }], noon),
    { title: "Person at Front Door", detail: "Front Door, 1 min ago." });
// Motion after a person doesn't hide the person.
assert.deepStrictEqual(daySummary([{ ts: at(2), kind: "motion" }, { ts: at(20), kind: "pet", text: "Dog in Back Yard", place: "Back Yard" }], noon),
    { title: "Quiet now", detail: "Last: Dog in Back Yard, 20 min ago." });
console.log("today ok");
