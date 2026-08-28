const test = require("node:test")
const assert = require("node:assert")
const M = require("../Model.js")

test("formatTime pads seconds but not leading minutes", () => {
  assert.strictEqual(M.formatTime(271), "4:31")
  assert.strictEqual(M.formatTime(5), "0:05")
  assert.strictEqual(M.formatTime(0), "0:00")
})

test("formatTime grows an hours field and then pads minutes", () => {
  assert.strictEqual(M.formatTime(3600), "1:00:00")
  assert.strictEqual(M.formatTime(3871), "1:04:31")
})

test("formatTime clamps negatives and tolerates junk", () => {
  assert.strictEqual(M.formatTime(-5), "0:00")
  assert.strictEqual(M.formatTime(undefined), "0:00")
})

test("formatRemaining uses a real minus sign, not a hyphen", () => {
  assert.strictEqual(M.formatRemaining(271, 585), "−" + "5:14")
})

test("formatRemaining floors at zero rather than going negative", () => {
  assert.strictEqual(M.formatRemaining(600, 585), "−0:00")
})

test("position advances with the wall clock while playing", () => {
  const zone = { state: "playing", position: 271, length: 585 }
  assert.strictEqual(M.position(zone, 1000, 1000), 271)
  assert.strictEqual(M.position(zone, 1000, 4000), 274)
})

test("position is frozen when not playing", () => {
  const zone = { state: "paused", position: 271, length: 585 }
  assert.strictEqual(M.position(zone, 1000, 60000), 271)
})

test("position clamps at the track length", () => {
  // The daemon may be slow to push the next track; the bar must not run past
  // the end of this one.
  const zone = { state: "playing", position: 580, length: 585 }
  assert.strictEqual(M.position(zone, 1000, 999000), 585)
})

test("position never runs backwards on clock skew", () => {
  const zone = { state: "playing", position: 271, length: 585 }
  assert.strictEqual(M.position(zone, 5000, 1000), 271)
})

test("position on a null zone is zero", () => {
  assert.strictEqual(M.position(null, 1000, 2000), 0)
})
