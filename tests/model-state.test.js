const test = require("node:test")
const assert = require("node:assert")
const M = require("../Model.js")

const CORE = { host: "192.168.50.118", http_port: 9330, name: "yavin" }
const PLAYING = {
  v: 1, status: "ok", core: CORE,
  zone: {
    id: "z1", name: "Living Room", state: "playing", pinned: true,
    volume: { value: 62, min: 0, max: 100, step: 1, muted: false },
    position: 271, length: 585,
    now_playing: { title: "Blue Train", artist: "John Coltrane", album: "Blue Train", image_key: "a1b2" }
  },
  zones: [ { id: "z1", name: "Living Room", state: "playing" },
           { id: "z2", name: "Study", state: "stopped" } ]
}

test("glyph codepoints are asserted, not shape-checked", () => {
  // A typo in an astral literal yields an invisible widget with nothing logged,
  // and a shape check passes just as happily on the wrong glyph.
  assert.strictEqual(M.GLYPH_PLAYING.codePointAt(0), 0xf040a)
  assert.strictEqual(M.GLYPH_PAUSED.codePointAt(0), 0xf03e4)
  assert.strictEqual(M.GLYPH_IDLE.codePointAt(0), 0xf0387)
  assert.strictEqual(M.GLYPH_FAULT.codePointAt(0), 0xf0026)
})

test("popup transport/volume glyph codepoints are asserted, not shape-checked", () => {
  // Panel.qml's popup originally used plain Unicode media symbols here
  // (U+23EE/U+25B6/U+23F8/U+23ED), which carry emoji presentation in the
  // deployed font and rendered as colour blocks instead of monochrome
  // glyphs. Same reasoning as the bar glyphs above: assert the codepoint.
  assert.strictEqual(M.GLYPH_PREV.codePointAt(0), 0xf04ae)
  assert.strictEqual(M.GLYPH_NEXT.codePointAt(0), 0xf04ad)
  assert.strictEqual(M.GLYPH_VOLUME_HIGH.codePointAt(0), 0xf057e)
  assert.strictEqual(M.GLYPH_VOLUME_MUTED.codePointAt(0), 0xf075f)
})

test("playing is ok severity with art", () => {
  const s = M.barState(PLAYING, 1000, 1000)
  assert.strictEqual(s.severity, "ok")
  assert.strictEqual(s.glyph, M.GLYPH_PLAYING)
  assert.strictEqual(s.showArt, true)
  assert.strictEqual(s.playing, true)
})

test("paused keeps ok severity but changes glyph", () => {
  const st = JSON.parse(JSON.stringify(PLAYING))
  st.zone.state = "paused"
  const s = M.barState(st, 1000, 1000)
  assert.strictEqual(s.severity, "ok")
  assert.strictEqual(s.glyph, M.GLYPH_PAUSED)
  assert.strictEqual(s.playing, false)
})

test("connected but idle is ok severity with no art", () => {
  const s = M.barState({ v: 1, status: "ok", core: CORE, zone: null, zones: [] }, 1000, 1000)
  assert.strictEqual(s.severity, "ok")
  assert.strictEqual(s.glyph, M.GLYPH_IDLE)
  assert.strictEqual(s.showArt, false)
  assert.strictEqual(s.playing, false)
})

test("a loading (buffering) zone is not playing", () => {
  // zones.py's Arbiter treats "playing" and "loading" both as ACTIVE for
  // zone-selection purposes (Task 8), but that is a different question from
  // whether audio is actually advancing right now. barState's `playing` pins
  // the narrower intent this widget needs: dim like any non-playing state,
  // and do not tick the seek clock, while buffering.
  const st = JSON.parse(JSON.stringify(PLAYING))
  st.zone.state = "loading"
  const s = M.barState(st, 1000, 1000)
  assert.strictEqual(s.playing, false)
})

test("unpaired is a warning, unreachable is an error", () => {
  assert.strictEqual(M.barState({ status: "unpaired", zone: null }, 1, 1).severity, "warn")
  assert.strictEqual(M.barState({ status: "unreachable", zone: null }, 1, 1).severity, "error")
  assert.strictEqual(M.barState({ status: "unreachable", zone: null }, 1, 1).glyph, M.GLYPH_FAULT)
})

test("a null state -- the relay has not spoken yet -- is an error", () => {
  assert.strictEqual(M.barState(null, 1, 1).severity, "error")
})

test("an unrecognised status is never ok", () => {
  // Guards the prototype-chain trap: a status of "constructor" must not inherit
  // a truthy member and be treated as healthy.
  assert.strictEqual(M.barState({ status: "constructor", zone: null }, 1, 1).severity, "error")
  assert.strictEqual(M.barState({ status: "toString", zone: null }, 1, 1).severity, "error")
})

test("showArt is false when the track has no image_key", () => {
  const st = JSON.parse(JSON.stringify(PLAYING))
  delete st.zone.now_playing.image_key
  assert.strictEqual(M.barState(st, 1000, 1000).showArt, false)
})

test("tooltip names the track and the zone", () => {
  assert.strictEqual(M.tooltipText(PLAYING), "Blue Train — John Coltrane · Living Room")
})

test("tooltip explains each fault in words", () => {
  assert.strictEqual(M.tooltipText(null), "tonearmd not running")
  assert.strictEqual(M.tooltipText({ status: "unpaired", zone: null }),
    "Enable tonearm in Roon → Settings → Extensions")
  assert.strictEqual(M.tooltipText({ status: "unreachable", core: CORE, zone: null }),
    "Roon Core unreachable")
  assert.strictEqual(M.tooltipText({ status: "ok", core: CORE, zone: null }),
    "Nothing playing · yavin")
})

test("connecting is normal startup, not a failure -- never reads as unreachable", () => {
  // The daemon is actively reaching for the Core. This is the ordinary first
  // second or two of every boot, not a fault, and must say something distinct
  // from "Roon Core unreachable" or a healthy startup reads as broken.
  const withCore = M.tooltipText({ status: "connecting", core: CORE, zone: null })
  assert.notStrictEqual(withCore, "Roon Core unreachable")
  assert.strictEqual(withCore, "Connecting to yavin…")

  // connecting is exactly the state where the Core may not be resolved yet,
  // so core can legitimately be absent -- must not assume it exists.
  const noCore = M.tooltipText({ status: "connecting", core: null, zone: null })
  assert.notStrictEqual(noCore, "Roon Core unreachable")
  assert.strictEqual(noCore, "Connecting to Roon…")
})

test("zoneList puts the pinned zone first and is otherwise stable by name", () => {
  const st = { status: "ok", zone: { id: "z2", pinned: true },
               zones: [ { id: "z3", name: "Kitchen", state: "stopped" },
                        { id: "z1", name: "Living Room", state: "playing" },
                        { id: "z2", name: "Study", state: "stopped" } ] }
  assert.deepStrictEqual(M.zoneList(st).map((z) => z.id), ["z2", "z3", "z1"])
})

test("zoneList breaks name ties by id for a total order", () => {
  // V4's sort is unstable, so equal names must still order deterministically or
  // the switcher visibly reshuffles between pushes.
  const mk = (zones) => ({ status: "ok", zone: null, zones })
  const a = M.zoneList(mk([{ id: "b", name: "Den" }, { id: "a", name: "Den" }]))
  const b = M.zoneList(mk([{ id: "a", name: "Den" }, { id: "b", name: "Den" }]))
  assert.deepStrictEqual(a.map((z) => z.id), b.map((z) => z.id))
})

test("zoneList tolerates a missing zones array", () => {
  assert.deepStrictEqual(M.zoneList({ status: "ok", zone: null }), [])
  assert.deepStrictEqual(M.zoneList(null), [])
})

test("isZonePinned is true only for the pinned zone's own id", () => {
  assert.strictEqual(M.isZonePinned({ id: "z1", pinned: true }, "z1"), true)
  assert.strictEqual(M.isZonePinned({ id: "z1", pinned: true }, "z2"), false)
  assert.strictEqual(M.isZonePinned({ id: "z1", pinned: false }, "z1"), false)
})

test("isZonePinned tolerates a null zone", () => {
  assert.strictEqual(M.isZonePinned(null, "z1"), false)
})

test("nextRetryDelay backs off and then caps", () => {
  assert.strictEqual(M.nextRetryDelay(0), 1000)
  assert.strictEqual(M.nextRetryDelay(1), 2000)
  assert.strictEqual(M.nextRetryDelay(2), 4000)
  assert.strictEqual(M.nextRetryDelay(99), 30000)
})

test("COLOR_ERROR and COLOR_WARN are hex strings normalizeHex accepts", () => {
  // A typo here (e.g. a 5-digit hex, or a CSS name) would ship a silently
  // transparent binding in the shell with nothing to catch it at build time.
  assert.strictEqual(M.normalizeHex(M.COLOR_ERROR), M.COLOR_ERROR.toLowerCase())
  assert.strictEqual(M.normalizeHex(M.COLOR_WARN), M.COLOR_WARN.toLowerCase())
})
