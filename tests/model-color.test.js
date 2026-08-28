const test = require("node:test")
const assert = require("node:assert")
const M = require("../Model.js")

test("normalizeHex accepts #rrggbb", () => {
  assert.strictEqual(M.normalizeHex("#B59790"), "#b59790")
})

test("normalizeHex strips Qt's #aarrggbb alpha prefix", () => {
  // ColorQuantizer yields QColor; String(qcolor) is #aarrggbb when it has alpha.
  assert.strictEqual(M.normalizeHex("#ff3f8fd4"), "#3f8fd4")
})

test("normalizeHex rejects anything else", () => {
  assert.strictEqual(M.normalizeHex("rgb(1,2,3)"), null)
  assert.strictEqual(M.normalizeHex(""), null)
  assert.strictEqual(M.normalizeHex(undefined), null)
})

test("contrastRatio is symmetric and bounded", () => {
  const c = M.contrastRatio("#ffffff", "#000000")
  assert.ok(Math.abs(c - 21) < 0.01, "white on black is 21:1")
  assert.strictEqual(M.contrastRatio("#0c0b0c", "#0c0b0c"), 1)
})

test("saturation separates grey from vivid", () => {
  assert.strictEqual(M.saturation("#808080"), 0)
  assert.ok(M.saturation("#3f8fd4") > 0.6)
})

test("pickAccent takes the most saturated color clearing the contrast floor", () => {
  const colors = ["#101010", "#3f8fd4", "#8a8588"]
  assert.strictEqual(M.pickAccent(colors, "#0c0b0c"), "#3f8fd4")
})

test("pickAccent falls back to the theme accent when nothing clears the floor", () => {
  // A near-black cover: every entry is invisible on #0c0b0c.
  assert.strictEqual(M.pickAccent(["#0d0d0d", "#111111"], "#0c0b0c"), "#b59790")
})

test("pickAccent falls back on empty and malformed input", () => {
  assert.strictEqual(M.pickAccent([], "#0c0b0c"), "#b59790")
  assert.strictEqual(M.pickAccent(null, "#0c0b0c"), "#b59790")
  assert.strictEqual(M.pickAccent(["not a color"], "#0c0b0c"), "#b59790")
})

test("pickAccent is deterministic when saturation ties", () => {
  // V4's sort is not stable and this runs in both engines, so the tie-break
  // must be a total order, not insertion order.
  const a = M.pickAccent(["#d40000", "#00d400"], "#0c0b0c")
  const b = M.pickAccent(["#00d400", "#d40000"], "#0c0b0c")
  assert.strictEqual(a, b)
})

test("artUrl builds a sized URL against the core's http port", () => {
  // http_port is deliberately NOT 9330 (artUrl's own fallback default) --
  // a regression that ignored state.core.http_port entirely and always
  // emitted the fallback would still pass a fixture using 9330.
  const state = { core: { host: "192.168.50.118", http_port: 8080, name: "yavin" },
                  zone: { now_playing: { image_key: "a1b2" } } }
  assert.strictEqual(M.artUrl(state, 256),
    "http://192.168.50.118:8080/api/image/a1b2?scale=fit&width=256&height=256")
})

test("artUrl is empty when there is nothing to show", () => {
  assert.strictEqual(M.artUrl(null, 256), "")
  assert.strictEqual(M.artUrl({ core: null, zone: null }, 256), "")
  assert.strictEqual(M.artUrl({ core: { host: "h", http_port: 1 },
                                zone: { now_playing: {} } }, 256), "")
})
