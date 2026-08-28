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

if (typeof module !== "undefined") {
  module.exports = {
    THEME_ACCENT: THEME_ACCENT,
    THEME_BACKGROUND: THEME_BACKGROUND,
    CONTRAST_FLOOR: CONTRAST_FLOOR,
    normalizeHex: normalizeHex,
    relativeLuminance: relativeLuminance,
    contrastRatio: contrastRatio,
    saturation: saturation,
    pickAccent: pickAccent
  }
}
