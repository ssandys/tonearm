// tests/model-visual-state.test.js
//
// The empty card, header and tooltip copy across the snapshot lifecycle --
// including cached-zone readiness ("Loading outputs…") and the precedence
// rules between faults, readiness, missing pins and idle. Fixtures are
// inline (upstream ships no tools/preview tree) and mirror the real snapshot
// contracts; the legacy v1 case has no `selection` key at all.
const test = require("node:test")
const assert = require("node:assert/strict")
const M = require("../Model.js")

const CORE = { name: "Preview Core" }

function sel(mode, zoneId, availability) {
  return { mode: mode, zone_id: zoneId, availability: availability }
}

function base(status, zone, zones, selection) {
  return { v: 1, status: status, core: CORE, zone: zone, zones: zones || [], selection: selection }
}

function zone(overrides) {
  return Object.assign({
    id: "fixture-dac", name: "Preview USB DAC", state: "playing", pinned: true,
    length: 240,
    now_playing: { title: "A synthetic track title", artist: "Preview Artist", album: "Preview Album" },
    volume: { value: 25, min: 0, max: 100, muted: false },
    position: 42
  }, overrides || {})
}

function controls(overrides) {
  return Object.assign({
    transport: true, seek: true, volume: true,
    volume_output_id: "fixture-output", mute_output_ids: ["fixture-output"], muted: false
  }, overrides || {})
}

const states = {
  initial: null,
  connecting: base("connecting", null, [], sel("auto", null, "none")),
  unpaired: base("unpaired", null, [], sel("auto", null, "none")),
  disconnected: base("unreachable", null, [], sel("auto", null, "none")),
  idle: base("ok", null, [], sel("auto", null, "none")),
  unknown: base("ok", null, [], sel("pinned", "missing-dac", "unknown")),
  missing: base("ok", null,
    [{ id: "fixture-room", name: "Preview network room", state: "paused" }],
    sel("pinned", "missing-dac", "missing")),
  playing: base("ok", zone({ controls: controls() }),
    [{ id: "fixture-dac", name: "Preview USB DAC", state: "playing" },
     { id: "fixture-room", name: "Preview network room", state: "paused" }],
    sel("pinned", "fixture-dac", "available")),
  paused: base("ok", zone({ state: "paused", controls: controls() }),
    [{ id: "fixture-dac", name: "Preview USB DAC", state: "playing" }],
    sel("pinned", "fixture-dac", "available")),
  fixed: base("ok", zone({ state: "paused", volume: null,
      controls: controls({ volume: false, volume_output_id: null, mute_output_ids: [], muted: false }) }),
    [{ id: "fixture-dac", name: "Preview USB DAC", state: "playing" }],
    sel("pinned", "fixture-dac", "available"))
}

test("empty body distinguishes startup, pairing, outage, loading, missing pin and idle", () => {
  const expected = {
    initial: "Waiting for Tonearm", connecting: "Connecting to Roon…",
    unpaired: "Authorization required", disconnected: "Connection unavailable",
    idle: "No output selected", unknown: "Loading outputs…", missing: "Output unavailable"
  }
  for (const [name, title] of Object.entries(expected)) {
    assert.equal(M.emptyTitle(states[name]), title, name)
    assert.ok(M.emptyDetail(states[name]).length > 0, name + " needs an explanation")
  }
})

test("fresh-list waiting has explicit header/tooltip without a false missing warning", () => {
  for (const mode of ["auto", "pinned"]) {
    const st = Object.assign({}, states.unknown, {
      selection: Object.assign({}, states.unknown.selection, { mode: mode })
    })
    assert.equal(M.headerStatus(st), "Loading outputs…")
    assert.equal(M.tooltipText(st), "Loading outputs… · Preview Core")
    // Readiness is not a fault: the bar stays at ok severity.
    assert.equal(M.barState(st, 1000, 1000).severity, "ok")
    assert.ok(!M.emptyDetail(st).includes("unavailable"))
    assert.equal(M.emptyTitle(st), "Loading outputs…")
    assert.equal(M.waitingForZones(st), true)
  }
})

test("connection fault takes precedence over remembered unknown or missing identity", () => {
  for (const availability of ["unknown", "missing"]) {
    const st = Object.assign({}, states.disconnected, {
      selection: sel("pinned", "missing-dac", availability)
    })
    assert.equal(M.headerStatus(st), "Roon Core unreachable")
    assert.equal(M.emptyTitle(st), "Connection unavailable")
    assert.equal(M.waitingForZones(st), false)
  }
  assert.equal(M.emptyTitle(Object.assign({}, states.disconnected, { status: "no_network" })),
    "Connection unavailable")
  assert.equal(M.emptyTitle(Object.assign({}, states.unknown, { status: "unpaired" })),
    "Authorization required")
})

test("pairing instructions remain available in full, beyond the elided header", () => {
  assert.equal(M.emptyDetail(states.unpaired), "Enable Tonearm in Roon → Settings → Extensions.")
  assert.equal(M.headerStatus(states.missing), "Pinned output unavailable")
  assert.match(M.emptyDetail(states.missing), /Choose an available output/)
})

test("legacy v1 idle and available zones without metadata keep Nothing playing", () => {
  const legacy = Object.assign({}, states.idle)
  delete legacy.selection
  assert.equal(M.emptyTitle(legacy), "Nothing playing")
  assert.equal(M.emptyDetail(legacy), "")
  for (const name of ["playing", "paused", "fixed"]) {
    assert.equal(M.emptyTitle(states[name]), "Nothing playing", name)
    assert.equal(M.emptyDetail(states[name]), "", name)
    assert.equal(M.headerStatus(states[name]), "Preview Core", name)
  }
})
