// tests/model-player.test.js
//
// The pre-dispatch player route, the captured modern envelope, the legacy
// argv, and reply correlation -- all pure Model.js, no tools/preview
// dependency (upstream has no tools/ tree; fixtures are built inline here).
//
// The three snapshot shapes fixtures mirror, because the ROUTE is chosen from
// shape before anything spawns:
//   legacy092()    092e950 daemon: no `selection` key, no `zone.controls`
//   transitional() 0.14.4 daemon: `selection`, still no `zone.controls`
//   modern()       proposal daemon: `selection` + `zone.controls`
const test = require("node:test")
const assert = require("node:assert/strict")
const M = require("../Model.js")

function core() {
  return { host: "192.168.50.118", http_port: 9330, name: "yavin" }
}

function legacy092() {
  return {
    v: 1, status: "ok", core: core(),
    zone: {
      id: "z1", name: "Living Room", state: "playing", pinned: true,
      volume: { value: 62, min: 0, max: 100, step: 1, muted: false },
      position: 271, length: 585,
      now_playing: { title: "Blue Train", artist: "John Coltrane", album: "Blue Train", image_key: "a1b2" }
    },
    zones: [{ id: "z1", name: "Living Room", state: "playing" },
            { id: "z2", name: "Study", state: "stopped" }]
  }
}

function transitional() {
  const st = legacy092()
  st.selection = { mode: "pinned", zone_id: "z1", availability: "available" }
  return st
}

function modern(controls) {
  const st = transitional()
  st.zone.controls = controls || {
    transport: true, seek: true, volume: true,
    volume_output_id: "out-a", mute_output_ids: ["out-a"], muted: false
  }
  return st
}

function clone(st) { return JSON.parse(JSON.stringify(st)) }

// ---------------------------------------------------------------------------
// vocabulary
// ---------------------------------------------------------------------------

test("the verb vocabulary is the full transport set, never a pause-only client", () => {
  assert.deepEqual(M.PLAYER_VERBS,
    ["play", "pause", "playpause", "next", "previous", "seek", "volume", "mute", "unmute"])
  for (const verb of M.PLAYER_VERBS) assert.equal(M.isPlayerVerb(verb), true, verb)
  for (const verb of ["transfer", "zone", "browse", "subscribe", "status", "", null, undefined, 5]) {
    assert.equal(M.isPlayerVerb(verb), false, String(verb))
  }
})

// ---------------------------------------------------------------------------
// the captured modern request
// ---------------------------------------------------------------------------

test("player request captures zone and output identity at click, not at reply", () => {
  const st = modern()
  const req = M.playerRequest(st, "volume", 20, "test-1")
  assert.equal(req.cmd, "player_targeted")
  assert.equal(req.zone_id, "z1")
  assert.equal(req.output_id, "out-a")
  assert.equal(req.arg, 20)
  // The snapshot moving on cannot retarget a request already built.
  st.zone.id = "other"
  st.zone.controls.volume_output_id = "out-b"
  assert.equal(req.zone_id, "z1")
  assert.equal(req.output_id, "out-a")
  const mute = M.playerRequest(modern(), "mute", undefined, "m")
  assert.deepEqual(mute.output_ids, ["out-a"])
})

test("the modern envelope is exactly the player_targeted wire shape", () => {
  assert.deepEqual(M.playerRequest(modern(), "volume", 20, "r1"),
    { cmd: "player_targeted", v: 1, request_id: "r1", zone_id: "z1", verb: "volume",
      arg: 20, output_id: "out-a", output_ids: null })
  assert.deepEqual(M.playerRequest(modern(), "mute", undefined, "r2"),
    { cmd: "player_targeted", v: 1, request_id: "r2", zone_id: "z1", verb: "mute",
      arg: null, output_id: null, output_ids: ["out-a"] })
  assert.deepEqual(M.playerRequest(modern(), "unmute", null, "r3"),
    { cmd: "player_targeted", v: 1, request_id: "r3", zone_id: "z1", verb: "unmute",
      arg: null, output_id: null, output_ids: ["out-a"] })
  assert.deepEqual(M.playerRequest(modern(), "playpause", null, "r4"),
    { cmd: "player_targeted", v: 1, request_id: "r4", zone_id: "z1", verb: "playpause",
      arg: null, output_id: null, output_ids: null })
  assert.deepEqual(M.playerRequest(modern(), "seek", 0, "r5"),
    { cmd: "player_targeted", v: 1, request_id: "r5", zone_id: "z1", verb: "seek",
      arg: 0, output_id: null, output_ids: null })
})

test("request ids are validated at the 64-character wire cap", () => {
  assert.ok(M.playerRequest(modern(), "pause", null, "x".repeat(64)))
  assert.equal(M.playerRequest(modern(), "pause", null, "x".repeat(65)), null)
  assert.equal(M.playerRequest(modern(), "pause", null, ""), null)
  assert.equal(M.playerRequest(modern(), "pause", null, 5), null)
  assert.equal(M.playerRequest(modern(), "pause", null, null), null)
})

test("zone and output ids are validated at the 128-character cap", () => {
  const long = "z".repeat(128)
  const st = modern()
  st.zone.id = long
  st.selection.zone_id = long
  assert.equal(M.canPlayer(st, "pause"), true)
  st.zone.id = "z".repeat(129)
  st.selection.zone_id = st.zone.id
  assert.equal(M.canPlayer(st, "pause"), false)
  assert.equal(M.playerRequest(st, "pause", null, "r"), null)

  const badOut = modern()
  badOut.zone.controls.volume_output_id = "o".repeat(129)
  assert.equal(M.canPlayer(badOut, "volume"), false)
  assert.equal(M.canPlayer(badOut, "pause"), true)   // per-verb, not all-or-nothing

  const badMute = modern()
  badMute.zone.controls.mute_output_ids = ["out-a", "o".repeat(129)]
  assert.equal(M.canPlayer(badMute, "mute"), false)
})

test("a mute group is at most 32 unique output ids", () => {
  const many = modern()
  many.zone.controls.mute_output_ids = Array.from({ length: 32 }, (_, i) => "out-" + i)
  assert.equal(M.canPlayer(many, "mute"), true)
  many.zone.controls.mute_output_ids.push("out-32")
  assert.equal(M.canPlayer(many, "mute"), false)

  const dup = modern()
  dup.zone.controls.mute_output_ids = ["out-a", "out-a"]
  assert.equal(M.canPlayer(dup, "mute"), false)
  assert.equal(M.playerRequest(dup, "mute", null, "r"), null)

  const empty = modern()
  empty.zone.controls.mute_output_ids = []
  assert.equal(M.canPlayer(empty, "mute"), false)
})

// ---------------------------------------------------------------------------
// readiness, capability and strict arguments
// ---------------------------------------------------------------------------

test("unknown/missing/disconnected snapshots and old daemons disable the targeted path", () => {
  const dead = {
    initial: null,
    connecting: { status: "connecting", zone: null },
    unpaired: { status: "unpaired", zone: null },
    unreachable: { status: "unreachable", core: core(), zone: null },
    no_network: { status: "no_network", core: core(), zone: null },
    no_zone: { status: "ok", core: core(), zone: null, zones: [] },
    loading_outputs: { status: "ok", core: core(), zone: null, zones: [],
      selection: { mode: "auto", zone_id: null, availability: "unknown" } },
    missing_pin: { status: "ok", core: core(), zone: null,
      selection: { mode: "pinned", zone_id: "pin", availability: "missing" } },
    selection_mismatch: (() => { const s = modern(); s.selection.zone_id = "other"; return s })(),
    old_daemon_controls: (() => { const s = modern(); delete s.zone.controls; return s })(),
    old_daemon_both: legacy092()
  }
  for (const [name, st] of Object.entries(dead)) {
    assert.equal(M.canPlayer(st, "playpause"), false, name)
    assert.equal(M.playerRequest(st, "playpause", undefined, "x"), null, name)
  }
})

test("seek/volume permission and strict integer/range checks use no presentation defaults", () => {
  const st = modern()
  st.zone.controls.seek = false
  assert.equal(M.playerRequest(st, "seek", 2, "x"), null)
  st.zone.controls.volume = false
  assert.equal(M.playerRequest(st, "volume", 2, "x"), null)
  // Strict types first: no boolean, string, NaN or infinite ever coerces.
  for (const arg of [true, "20", NaN, Infinity, -Infinity, 1.5, null, undefined]) {
    assert.equal(M.playerRequest(modern(), "volume", arg, "x"), null, String(arg))
    assert.equal(M.playerRequest(modern(), "seek", arg, "x"), null, String(arg))
  }
  // Then bounds, per verb: volume 0..100, seek 0..585 (the fixture's length).
  assert.equal(M.playerRequest(modern(), "volume", -1, "x"), null)
  assert.equal(M.playerRequest(modern(), "volume", 101, "x"), null)
  assert.equal(M.playerRequest(modern(), "seek", -1, "x"), null)
  assert.equal(M.playerRequest(modern(), "seek", 586, "x"), null)
  assert.ok(M.playerRequest(modern(), "seek", 585, "x"))
  assert.ok(M.playerRequest(modern(), "seek", 0, "x"))
  assert.ok(M.playerRequest(modern(), "volume", 0, "x"))
  assert.ok(M.playerRequest(modern(), "volume", 100, "x"))

  const fixed = modern({ transport: true, seek: true, volume: true,
    volume_output_id: "out-a", mute_output_ids: [], muted: false })
  fixed.zone.volume = null
  assert.equal(M.canPlayer(fixed, "volume"), false)
  assert.equal(M.canPlayer(fixed, "seek"), true)

  const badVolume = modern()
  badVolume.zone.volume = { value: "62", min: 0, max: 100 }
  assert.equal(M.canPlayer(badVolume, "volume"), false)
  assert.equal(M.canPlayer(badVolume, "mute"), true)   // mute facts are the id group
  const noBounds = modern()
  noBounds.zone.volume = { value: 62 }
  assert.equal(M.canPlayer(noBounds, "volume"), false)
})

test("bare verbs take no argument, and non-player verbs never target", () => {
  for (const verb of ["play", "pause", "playpause", "next", "previous", "mute", "unmute"]) {
    assert.equal(M.playerRequest(modern(), verb, 1, "x"), null, verb)
  }
  for (const verb of ["transfer", "zone", "browse", "unknown", "", null]) {
    assert.equal(M.playerRequest(modern(), verb, undefined, "x"), null, String(verb))
    const plan = M.playerRoute(modern(), verb, undefined, "x")
    assert.equal(plan.route, "none", String(verb))
    assert.equal(plan.request, undefined)
    assert.equal(plan.argv, undefined)
    assert.equal(M.playerUsable(modern(), verb), false, String(verb))
  }
})

// ---------------------------------------------------------------------------
// the pre-dispatch route matrix
// ---------------------------------------------------------------------------

test("a 092e950 snapshot (no selection, no controls) routes to the plain legacy CLI", () => {
  const st = legacy092()
  assert.deepEqual(M.playerRoute(st, "playpause", null, "r"), { route: "legacy", argv: ["playpause"] })
  for (const verb of ["play", "pause", "next", "previous"]) {
    assert.deepEqual(M.playerRoute(st, verb, null, "r"), { route: "legacy", argv: [verb] }, verb)
  }
  assert.deepEqual(M.playerRoute(st, "seek", 42, "r"), { route: "legacy", argv: ["seek", "42"] })
  assert.deepEqual(M.playerRoute(st, "volume", 30, "r"), { route: "legacy", argv: ["volume", "30"] })
  assert.deepEqual(M.playerRoute(st, "mute", null, "r"), { route: "legacy", argv: ["mute"] })
  assert.deepEqual(M.playerRoute(st, "unmute", null, "r"), { route: "legacy", argv: ["unmute"] })
})

test("a 0.14.4 snapshot (valid selection, no controls) still routes legacy", () => {
  const st = transitional()
  assert.deepEqual(M.playerRoute(st, "playpause", null, "r"), { route: "legacy", argv: ["playpause"] })
  assert.deepEqual(M.playerRoute(st, "seek", 42, "r"), { route: "legacy", argv: ["seek", "42"] })
  assert.deepEqual(M.playerRoute(st, "volume", 30, "r"), { route: "legacy", argv: ["volume", "30"] })
})

test("a modern snapshot routes to exactly one captured targeted request", () => {
  const plan = M.playerRoute(modern(), "playpause", null, "player-1")
  assert.equal(plan.route, "targeted")
  assert.deepEqual(plan.request, M.playerRequest(modern(), "playpause", null, "player-1"))
  // No fallback unit exists on the plan: one click, one dispatch.
  assert.equal(plan.argv, undefined)
})

test("modern capability failures deny with NO legacy fallback", () => {
  const noSeek = modern({ transport: true, seek: false, volume: true,
    volume_output_id: "out-a", mute_output_ids: ["out-a"], muted: false })
  assert.equal(M.playerRoute(noSeek, "seek", 10, "r").route, "none")
  assert.equal(M.playerRoute(noSeek, "playpause", null, "r").route, "targeted")

  const noTransport = modern({ transport: false, seek: true, volume: true,
    volume_output_id: "out-a", mute_output_ids: ["out-a"], muted: false })
  for (const verb of ["playpause", "seek", "volume", "mute"]) {
    assert.equal(M.playerRoute(noTransport, verb, 10, "r").route, "none", verb)
    assert.equal(M.playerRoute(noTransport, verb, 10, "r").argv, undefined, verb)
  }

  const badOutput = modern()
  badOutput.zone.controls.volume_output_id = ""
  assert.equal(M.playerRoute(badOutput, "volume", 10, "r").route, "none")

  const dupMute = modern()
  dupMute.zone.controls.mute_output_ids = ["out-a", "out-a"]
  assert.equal(M.playerRoute(dupMute, "mute", null, "r").route, "none")
})

test("malformed selection or controls deny everything, never legacy", () => {
  const shapes = {
    controls_null: (s) => { s.zone.controls = null },
    controls_array: (s) => { s.zone.controls = [{ transport: true }] },
    controls_string: (s) => { s.zone.controls = "transport" },
    controls_number: (s) => { s.zone.controls = 1 },
    selection_null: (s) => { s.selection = null },
    selection_array: (s) => { s.selection = [{ availability: "available" }] },
    selection_bad_id: (s) => { s.selection = { mode: "pinned", zone_id: 5, availability: "available" } },
    selection_bad_availability: (s) => { s.selection = { mode: "pinned", zone_id: "z1", availability: 1 } },
    partial_controls_without_selection: (s) => { delete s.selection },
    partial_controls_bad_zone: (s) => { s.zone.id = 7; s.selection.zone_id = "z1" }
  }
  for (const [name, mutate] of Object.entries(shapes)) {
    const st = modern()
    mutate(st)
    for (const [verb, arg] of [["playpause", null], ["seek", 10], ["volume", 10], ["mute", null]]) {
      const plan = M.playerRoute(st, verb, arg, "r")
      assert.equal(plan.route, "none", name + "/" + verb)
      assert.equal(plan.argv, undefined, name + "/" + verb)
      assert.equal(plan.request, undefined, name + "/" + verb)
      assert.equal(M.playerUsable(st, verb), false, name + "/" + verb)
    }
  }
})

test("an explicit selection that does not confirm the displayed zone denies legacy", () => {
  for (const [availability, zoneId] of [["unknown", "z1"], ["missing", "z1"],
                                        ["none", "z1"], ["available", "other"],
                                        ["available", null]]) {
    const st = transitional()
    st.selection = { mode: "pinned", zone_id: zoneId, availability }
    const plan = M.playerRoute(st, "playpause", null, "r")
    assert.equal(plan.route, "none", availability + "/" + zoneId)
    assert.equal(plan.argv, undefined)
    assert.equal(M.playerUsable(st, "playpause"), false)
  }
})

test("selection mode is part of the contract shape: partial or unknown mode denies", () => {
  // state.py selection() only ever emits mode "pinned" or "auto"; anything
  // else (including a missing mode) is a partial/malformed selection, not an
  // absent one -- no targeted route, and never a legacy fallback.
  const bad = [
    { zone_id: "z1", availability: "available" },              // partial: no mode
    { mode: "follow", zone_id: "z1", availability: "available" },
    { mode: null, zone_id: "z1", availability: "available" },
    { mode: 5, zone_id: "z1", availability: "available" }
  ]
  for (const selection of bad) {
    for (const [name, st] of [["modern", modern()], ["legacy-shape", transitional()]]) {
      st.selection = selection
      for (const [verb, arg] of [["playpause", null], ["seek", 10], ["volume", 10], ["mute", null]]) {
        const plan = M.playerRoute(st, verb, arg, "r")
        assert.equal(plan.route, "none", name + "/" + JSON.stringify(selection) + "/" + verb)
        assert.equal(plan.argv, undefined, name)
        assert.equal(plan.request, undefined, name)
      }
      assert.equal(M.canPlayer(st, "playpause"), false, name)
      assert.equal(M.playerUsable(st, "playpause"), false, name)
    }
  }
  // ...and both real modes still route.
  for (const mode of ["pinned", "auto"]) {
    const mod = modern()
    mod.selection = { mode: mode, zone_id: "z1", availability: "available" }
    assert.equal(M.playerRoute(mod, "playpause", null, "r").route, "targeted", mode)
    const legacy = transitional()
    legacy.selection = { mode: mode, zone_id: "z1", availability: "available" }
    assert.deepEqual(M.playerRoute(legacy, "playpause", null, "r"),
      { route: "legacy", argv: ["playpause"] }, mode)
  }
})

test("volume facts require min < max and value within range, modern and legacy", () => {
  const broken = [
    { value: 0, min: 0, max: 0 },        // zero-width
    { value: 25, min: 25, max: 25 },     // zero-width at value
    { value: 25, min: 50, max: 40 },     // inverted range
    { value: 120, min: 0, max: 100 },    // value above range
    { value: -5, min: 0, max: 100 }      // value below range
  ]
  for (const volume of broken) {
    for (const [name, st] of [["modern", modern()], ["legacy-shape", legacy092()]]) {
      st.zone.volume = volume
      const plan = M.playerRoute(st, "volume", 25, "r")
      assert.equal(plan.route, "none", name + "/" + JSON.stringify(volume))
      assert.equal(plan.argv, undefined, name)
      assert.equal(plan.request, undefined, name)
      assert.equal(M.playerUsable(st, "volume"), false, name)
    }
    // Modern: the volume capability is false, and there is no legacy
    // fallback hiding behind it.
    const mod = modern()
    mod.zone.volume = volume
    assert.equal(M.canPlayer(mod, "volume"), false)
    // A valid range with an out-of-range value is malformed metadata, not a
    // volume to render or send.
    assert.equal(M.playerRequest(mod, "volume", 25, "r"), null)
  }
})

test("a true legacy zero-width volume keeps mute but not volume", () => {
  // Justified from actual upstream fields: state.py._volume_of coerces min
  // and max independently, so min === max is a real published shape, and its
  // `muted` flag is a real boolean. Legacy mute/unmute are BARE CLI verbs
  // (cli.py BARE_VERBS) that need no range -- only the mute fact -- so the
  // zero-width shape keeps muting while volume setting is denied. Nothing is
  // fabricated: without the muted flag there is no mute fact at all.
  const zero = legacy092()
  zero.zone.volume = { value: 0, min: 0, max: 0, step: 1, muted: true }
  assert.equal(M.playerRoute(zero, "volume", 0, "r").route, "none")
  assert.equal(M.playerUsable(zero, "volume"), false)
  assert.deepEqual(M.playerRoute(zero, "mute", null, "r"), { route: "legacy", argv: ["mute"] })
  assert.deepEqual(M.playerRoute(zero, "unmute", null, "r"), { route: "legacy", argv: ["unmute"] })
  assert.equal(M.playerUsable(zero, "mute"), true)
  assert.equal(M.playerMuted(zero), true)

  // Without a real muted flag there is no mute fact, whatever the range.
  const noFlag = legacy092()
  noFlag.zone.volume = { value: 0, min: 0, max: 0, step: 1 }
  assert.equal(M.playerUsable(noFlag, "mute"), false)
  assert.equal(M.playerRoute(noFlag, "mute", null, "r").route, "none")
  assert.equal(M.playerMuted(noFlag), false)
  const noFlagWide = legacy092()
  noFlagWide.zone.volume = { value: 50, min: 0, max: 100 }   // no muted field
  assert.deepEqual(M.playerRoute(noFlagWide, "volume", 50, "r"), { route: "legacy", argv: ["volume", "50"] })
  assert.equal(M.playerRoute(noFlagWide, "mute", null, "r").route, "none")

  // On a modern daemon mute is per-verb capability facts (mute_output_ids),
  // so a zero-width volume denies volume and nothing else.
  const mod = modern()
  mod.zone.volume = { value: 0, min: 0, max: 0, muted: false }
  assert.equal(M.canPlayer(mod, "volume"), false)
  assert.equal(M.canPlayer(mod, "mute"), true)
  assert.equal(M.playerUsable(mod, "volume"), false)
  assert.equal(M.playerUsable(mod, "mute"), true)
})

test("no route at all without a healthy status or a displayed zone", () => {
  for (const status of ["connecting", "unpaired", "unreachable", "no_network", "weird", "constructor"]) {
    const st = legacy092()
    st.status = status
    assert.equal(M.playerRoute(st, "playpause", null, "r").route, "none", status)
    assert.equal(M.playerRoute(st, "playpause", null, "r").argv, undefined, status)
  }
  const noZone = legacy092()
  noZone.zone = null
  assert.equal(M.playerRoute(noZone, "playpause", null, "r").route, "none")
  const badZone = legacy092()
  badZone.zone = []
  assert.equal(M.playerRoute(badZone, "playpause", null, "r").route, "none")
  assert.equal(M.playerRoute(null, "playpause", null, "r").route, "none")
})

test("legacy arguments are strict integers inside the displayed bounds", () => {
  const st = legacy092()
  // seek: 0..length inclusive, integer only.
  assert.deepEqual(M.playerRoute(st, "seek", 0, "r").argv, ["seek", "0"])
  assert.deepEqual(M.playerRoute(st, "seek", 585, "r").argv, ["seek", "585"])
  for (const arg of [-1, 586, 1.5, true, "3", NaN, Infinity, null, undefined]) {
    assert.equal(M.playerRoute(st, "seek", arg, "r").route, "none", String(arg))
  }
  // volume: min..max inclusive, integer only.
  assert.deepEqual(M.playerRoute(st, "volume", 0, "r").argv, ["volume", "0"])
  assert.deepEqual(M.playerRoute(st, "volume", 100, "r").argv, ["volume", "100"])
  for (const arg of [-1, 101, 1.5, true, "30", NaN, null, undefined]) {
    assert.equal(M.playerRoute(st, "volume", arg, "r").route, "none", String(arg))
  }
  // bare verbs and mute take no argument at all.
  assert.equal(M.playerRoute(st, "playpause", 1, "r").route, "none")
  assert.equal(M.playerRoute(st, "mute", 1, "r").route, "none")

  // A seek bar with nothing to seek and mute without mute facts are denied,
  // not guessed.
  const noLength = legacy092()
  noLength.zone.length = 0
  assert.equal(M.playerRoute(noLength, "seek", 0, "r").route, "none")
  assert.equal(M.playerUsable(noLength, "seek"), false)
  const noVolume = legacy092()
  noVolume.zone.volume = null
  assert.equal(M.playerRoute(noVolume, "volume", 30, "r").route, "none")
  assert.equal(M.playerRoute(noVolume, "mute", null, "r").route, "none")
  assert.equal(M.playerRoute(noVolume, "playpause", null, "r").route, "legacy")
})

test("the plan is captured before dispatch and immune to later state changes", () => {
  const st = modern()
  const plan = M.playerRoute(st, "volume", 20, "player-1")
  st.zone.id = "other"
  st.selection = { mode: "pinned", zone_id: "other", availability: "available" }
  st.zone.controls.volume_output_id = "out-b"
  st.status = "unreachable"
  assert.equal(plan.route, "targeted")
  assert.equal(plan.request.zone_id, "z1")
  assert.equal(plan.request.output_id, "out-a")

  const old = legacy092()
  const legacyPlan = M.playerRoute(old, "seek", 42, "r")
  old.zone.length = 1
  assert.deepEqual(legacyPlan, { route: "legacy", argv: ["seek", "42"] })
})

test("no plan ever carries both a targeted request and a legacy argv", () => {
  const states = [modern(), legacy092(), transitional(), null,
    (() => { const s = modern(); s.selection = null; return s })(),
    (() => { const s = modern(); s.zone.controls = []; return s })()]
  for (const st of states) {
    for (const verb of M.PLAYER_VERBS) {
      for (const arg of [null, 10]) {
        const plan = M.playerRoute(st, verb, arg, "r")
        assert.ok(!(plan.request && plan.argv), verb)
        if (plan.route === "targeted") assert.ok(plan.request && plan.argv === undefined)
        if (plan.route === "legacy") assert.ok(plan.argv && plan.request === undefined)
        if (plan.route === "none") assert.ok(plan.request === undefined && plan.argv === undefined)
      }
    }
  }
})

// ---------------------------------------------------------------------------
// compatibility-aware gating
// ---------------------------------------------------------------------------

test("playerUsable keeps old-daemon transport alive and follows modern capabilities", () => {
  // 092e950 and 0.14.4: everything the displayed facts support stays enabled.
  for (const st of [legacy092(), transitional()]) {
    assert.equal(M.playerUsable(st, "playpause"), true)
    assert.equal(M.playerUsable(st, "seek"), true)
    assert.equal(M.playerUsable(st, "volume"), true)
    assert.equal(M.playerUsable(st, "mute"), true)
    // ...but the strict modern predicate stays strict.
    assert.equal(M.canPlayer(st, "playpause"), false)
  }
  // Modern per-verb.
  assert.equal(M.playerUsable(modern(), "seek"), true)
  const noSeek = modern({ transport: true, seek: false, volume: true,
    volume_output_id: "out-a", mute_output_ids: ["out-a"], muted: false })
  assert.equal(M.playerUsable(noSeek, "playpause"), true)
  assert.equal(M.playerUsable(noSeek, "seek"), false)
  // Fixed volume (no facts at all): transport and seek live, volume/mute do not.
  const fixed = modern({ transport: true, seek: true, volume: false,
    volume_output_id: null, mute_output_ids: [], muted: false })
  fixed.zone.volume = null
  assert.equal(M.playerUsable(fixed, "playpause"), true)
  assert.equal(M.playerUsable(fixed, "volume"), false)
  assert.equal(M.playerUsable(fixed, "mute"), false)
  // Incremental output: mute without a fabricated absolute slider.
  const incremental = modern({ transport: true, seek: true, volume: false,
    volume_output_id: null, mute_output_ids: ["out-a"], muted: true })
  incremental.zone.volume = null
  assert.equal(M.playerUsable(incremental, "volume"), false)
  assert.equal(M.playerUsable(incremental, "mute"), true)
  // A modern snapshot that fails its own contract disables everything.
  const mismatch = modern()
  mismatch.selection.zone_id = "other"
  for (const verb of ["playpause", "seek", "volume", "mute"]) {
    assert.equal(M.playerUsable(mismatch, verb), false, verb)
  }
})

test("playerMuted reads the shape-appropriate fact", () => {
  const mod = modern()
  assert.equal(M.playerMuted(mod), false)
  mod.zone.controls.muted = true
  assert.equal(M.playerMuted(mod), true)
  const legacy = legacy092()
  assert.equal(M.playerMuted(legacy), false)
  legacy.zone.volume.muted = true
  assert.equal(M.playerMuted(legacy), true)
  const noVolume = legacy092()
  noVolume.zone.volume = null
  assert.equal(M.playerMuted(noVolume), false)
  const broken = modern()
  broken.zone.controls = null
  assert.equal(M.playerMuted(broken), false)
  assert.equal(M.playerMuted(null), false)
  assert.equal(M.playerMuted({ zone: [] }), false)
})

// ---------------------------------------------------------------------------
// replies: correlation first, then vocabulary
// ---------------------------------------------------------------------------

function reply(request, over) {
  return Object.assign({
    v: 1, type: "command_result", request_id: request.request_id,
    zone_id: request.zone_id, verb: request.verb,
    outcome: "attempted", error: null
  }, over || {})
}

test("correlated results convey attempted/refused/unknown, never remote success", () => {
  const request = M.playerRequest(modern(), "pause", null, "r")
  assert.equal(M.playerReplyText(request, reply(request)), "")
  assert.equal(M.playerReplyText(request, reply(request, { outcome: "unknown", error: "dispatch_failed" })),
    M.PLAYER_UNKNOWN)
  assert.equal(M.playerReplyText(request, reply(request, { outcome: "refused", error: "selection_changed" })),
    "Output selection changed; command refused.")
  assert.equal(M.playerReplyText(request, reply(request, { outcome: "refused", error: "capability_unavailable" })),
    "Control unavailable for this output.")
  assert.equal(M.playerReplyText(request, reply(request, { outcome: "refused", error: "out_of_range" })),
    "Requested value outside the output range.")
  assert.equal(M.playerReplyText(request, reply(request, { outcome: "refused", error: "bad_request" })),
    "Invalid player command.")
  assert.equal(M.playerReplyText(request, reply(request, { outcome: "refused", error: "unsupported" })),
    "This daemon does not support targeted player controls.")
  // Unknown codes and prototype-chain names are unknown, not a verdict.
  assert.match(M.playerReplyText(request, reply(request, { outcome: "refused", error: "constructor" })), /unknown/i)
  assert.match(M.playerReplyText(request, reply(request, { outcome: "refused", error: "toString" })), /unknown/i)
  // attempted with an error attached, a foreign v/type, or a changed verb is
  // not an attempted result.
  assert.equal(M.playerReplyText(request, reply(request, { error: "x" })), M.PLAYER_UNKNOWN)
  assert.equal(M.playerReplyText(request, reply(request, { v: 2 })), M.PLAYER_UNKNOWN)
  assert.equal(M.playerReplyText(request, reply(request, { type: "state" })), M.PLAYER_UNKNOWN)
  assert.equal(M.playerReplyText(request, reply(request, { verb: "play" })), M.PLAYER_UNKNOWN)
  assert.match(M.playerReplyText(request, null), /unknown/i)
})

test("foreign, late and different-zone replies never overwrite feedback", () => {
  const st = modern()
  const request = M.playerRequest(st, "pause", null, "player-1")
  // The happy path.
  assert.equal(M.playerFeedback("player-1", request, st, reply(request)), "")
  // Superseded: a newer send claimed the correlation slot.
  assert.equal(M.playerFeedback("player-2", request, st, reply(request)), null)
  // Foreign: a reply about some other request or some other room.
  assert.equal(M.playerFeedback("player-1", request, st, reply(request, { request_id: "player-9" })), null)
  assert.equal(M.playerFeedback("player-1", request, st, reply(request, { zone_id: "z9" })), null)
  // The selected zone changed after the request was captured.
  const moved = clone(st)
  moved.zone.id = "z2"
  assert.equal(M.playerFeedback("player-1", request, moved, reply(request)), null)
  assert.equal(M.playerFeedback("player-1", request, null, reply(request)), null)
  // Garbage shapes are ignored rather than rendered as a verdict.
  assert.equal(M.playerFeedback("player-1", request, st, "junk"), null)
  assert.equal(M.playerFeedback("player-1", request, st, [reply(request)]), null)
  assert.equal(M.playerFeedback("player-1", null, st, reply(request)), null)
  // Empty/absent reply (failed parse, timeout, crash) IS about this request:
  // reported as unknown, never as permission to retry anywhere.
  assert.equal(M.playerFeedback("player-1", request, st, null), M.PLAYER_UNKNOWN)
  assert.equal(M.playerFeedback("player-1", request, st, undefined), M.PLAYER_UNKNOWN)
  // A correlated refusal keeps its own copy.
  assert.equal(M.playerFeedback("player-1", request, st, reply(request, { outcome: "refused", error: "unavailable" })),
    "Selected output unavailable; command refused.")
})

test("every player send claims the correlation slot, whatever its route", () => {
  // Service.send's contract, exercised through the exact primitives it calls:
  // one dispatch unit per click, newest id wins, older replies are dropped.
  const st = modern()
  const first = M.playerRoute(st, "playpause", null, "player-1")
  let latest = "player-1"
  // A legacy send (old shape) or a rejected click still claims the slot.
  latest = "player-2"
  assert.equal(M.playerFeedback(latest, first.request, st, reply(first.request)), null)
  const second = M.playerRoute(st, "volume", 30, "player-2")
  assert.equal(second.route, "targeted")
  assert.equal(M.playerFeedback(latest, second.request, st, reply(second.request)), "")
  // And the click that started it all can never come back and overwrite it.
  assert.equal(M.playerFeedback(latest, first.request, st, reply(first.request)), null)
})

// ---------------------------------------------------------------------------
// malformed facts deny without throwing
// ---------------------------------------------------------------------------

test("malformed snapshots answer false/null instead of throwing", () => {
  const broken = [null, undefined, 0, "", "state", [], {},
    { status: "ok" }, { status: "ok", zone: null },
    { status: "ok", zone: [] }, { status: "ok", zone: "z1" },
    { status: "ok", zone: { id: 5 } },
    { status: "ok", zone: { id: "z1", controls: 0 } },
    { status: "ok", zone: { id: "z1", controls: [] }, selection: [] },
    { status: "ok", zone: { id: "z1", volume: [], length: "10" }, selection: { availability: true } }]
  for (const st of broken) {
    for (const verb of M.PLAYER_VERBS) {
      assert.equal(M.canPlayer(st, verb), false, JSON.stringify(st))
      assert.equal(M.playerUsable(st, verb), false, JSON.stringify(st))
      assert.equal(M.playerRequest(st, verb, 1, "r"), null, JSON.stringify(st))
      const plan = M.playerRoute(st, verb, 1, "r")
      assert.equal(plan.route, "none", JSON.stringify(st))
    }
    assert.equal(M.playerMuted(st), false, JSON.stringify(st))
    assert.equal(typeof M.playerContext(st), "string", JSON.stringify(st))
    assert.equal(M.playerFeedback("r", { request_id: "r", zone_id: "zz" }, st, null), null,
      JSON.stringify(st))
  }
})
