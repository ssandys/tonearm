// Model.js -- all of tonearm's display logic.
//
// Dual-loaded by node (tests) and Qt's V4 engine (the live shell), so this file
// stays in the ES3-ish subset: var and function only, no arrow functions, no
// template literals, no let/const, no spread, no .includes(.
//
// No .pragma library: it would share one instance across importing components
// but makes node refuse to parse the file. The consequence is that EVERY
// component importing this gets its own copy, so nothing here may hold mutable
// state. Every top-level var is assigned once and never reassigned.

var THEME_ACCENT = "#b59790"
var THEME_BACKGROUND = "#0c0b0c"

// Below this ratio against the panel background an extracted accent reads as a
// hole rather than a color. 3.0 is the WCAG floor for large graphical objects.
var CONTRAST_FLOOR = 3.0

function normalizeHex(c) {
  if (c === null || c === undefined) return null
  var s = String(c)
  if (s.charAt(0) !== "#") return null
  // Qt renders a QColor with alpha as #aarrggbb. Drop the alpha pair.
  if (s.length === 9) s = "#" + s.substring(3)
  if (s.length === 4) {
    s = "#" + s.charAt(1) + s.charAt(1) + s.charAt(2) + s.charAt(2) +
        s.charAt(3) + s.charAt(3)
  }
  if (s.length !== 7) return null
  if (!/^#[0-9a-fA-F]{6}$/.test(s)) return null
  return s.toLowerCase()
}

function channels(hex) {
  var h = normalizeHex(hex)
  if (h === null) return null
  return {
    r: parseInt(h.substring(1, 3), 16),
    g: parseInt(h.substring(3, 5), 16),
    b: parseInt(h.substring(5, 7), 16)
  }
}

function relativeLuminance(hex) {
  var c = channels(hex)
  if (c === null) return 0
  var raw = [c.r / 255, c.g / 255, c.b / 255]
  var lin = []
  for (var i = 0; i < 3; i++) {
    var v = raw[i]
    lin.push(v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4))
  }
  return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
}

function contrastRatio(a, b) {
  var la = relativeLuminance(a)
  var lb = relativeLuminance(b)
  var hi = la > lb ? la : lb
  var lo = la > lb ? lb : la
  return (hi + 0.05) / (lo + 0.05)
}

function saturation(hex) {
  var c = channels(hex)
  if (c === null) return 0
  var max = Math.max(c.r, Math.max(c.g, c.b))
  var min = Math.min(c.r, Math.min(c.g, c.b))
  if (max === 0) return 0
  return (max - min) / max
}

// Direction C: exactly one element takes its color from the cover. Choose the
// most saturated quantized entry that is still legible on the panel, and fall
// back to the theme accent when none is -- never darken or synthesize a color,
// because a cover with no legible entry should look deliberate, not muddy.
function pickAccent(colors, bgHex) {
  var bg = normalizeHex(bgHex)
  if (bg === null) bg = THEME_BACKGROUND
  var list = colors || []
  var best = null
  var bestSat = -1
  for (var i = 0; i < list.length; i++) {
    var hex = normalizeHex(list[i])
    if (hex === null) continue
    if (contrastRatio(hex, bg) < CONTRAST_FLOOR) continue
    var s = saturation(hex)
    // Strict > keeps the first of equals; the lexical compare then imposes a
    // total order so the result cannot depend on V4's unstable sort or on the
    // order the quantizer happened to emit.
    if (s > bestSat || (s === bestSat && best !== null && hex < best)) {
      bestSat = s
      best = hex
    }
  }
  return best === null ? THEME_ACCENT : best
}

function formatTime(sec) {
  var s = Math.floor(sec || 0)
  if (!(s > 0)) s = 0          // also catches NaN
  var h = Math.floor(s / 3600)
  var m = Math.floor((s % 3600) / 60)
  var r = s % 60
  var mm = (h > 0 && m < 10) ? "0" + m : String(m)
  var ss = r < 10 ? "0" + r : String(r)
  if (h > 0) return h + ":" + mm + ":" + ss
  return mm + ":" + ss
}

function formatRemaining(posSec, lenSec) {
  var rem = (lenSec || 0) - (posSec || 0)
  if (!(rem > 0)) rem = 0
  // U+2212 MINUS SIGN, not U+002D HYPHEN-MINUS: the hyphen renders short and
  // sits off the numeric baseline in a tabular-figures run.
  return "−" + formatTime(rem)
}

// The daemon sends position without a timestamp; Service.qml stamps arrival.
// Interpolating against a clock this side owns avoids any cross-process clock
// assumption, and keeps this a pure function.
function position(zone, recvMs, nowMs) {
  if (!zone) return 0
  var base = zone.position || 0
  if (zone.state !== "playing") return base
  var elapsed = (nowMs - recvMs) / 1000
  if (!(elapsed > 0)) elapsed = 0
  var p = base + elapsed
  var len = zone.length || 0
  if (len > 0 && p > len) p = len
  return p
}

// Built, never typed. A literal astral character does not survive every editing
// path, and the failure mode is an invisible widget with nothing logged. The
// tests assert the CODEPOINT, because a shape check passes on a typo too.
var GLYPH_PLAYING = String.fromCodePoint(0xf040a)   // nf-md-play
var GLYPH_PAUSED  = String.fromCodePoint(0xf03e4)   // nf-md-pause
var GLYPH_IDLE    = String.fromCodePoint(0xf0387)   // nf-md-music
var GLYPH_FAULT   = String.fromCodePoint(0xf0026)   // nf-md-alert

// Severity colors live here, not in Commons/Color.qml: that exposes only
// background, foreground, accent and the per-surface roles -- there is no
// Color.red or Color.yellow to bind to. headway carries its own for the same
// reason. Chosen against the theme's own red and yellow.
var COLOR_ERROR = "#c38b7b"
var COLOR_WARN  = "#6B5E73"

// Read with hasOwnProperty only. A bare STATUS_SEVERITY[key] walks the
// prototype chain, so a status of "constructor" or "toString" would return a
// truthy inherited member and report a broken daemon as healthy.
var STATUS_SEVERITY = {
  ok: "ok",
  connecting: "warn",
  unpaired: "warn",
  unreachable: "error"
}

function severityFor(status) {
  if (typeof status !== "string") return "error"
  if (!Object.prototype.hasOwnProperty.call(STATUS_SEVERITY, status)) return "error"
  return STATUS_SEVERITY[status]
}

function nowPlayingOf(state) {
  if (!state || !state.zone || !state.zone.now_playing) return null
  return state.zone.now_playing
}

function barState(state, recvMs, nowMs) {
  // A null state means the relay has not delivered a line yet, which in
  // practice means tonearmd is not running.
  if (!state) return { severity: "error", glyph: GLYPH_FAULT, showArt: false }

  var severity = severityFor(state.status)
  if (severity !== "ok") {
    return { severity: severity, glyph: GLYPH_FAULT, showArt: false }
  }

  var zone = state.zone
  if (!zone) return { severity: "ok", glyph: GLYPH_IDLE, showArt: false }

  var np = nowPlayingOf(state)
  var showArt = !!(np && np.image_key)
  var glyph = GLYPH_IDLE
  if (zone.state === "playing") glyph = GLYPH_PLAYING
  else if (zone.state === "paused") glyph = GLYPH_PAUSED

  return { severity: "ok", glyph: glyph, showArt: showArt }
}

function tooltipText(state) {
  if (!state) return "tonearmd not running"
  var severity = severityFor(state.status)
  if (state.status === "unpaired") {
    return "Enable tonearm in Roon → Settings → Extensions"
  }
  if (severity !== "ok") return "Roon Core unreachable"

  var coreName = (state.core && state.core.name) ? state.core.name : "Roon"
  if (!state.zone) return "Nothing playing · " + coreName

  var np = nowPlayingOf(state)
  if (!np) return "Nothing playing · " + coreName
  var head = (np.title || "Unknown title")
  if (np.artist) head = head + " — " + np.artist
  return head + " · " + (state.zone.name || coreName)
}

function zoneList(state) {
  if (!state || !state.zones) return []
  var pinnedId = (state.zone && state.zone.pinned) ? state.zone.id : null
  var out = []
  for (var i = 0; i < state.zones.length; i++) out.push(state.zones[i])
  out.sort(function (a, b) {
    var ap = (a.id === pinnedId) ? 0 : 1
    var bp = (b.id === pinnedId) ? 0 : 1
    if (ap !== bp) return ap - bp
    var an = String(a.name || "")
    var bn = String(b.name || "")
    if (an < bn) return -1
    if (an > bn) return 1
    // Total order. Without this, equal names reshuffle under V4's unstable sort
    // and the switcher visibly jumps between pushes.
    var ai = String(a.id || "")
    var bi = String(b.id || "")
    if (ai < bi) return -1
    if (ai > bi) return 1
    return 0
  })
  return out
}

// With tonearmd down, `tonearmctl subscribe` exits immediately, so a Process
// that respawns on exit becomes a fork loop. Service.qml waits this long first.
function nextRetryDelay(attempt) {
  var n = attempt || 0
  if (n < 0) n = 0
  var ms = 1000 * Math.pow(2, n)
  return ms > 30000 ? 30000 : ms
}

if (typeof module !== "undefined") {
  module.exports = {
    THEME_ACCENT: THEME_ACCENT,
    THEME_BACKGROUND: THEME_BACKGROUND,
    CONTRAST_FLOOR: CONTRAST_FLOOR,
    normalizeHex: normalizeHex,
    relativeLuminance: relativeLuminance,
    contrastRatio: contrastRatio,
    saturation: saturation,
    pickAccent: pickAccent,
    formatTime: formatTime,
    formatRemaining: formatRemaining,
    position: position,
    GLYPH_PLAYING: GLYPH_PLAYING,
    GLYPH_PAUSED: GLYPH_PAUSED,
    GLYPH_IDLE: GLYPH_IDLE,
    GLYPH_FAULT: GLYPH_FAULT,
    COLOR_ERROR: COLOR_ERROR,
    COLOR_WARN: COLOR_WARN,
    barState: barState,
    tooltipText: tooltipText,
    zoneList: zoneList,
    nextRetryDelay: nextRetryDelay
  }
}
