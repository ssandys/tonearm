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

// pickAccent's lighten-a-dark-vivid-color path (see below). Below DRAB, a
// legible color is not carrying the record's character, so a lightened vivid
// one that clears the floor is preferred over it. Below MIN_LIGHTNESS, a
// pixel's hue is mostly compression/quantization noise -- without this guard
// a near-black entry like #060301 lightens into a confident vivid orange that
// is not actually in the artwork. Above MAX_LIGHTNESS a color has washed out
// and stopped reading as the record's own color.
var DRAB = 0.35
var MIN_LIGHTNESS = 0.10
var MAX_LIGHTNESS = 0.75
var LIGHTEN_STEP = 0.02

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

// RGB (0-255 channels) <-> HSL (h in [0,1), s and l in [0,1]). Used only by
// pickAccent's lighten path below, to raise a dark vivid color's lightness
// while preserving its hue and HSL saturation. saturation() above stays the
// HSV-style (max-min)/max metric pickAccent has always ranked by; these are
// a separate pair of helpers for the lighten transform only.
function rgbToHsl(r, g, b) {
  var rn = r / 255, gn = g / 255, bn = b / 255
  var max = Math.max(rn, Math.max(gn, bn))
  var min = Math.min(rn, Math.min(gn, bn))
  var l = (max + min) / 2
  var h = 0
  var s = 0
  if (max !== min) {
    var d = max - min
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min)
    if (max === rn) h = (gn - bn) / d + (gn < bn ? 6 : 0)
    else if (max === gn) h = (bn - rn) / d + 2
    else h = (rn - gn) / d + 4
    h = h / 6
  }
  return { h: h, s: s, l: l }
}

function hueToRgbChannel(p, q, t) {
  if (t < 0) t = t + 1
  if (t > 1) t = t - 1
  if (t < 1 / 6) return p + (q - p) * 6 * t
  if (t < 1 / 2) return q
  if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6
  return p
}

function hslToRgb(h, s, l) {
  var r, g, b
  if (s === 0) {
    r = l
    g = l
    b = l
  } else {
    var q = l < 0.5 ? l * (1 + s) : l + s - l * s
    var p = 2 * l - q
    r = hueToRgbChannel(p, q, h + 1 / 3)
    g = hueToRgbChannel(p, q, h)
    b = hueToRgbChannel(p, q, h - 1 / 3)
  }
  return {
    r: Math.round(r * 255),
    g: Math.round(g * 255),
    b: Math.round(b * 255)
  }
}

function clampByte(v) {
  return Math.max(0, Math.min(255, v))
}

function byteToHexPair(v) {
  var s = clampByte(v).toString(16)
  return s.length === 1 ? "0" + s : s
}

function hslToHex(h, s, l) {
  var c = hslToRgb(h, s, l)
  return "#" + byteToHexPair(c.r) + byteToHexPair(c.g) + byteToHexPair(c.b)
}

// Raise hex's lightness in LIGHTEN_STEP increments, hue and HSL saturation
// held fixed, until the result clears CONTRAST_FLOOR against bg. Returns null
// if it never clears the floor before MAX_LIGHTNESS.
function liftToContrast(hex, bg) {
  var c = channels(hex)
  if (c === null) return null
  var hsl = rgbToHsl(c.r, c.g, c.b)
  var l = hsl.l + LIGHTEN_STEP
  while (l <= MAX_LIGHTNESS) {
    var candidate = hslToHex(hsl.h, hsl.s, l)
    if (contrastRatio(candidate, bg) >= CONTRAST_FLOOR) return candidate
    l = l + LIGHTEN_STEP
  }
  return null
}

// Direction C: exactly one element takes its color from the cover. Choose the
// most saturated quantized entry that is still legible on the panel, and fall
// back to the theme accent when none is -- never darken or synthesize a color
// out of nothing.
//
// A cover that is dark throughout (a lot of album art is) used to fail this
// entirely: every vivid entry is dark, fails CONTRAST_FLOOR, and gets thrown
// away, leaving only a desaturated highlight that looks nothing like the
// record. So a dark vivid color is no longer discarded outright -- it is
// lightened (preserving hue/saturation) until it clears the floor, and that
// lightened color is used ONLY when nothing already-legible is vivid enough
// (DRAB) to carry the record's character on its own. Saturation is always
// judged on the ORIGINAL color in both buckets, never the lightened one --
// that original is the record's actual character; lightening is only how it
// is made legible.
function pickAccent(colors, bgHex) {
  var bg = normalizeHex(bgHex)
  if (bg === null) bg = THEME_BACKGROUND
  var list = colors || []

  var bestLegible = null
  var bestLegibleSat = -1
  var bestLiftedResult = null   // the lightened hex actually returned
  var bestLiftedOrig = null     // the original hex, for ranking/tie-break
  var bestLiftedSat = -1

  for (var i = 0; i < list.length; i++) {
    var hex = normalizeHex(list[i])
    if (hex === null) continue
    var s = saturation(hex)

    if (contrastRatio(hex, bg) >= CONTRAST_FLOOR) {
      // Strict > keeps the first of equals; the lexical compare then imposes
      // a total order so the result cannot depend on V4's unstable sort or
      // on the order the quantizer happened to emit.
      if (s > bestLegibleSat || (s === bestLegibleSat && bestLegible !== null && hex < bestLegible)) {
        bestLegibleSat = s
        bestLegible = hex
      }
      continue
    }

    var c = channels(hex)
    if (c === null) continue
    var l = rgbToHsl(c.r, c.g, c.b).l
    if (l < MIN_LIGHTNESS) continue   // compression-noise near-black, not a real hue
    var lifted = liftToContrast(hex, bg)
    if (lifted === null) continue     // still illegible even at MAX_LIGHTNESS

    if (s > bestLiftedSat || (s === bestLiftedSat && bestLiftedOrig !== null && hex < bestLiftedOrig)) {
      bestLiftedSat = s
      bestLiftedResult = lifted
      bestLiftedOrig = hex
    }
  }

  if (bestLegible !== null && bestLegibleSat >= DRAB) return bestLegible
  if (bestLiftedResult !== null) return bestLiftedResult
  if (bestLegible !== null) return bestLegible
  return THEME_ACCENT
}

function formatTime(sec) {
  var s = Math.floor(sec || 0)  // `|| 0` already replaces NaN/undefined with 0
  if (!(s > 0)) s = 0          // clamps a negative duration to zero
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
// GLYPH_PLAYING/GLYPH_PAUSED are POPUP-ONLY now: they label the popup's play
// button, which is an ACTION (paused shows play). The bar no longer uses
// either -- it shows GLYPH_VINYL in every healthy state instead. That split is
// the point: the same two glyphs previously meant a STATUS in the bar and an
// ACTION in the popup, so "paused" and "press to play" rendered identically a
// hundred pixels apart.
var GLYPH_PLAYING = String.fromCodePoint(0xf040a)   // nf-md-play
var GLYPH_PAUSED  = String.fromCodePoint(0xf03e4)   // nf-md-pause
var GLYPH_FAULT   = String.fromCodePoint(0xf0026)   // nf-md-alert

// The bar's product icon: this is tonearm, not a transport mime. Checked
// against the deployed font rather than assumed -- `fc-list :charset=efbd`
// resolves it to the SAME family (BitstromWera Nerd Font) that already serves
// the glyphs rendering in the bar today, so if the old icon drew, this one
// draws. Verify that way, not by eye: a missing PUA codepoint is an invisible
// widget with nothing logged.
var GLYPH_VINYL = String.fromCodePoint(0xefbd)      // nf-fa-record_vinyl

// Popup transport/volume glyphs. Same rule as the four above: built, never
// typed, and read out of the Nerd Font's own cmap rather than guessed --
// Panel.qml originally used plain Unicode media symbols (U+23EE/U+25B6/
// U+23F8/U+23ED) here, which carry emoji presentation in the deployed font
// and rendered as colour blocks instead of monochrome glyphs.
var GLYPH_PREV         = String.fromCodePoint(0xf04ae)   // nf-md-skip_previous
var GLYPH_NEXT         = String.fromCodePoint(0xf04ad)   // nf-md-skip_next
var GLYPH_VOLUME_HIGH  = String.fromCodePoint(0xf057e)   // nf-md-volume_high
var GLYPH_VOLUME_MUTED = String.fromCodePoint(0xf075f)   // nf-md-volume_mute

// Severity colors live here, not in Commons/Color.qml: that exposes only
// background, foreground, accent and the per-surface roles -- there is no
// Color.red or Color.yellow to bind to. headway carries its own for the same
// reason. Chosen against the theme's own red and yellow.
var COLOR_ERROR = "#c38b7b"
var COLOR_WARN  = "#6b5e73"

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
  if (!state) return { severity: "error", glyph: GLYPH_FAULT, showArt: false, playing: false }

  var severity = severityFor(state.status)
  if (severity !== "ok") {
    return { severity: severity, glyph: GLYPH_FAULT, showArt: false, playing: false }
  }

  var zone = state.zone
  if (!zone) return { severity: "ok", glyph: GLYPH_VINYL, showArt: false, playing: false }

  var np = nowPlayingOf(state)
  var showArt = !!(np && np.image_key)
  // Strict equality against "playing" alone, NOT zones.py's broader ACTIVE
  // tuple ("playing", "loading") -- that tuple answers "does this zone count
  // for auto-follow arbitration", a different question from "is audio
  // actually advancing right now". A buffering zone should not be shown at
  // full brightness or tick the seek clock; it renders as idle below, same
  // as any other non-playing state that isn't "paused".
  var playing = zone.state === "playing"

  // One icon for all three healthy states. `playing` is the only channel that
  // carries transport state to the bar, and Panel.qml renders it as
  // brightness; branching the glyph here as well would put the same fact in
  // two places, only one of them tested.
  return { severity: "ok", glyph: GLYPH_VINYL, showArt: showArt, playing: playing }
}

function tooltipText(state) {
  if (!state) return "tonearmd not running"
  var severity = severityFor(state.status)
  if (state.status === "unpaired") {
    return "Enable tonearm in Roon → Settings → Extensions"
  }
  if (state.status === "connecting") {
    // The daemon's normal startup state, reaching for the Core, which may not
    // even be resolved yet -- this is not a failure and must not read as one.
    var connectingName = (state.core && state.core.name) ? state.core.name : "Roon"
    return "Connecting to " + connectingName + "…"
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

// Whether `id` is the zone the daemon is currently pinned to. This exact
// three-clause comparison used to be duplicated character-for-character in
// two places in Panel.qml (the "pinned" row label and the pin/unpin click
// branch) -- both untested by design, since Panel.qml has no test coverage.
// One tested copy here instead of two untested ones there.
function isZonePinned(zone, id) {
  return !!(zone && zone.pinned && zone.id === id)
}

// Fraction of the volume's [min, max] range that `value` represents, for the
// slider fill width. 0 when there is no volume at all (a fixed-volume zone)
// or when min === max -- a genuine edge case, not a defensive nicety: a
// zero-width range makes "fraction of the range" undefined, and without this
// guard it divides by zero and renders NaN * width as the fill.
function volumeFraction(volume) {
  if (!volume || volume.max === volume.min) return 0
  return (volume.value - volume.min) / (volume.max - volume.min)
}

// Inverse of volumeFraction: the integer volume value a click at fraction
// `frac` of the track represents. Rounds here, not at the call site, because
// this is the one place that knows it is producing a volume value rather
// than a fill ratio. Clamps frac defensively even though Panel.qml's click
// handler already clamps before calling, since a stray caller must not be
// able to send an out-of-range volume.
function volumeFromFraction(volume, frac) {
  if (!volume) return 0
  if (volume.max === volume.min) return volume.min
  var f = frac
  if (!(f >= 0)) f = 0    // also catches NaN
  if (f > 1) f = 1
  return Math.round(volume.min + f * (volume.max - volume.min))
}

// With tonearmd down, `tonearmctl subscribe` exits immediately, so a Process
// that respawns on exit becomes a fork loop. Service.qml waits this long first.
function nextRetryDelay(attempt) {
  var n = attempt || 0
  if (n < 0) n = 0
  var ms = 1000 * Math.pow(2, n)
  return ms > 30000 ? 30000 : ms
}

// Extracted so browse rows and now-playing share one URL builder. Row art is
// loaded by the widget straight from the Core (spec 4.4): a plain Image can
// read these URLs -- only ColorQuantizer cannot -- so caching 100 images per
// page in the daemon would be pure waste.
function imageUrl(core, imageKey, px) {
  if (!core || !core.host) return ""
  if (!imageKey) return ""
  var size = px || 256
  return "http://" + core.host + ":" + (core.http_port || 9330) +
         "/api/image/" + imageKey +
         "?scale=fit&width=" + size + "&height=" + size
}

function artUrl(state, px) {
  if (!state) return ""
  var np = nowPlayingOf(state)
  return imageUrl(state.core, np && np.image_key, px)
}

function rowArtUrl(state, row, px) {
  if (!state || !row) return ""
  return imageUrl(state.core, row.image_key, px)
}

// Clamped, never wrapping. Wrapping in a short list makes Down feel like it
// jumped to the top by accident; clamping is what every list in the shell does.
// A count of 0 keeps the cursor at -1 ("nothing selected") rather than 0,
// which would select a row that is not there.
function moveCursor(current, delta, count) {
  if (!count || count <= 0) return -1
  var next = (current === undefined || current === null || current < 0)
    ? 0 : current + delta
  if (next < 0) return 0
  if (next > count - 1) return count - 1
  return next
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
    GLYPH_VINYL: GLYPH_VINYL,
    GLYPH_FAULT: GLYPH_FAULT,
    GLYPH_PREV: GLYPH_PREV,
    GLYPH_NEXT: GLYPH_NEXT,
    GLYPH_VOLUME_HIGH: GLYPH_VOLUME_HIGH,
    GLYPH_VOLUME_MUTED: GLYPH_VOLUME_MUTED,
    COLOR_ERROR: COLOR_ERROR,
    COLOR_WARN: COLOR_WARN,
    barState: barState,
    tooltipText: tooltipText,
    zoneList: zoneList,
    isZonePinned: isZonePinned,
    volumeFraction: volumeFraction,
    volumeFromFraction: volumeFromFraction,
    nextRetryDelay: nextRetryDelay,
    artUrl: artUrl,
    imageUrl: imageUrl,
    rowArtUrl: rowArtUrl,
    moveCursor: moveCursor
  }
}
