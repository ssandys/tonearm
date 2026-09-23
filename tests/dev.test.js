// tests/dev.test.js
//
// bin/dev's restart path, driven for real against PATH-shimmed omarchy
// binaries and a scratch $HOME.
//
// `omarchy restart shell` kills the running shell and launches a replacement,
// and the launch can win that race: the new process finds the old still alive,
// refuses with "An instance of this configuration is already running", and the
// old then exits on the IPC request it had already been given. Nothing is left
// running, and bin/dev exited 0 and printed its success lines over a dead
// desktop -- which is what makes it hard to catch, along with there being no
// coredump, because a clean exit is not a crash. Observed on this machine on
// 2026-09-22 while verifying the singleton work, and independently in headway
// (fixed there in dab5a13, which this ports).
//
// The shim's `restart shell` deliberately starts nothing, which IS that
// failure -- so these drive the guard rather than the happy path.
const test = require("node:test")
const assert = require("node:assert/strict")
const { execFileSync } = require("node:child_process")
const fs = require("node:fs")
const os = require("node:os")
const path = require("node:path")

const REPO = path.join(__dirname, "..")

// A scratch HOME plus a PATH directory of shims. `pingSucceedsAfter` is which
// `shell ping` call first answers, so a test can make the shell come back
// late, or never at all. `restartExit` is what `omarchy restart shell` exits
// with -- NOT always 0: the real one exits 1 when it loses its own race, and a
// shim that only ever succeeds exercises the happy path of the very command
// whose unhappy path this guard exists for.
function scratch(pingSucceedsAfter, restartExit = 0) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tonearm-dev-"))
  const bin = path.join(dir, "bin")
  fs.mkdirSync(bin)
  const log = path.join(dir, "calls.log")
  const counter = path.join(dir, "pings")
  fs.writeFileSync(log, "")
  fs.writeFileSync(counter, "0")

  fs.writeFileSync(path.join(bin, "omarchy"),
    `#!/bin/bash\n` +
    `echo "omarchy $*" >> ${JSON.stringify(log)}\n` +
    // Word for word what omarchy-restart-shell:92 prints before exiting 1.
    `if [[ "$1 $2" == "restart shell" ]]; then\n` +
    `  if (( ${restartExit} != 0 )); then\n` +
    `    echo "Omarchy shell did not become ready after restart." >&2\n` +
    `  fi\n` +
    `  exit ${restartExit}\n` +
    `fi\nexit 0\n`,
    { mode: 0o755 })

  // `shell ping` is the only call with behaviour: it fails until the nth ask,
  // standing in for a replacement shell that is not answering IPC yet.
  fs.writeFileSync(path.join(bin, "omarchy-shell"),
    `#!/bin/bash\n` +
    `echo "omarchy-shell $*" >> ${JSON.stringify(log)}\n` +
    `if [[ "$1 $2" == "shell ping" ]]; then\n` +
    `  n=$(cat ${JSON.stringify(counter)}); n=$((n + 1)); echo "$n" > ${JSON.stringify(counter)}\n` +
    `  if (( n >= ${pingSucceedsAfter} )); then echo ok; exit 0; fi\n` +
    `  exit 1\n` +
    `fi\nexit 0\n`,
    { mode: 0o755 })

  return {
    dir, bin,
    calls: () => fs.readFileSync(log, "utf8"),
    restarts: () => (fs.readFileSync(log, "utf8").match(/^omarchy restart shell$/gm) || []).length
  }
}

function runDev(s, verb) {
  try {
    const out = execFileSync(path.join(REPO, "bin", "dev"), [verb], {
      cwd: REPO, encoding: "utf8", timeout: 60000,
      env: Object.assign({}, process.env, {
        HOME: s.dir,
        PATH: s.bin + path.delimiter + process.env.PATH,
        // Bypasses the live registry query, the same hook wait_for_registration
        // is already driven through.
        DEV_STATE_FIXTURE: "enabled",
        DEV_SHELL_TIMEOUT: "1"
      })
    })
    return { code: 0, out }
  } catch (e) {
    return { code: e.status === undefined ? -1 : e.status,
             out: String(e.stdout || "") + String(e.stderr || "") }
  }
}

test("up succeeds, restarting once, when the shell answers straight away", () => {
  const s = scratch(1)
  const r = runDev(s, "up")
  assert.equal(r.code, 0, "should succeed: " + r.out)
  assert.equal(s.restarts(), 1, "one restart is enough when it works")
})

test("up waits for the shell rather than trusting the restart returned", () => {
  // The restart command returning tells you nothing: it returns before the
  // replacement is answering, which is the whole bug.
  const s = scratch(4)
  const r = runDev(s, "up")
  assert.equal(r.code, 0, "should succeed once the shell answers: " + r.out)
  assert.equal(s.restarts(), 1, "answering inside the first window needs no retry")
  assert.match(s.calls(), /omarchy-shell shell ping/, "it must actually ask")
})

test("up retries the restart once when the shell does not come back", () => {
  // Past the first window (1s at 0.2s intervals), inside the second.
  const s = scratch(7)
  const r = runDev(s, "up")
  assert.equal(r.code, 0, "the retry should recover it: " + r.out)
  assert.equal(s.restarts(), 2, "exactly one retry, not a loop")
})

test("up fails, naming the recovery, when two restarts do not bring it back", () => {
  const s = scratch(9999)
  const r = runDev(s, "up")
  assert.notEqual(r.code, 0, "a dead shell must not exit 0 with success lines")
  assert.match(r.out, /omarchy restart shell/,
    "the error must name the command that recovers it")
  assert.equal(s.restarts(), 2, "two attempts, then give up rather than thrash")
})

test("down guards its restart too", () => {
  // down() disables and restarts exactly like up(), so it can strand the
  // desktop exactly like up() -- and it is the command reached for when
  // something has already gone wrong.
  const s = scratch(9999)
  const r = runDev(s, "down")
  assert.notEqual(r.code, 0, "a dead shell must not exit 0 from down either: " + r.out)
  assert.equal(s.restarts(), 2, "same one-retry budget as up")
})

test("a failing restart does not abort the guard that exists for it", () => {
  // `omarchy restart shell` exits 1 when it loses its own race, printing
  // "Omarchy shell did not become ready after restart". Under `set -e` that
  // aborted restart_shell at its FIRST line -- before the wait and the retry
  // written for exactly this case -- so bin/dev exited carrying omarchy's
  // message and none of its own, having never once asked whether a shell was
  // there. Here one is, so the answer was available for the asking.
  const s = scratch(1, 1)
  const r = runDev(s, "up")
  assert.equal(r.code, 0, "a shell that IS answering must not be reported as failure: " + r.out)
  assert.match(s.calls(), /omarchy-shell shell ping/, "it has to ask before concluding")
  assert.equal(s.restarts(), 1, "no retry needed when the shell is up")
})

test("a failing restart still retries when nothing answers", () => {
  // The exit code is not the question; whether a shell answers afterwards is.
  // So a failing restart must reach the same retry-then-fail path as a silent
  // one, rather than short-circuiting it.
  const s = scratch(9999, 1)
  const r = runDev(s, "up")
  assert.notEqual(r.code, 0, "a dead shell must still fail")
  assert.match(r.out, /omarchy restart shell/,
    "the error must name the command that recovers it, not just relay omarchy's")
  assert.equal(s.restarts(), 2, "the retry must happen despite the non-zero exit")
})
