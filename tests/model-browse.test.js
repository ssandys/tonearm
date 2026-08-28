const test = require("node:test")
const assert = require("node:assert")
const M = require("../Model.js")

const CORE = { host: "192.168.50.118", http_port: 9330, name: "yavin" }
const STATE = { v: 1, status: "ok", core: CORE, zone: null, zones: [] }
const ROW = {
  title: "Dead Man's Party", subtitle: "Oingo Boingo",
  image_key: "48f5b5fe1ee1dcd0f89bf0f6babcc93a",
  can_descend: true, can_play: true
}

test("imageUrl builds a Core image URL from a bare key", () => {
  assert.strictEqual(
    M.imageUrl(CORE, "abc", 64),
    "http://192.168.50.118:9330/api/image/abc?scale=fit&width=64&height=64")
})

test("imageUrl returns empty string with no core", () => {
  assert.strictEqual(M.imageUrl(null, "abc", 64), "")
})

test("imageUrl returns empty string with no key", () => {
  assert.strictEqual(M.imageUrl(CORE, null, 64), "")
})

test("imageUrl falls back to port 9330", () => {
  assert.ok(M.imageUrl({ host: "h" }, "k", 32).indexOf(":9330/") > 0)
})

test("rowArtUrl uses the row's own image_key", () => {
  assert.ok(M.rowArtUrl(STATE, ROW, 48)
    .indexOf("48f5b5fe1ee1dcd0f89bf0f6babcc93a") > 0)
})

test("rowArtUrl is empty for a row with no art -- a category", () => {
  // Measured: category rows carry image_key null (spec 2.4).
  assert.strictEqual(
    M.rowArtUrl(STATE, { title: "Albums", image_key: null }, 48), "")
})

test("artUrl still works and still reads now_playing", () => {
  const playing = {
    v: 1, status: "ok", core: CORE,
    zone: { now_playing: { image_key: "zzz" } }, zones: []
  }
  assert.ok(M.artUrl(playing, 256).indexOf("/api/image/zzz") > 0)
})

test("moveCursor steps down and stops at the last row", () => {
  assert.strictEqual(M.moveCursor(0, 1, 3), 1)
  assert.strictEqual(M.moveCursor(2, 1, 3), 2)
})

test("moveCursor steps up and stops at the first row", () => {
  assert.strictEqual(M.moveCursor(1, -1, 3), 0)
  assert.strictEqual(M.moveCursor(0, -1, 3), 0)
})

test("moveCursor on an empty list stays at -1", () => {
  assert.strictEqual(M.moveCursor(-1, 1, 0), -1)
})

test("moveCursor clamps a cursor left past the end by a shorter page", () => {
  assert.strictEqual(M.moveCursor(9, 0, 3), 2)
})

// `rowLabel` was cut in the pre-flight scan: nothing consumed it. An exported
// helper with no caller is the kind of thing this project's own review rubric
// treats as a defect, and it is trivial to add back if a tooltip ever wants it.
