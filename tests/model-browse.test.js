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

// --- hardening: the widget side of the same rule the daemon already applies.
// art.py percent-encodes image_key before building its URL ("image_key is
// untrusted (it comes from the Core)"). Model.js interpolated it raw, and the
// URL it produces is handed to Image.source inside the shared shell process.
test("imageUrl percent-encodes a key carrying URL syntax", () => {
  const url = M.imageUrl(CORE, "a/b?c#d", 64)
  assert.ok(url.indexOf("/api/image/a%2Fb%3Fc%23d?") > 0, url)
})

test("imageUrl keeps the caller's query parameters intact", () => {
  // A key containing ? or & must not be able to add or replace scale/width.
  const url = M.imageUrl(CORE, "k&width=99999", 64)
  assert.ok(url.indexOf("width=64") > 0, url)
  assert.strictEqual(url.split("width=").length, 2, url)
})

test("imageUrl leaves an ordinary key unchanged", () => {
  assert.ok(M.imageUrl(CORE, "abc123", 64).indexOf("/api/image/abc123?") > 0)
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

test("activatePlayed is false when Enter descended instead of playing", () => {
  // THE case this predicate exists for. `activate` plays if the row is
  // playable and descends if it is not -- spec 2.4 measured that a category
  // row and an album row are both hint "list" and cannot be told apart
  // without descending, so the daemon decides and reports which happened.
  //
  // Get this wrong in the descend direction and Enter on `Albums` wipes the
  // search and throws away the list the user was about to navigate.
  assert.strictEqual(M.activatePlayed({ ok: true, played: false }), false)
  assert.strictEqual(M.activatePlayed({ ok: true }), false)
})

test("activatePlayed is true only when the daemon says it played", () => {
  assert.strictEqual(M.activatePlayed({ ok: true, played: true }), true)
})

test("activatePlayed rejects a failed reply even if it carries played", () => {
  // A `stale` reply re-renders the level and can carry payload from it. It is
  // not a play, and treating it as one would clear a search the user never
  // finished acting on.
  assert.strictEqual(M.activatePlayed({ ok: false, error: "stale", played: true }), false)
  assert.strictEqual(M.activatePlayed({ ok: false, error: "no_zone" }), false)
})

test("activatePlayed survives no reply at all", () => {
  // `_apply` passes through whatever the relay produced; a dead daemon yields
  // null, and reaching into it would throw inside a signal handler, which Qt
  // swallows silently.
  assert.strictEqual(M.activatePlayed(null), false)
  assert.strictEqual(M.activatePlayed(undefined), false)
})

test("activatePlayed is strict about the flag's type", () => {
  // The wire carries a JSON boolean. A truthy string would mean some other
  // producer is on the socket and the reply is not the shape we think.
  assert.strictEqual(M.activatePlayed({ ok: true, played: "true" }), false)
  assert.strictEqual(M.activatePlayed({ ok: true, played: 1 }), false)
})

// --- browseArgv ------------------------------------------------------------
//
// The widget must name its own browse session. `tonearmctl browse` now
// defaults to the "cli" key, so a widget that sends no key at all would land
// on the CLI's cursor rather than its own -- the same collision as before,
// pointed the other way.

test("browseArgv carries the surface's own session key", () => {
  assert.deepStrictEqual(
    M.browseArgv("widget-DP-1", ["back"]),
    ["browse", "--session", "widget-DP-1", "back"])
})

test("browseArgv puts the flag before the op, never after", () => {
  // `search` joins everything after its op into the term, so a flag on the
  // tail would be searched for instead of parsed.
  const argv = M.browseArgv("widget-DP-1", ["search", "oingo boingo"])
  assert.strictEqual(argv.indexOf("--session") < argv.indexOf("search"), true)
  assert.strictEqual(argv[argv.length - 1], "oingo boingo")
})

test("browseArgv stringifies its arguments", () => {
  // BrowsePane already sends strings, but index/level_id are numbers at their
  // source and Process.command rejects a non-string entry.
  assert.deepStrictEqual(
    M.browseArgv("widget-DP-1", ["enter", 2, 7]),
    ["browse", "--session", "widget-DP-1", "enter", "2", "7"])
})

test("browseArgv tolerates no arguments", () => {
  assert.deepStrictEqual(
    M.browseArgv("widget-DP-1", []),
    ["browse", "--session", "widget-DP-1"])
})

test("browseArgv never emits an empty key", () => {
  // cli.py REFUSES --session "", so an empty key here would not share a cursor
  // -- it would exit 2 with usage, the reply would be null, and the pane would
  // show an error with nothing to explain it. Fall back to the shared key.
  assert.deepStrictEqual(
    M.browseArgv("", ["back"]),
    ["browse", "--session", "widget", "back"])
  assert.deepStrictEqual(
    M.browseArgv(null, ["back"]),
    ["browse", "--session", "widget", "back"])
})

// --- browseSession ---------------------------------------------------------
//
// One browse cursor per bar SURFACE. The bar builds a widget per monitor, and
// before this they all sent the bare "widget" key -- so navigating on one
// monitor moved the cursor the other monitor's pane was rendering from, and
// that pane's next keystroke was rejected as stale and snapped back.
//
// The screen name is the surface's identity: stable across a hot reload,
// bounded by the number of monitors, and meaningful in a daemon log.

test("browseSession gives each screen its own key", () => {
  assert.notStrictEqual(M.browseSession("DP-1"), M.browseSession("HDMI-A-1"))
})

test("browseSession is stable for one screen", () => {
  // Stability is the whole point: a key that changed per call would allocate
  // a fresh Roon cursor on every keystroke and evict everyone else's.
  assert.strictEqual(M.browseSession("DP-1"), M.browseSession("DP-1"))
})

test("browseSession falls back to the bare widget key without a screen", () => {
  // QsWindow may not have resolved yet at Component.onCompleted. One shared
  // key is the old behaviour -- no worse -- where inventing a unique one per
  // call would churn through MAX_BROWSE_SESSIONS.
  assert.strictEqual(M.browseSession(""), "widget")
  assert.strictEqual(M.browseSession(null), "widget")
  assert.strictEqual(M.browseSession(undefined), "widget")
  assert.strictEqual(M.browseSession("   "), "widget")
})

test("browseSession strips what has no business on the wire", () => {
  // The name comes from the compositor and ends up as a dictionary key in the
  // daemon. core.py already calls the session key a wire field; this keeps the
  // shape of what crosses that boundary decided here rather than there.
  assert.strictEqual(M.browseSession("DP 1; drop"), "widget-DP1drop")
})

test("browseSession stays inside the daemon's key bound", () => {
  // server.py MAX_SESSION_KEY = 64. Past it the daemon refuses the request and
  // the pane shows an error for a reason no user could act on.
  const key = M.browseSession("D".repeat(200))
  assert.strictEqual(key.length <= 64, true)
  assert.strictEqual(key.indexOf("widget-"), 0)
})
