const test = require("node:test")
const assert = require("node:assert")
const fs = require("node:fs")
const path = require("node:path")

const ROOT = path.join(__dirname, "..")
const manifest = JSON.parse(fs.readFileSync(path.join(ROOT, "manifest.json"), "utf8"))

test("manifest declares a bar-widget entry point", () => {
  assert.strictEqual(manifest.id, "ssandys.tonearm")
  assert.deepStrictEqual(manifest.kinds, ["bar-widget"])
  assert.strictEqual(manifest.entryPoints.barWidget, "Panel.qml")
})

test("every schema key has a matching default", () => {
  const defaults = manifest.barWidget.defaults
  for (const entry of manifest.barWidget.schema) {
    assert.ok(
      Object.prototype.hasOwnProperty.call(defaults, entry.key),
      `schema key ${entry.key} has no default`
    )
    assert.strictEqual(defaults[entry.key], entry.defaultValue,
      `schema key ${entry.key} defaultValue disagrees with barWidget.defaults`)
  }
})

test("dev scripts stay plugin-agnostic", () => {
  // bin/dev and bin/dev-watch are copied byte-identical between plugins and
  // derive identity from manifest.json. A hardcoded id here breaks that.
  for (const script of ["dev", "dev-watch"]) {
    const body = fs.readFileSync(path.join(ROOT, "bin", script), "utf8")
    const code = body.split("\n").filter((l) => !l.trim().startsWith("#")).join("\n")
    assert.ok(!code.includes("tonearm"),
      `bin/${script} contains a plugin-specific literal outside a comment`)
  }
})
