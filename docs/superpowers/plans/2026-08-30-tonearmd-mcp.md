# tonearmd-mcp Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An MCP server that lets Claude read tonearm's state, search the Roon library, and put music on, by consuming `tonearmd`'s unix socket.

**Architecture:** A stdio MCP server in plain JavaScript. It opens
`$XDG_RUNTIME_DIR/tonearm/sock` directly — one JSON line in, one line or EOF
out — and never shells out to `tonearmctl`. All decisions live in pure modules
(`codec`, `candidates`, `zones`); `client.js` holds I/O and no decisions;
`server.js` is tool wiring only.

**Tech Stack:** Node (>=20), `@modelcontextprotocol/sdk` 1.30.0, `zod`,
`node:test` + `node:assert`. ESM. No build step, no TypeScript, no framework.

**Spec:** `docs/superpowers/specs/2026-08-30-tonearm-mcp-design.md` (branch
`mcp-spec` of `~/Src/tonearm`). Read it first; this plan argues from it.

## Global Constraints

- **New repo `tonearmd-mcp`.** Nothing in this plan modifies `~/Src/tonearm`.
- **No `tonearm` daemon changes.** Everything works against the daemon as it
  stands at commit `d4a3513`.
- **Session key is the constant string `"mcp"`** on every browse request. Never
  generate one per request — `FOLLOWUPS` item 9: the daemon's session dict is
  unbounded.
- **Plain JavaScript, ESM.** `"type": "module"` in package.json. No
  TypeScript, no build step, no transpiler.
- **Tests are `node:test` + `node:assert` only.** No jest, no mocha, no chai.
- **Every test must be verified able to fail.** Break the implementation,
  observe the failure, record the message, restore.
- **No zone arguments on `play` or `control`.** Spec §6. Those act on the
  followed zone. `transfer` and `pin` take a zone because naming a destination
  is their purpose.
- **Expansion caps are 10 albums and 10 tracks.** Spec §5.

---

## Measured facts this plan depends on

Every one was read from the running daemon or the installed SDK on 2026-08-30.
Do not re-derive; do not assume.

### The MCP SDK (verified by running it)

- Version `1.30.0`. `zod` is a required peer (`^3.25 || ^4.0`).
- `import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js"`
- `import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js"`
- `server.registerTool(name, { description, inputSchema }, cb)` — **`server.tool()`
  is deprecated**, do not use it.
- `inputSchema` is a **Zod raw shape** — a plain object of validators,
  `{ query: z.string() }`, *not* `z.object({...})`.
- A tool callback returns `{ content: [{ type: "text", text: "..." }] }`.

### The daemon wire protocol

Requests are one JSON object per line. Replies are one JSON line, **or EOF**.

| Intent | Request | Reply |
|---|---|---|
| State | `{"cmd":"status"}` | one line |
| Search | `{"cmd":"browse","session":"mcp","op":"search","term":"kind of blue"}` | one line |
| Descend | `{"cmd":"browse","session":"mcp","op":"enter","index":2,"level_id":1}` | one line |
| Play | `{"cmd":"browse","session":"mcp","op":"activate","index":0,"level_id":2}` | one line |
| Reset | `{"cmd":"browse","session":"mcp","op":"reset"}` | one line |
| Transport | `{"cmd":"playpause"}` | **EOF, no reply** |
| Transfer | `{"cmd":"transfer","arg":"<zone id>"}` | **EOF, no reply** |
| Pin | `{"cmd":"zone","arg":"<zone id>"}` | **EOF, no reply** |
| Unpin | `{"cmd":"zone","arg":"unpin"}` | **EOF, no reply** |

**Commands are fire-and-forget.** *(measured: a `playpause` request returned
EOF in 0.05s, never a line.)* The daemon does not confirm them —
`_command_locked` logs a warning and drops the command on an unknown verb or
when no zone is followed. So a tool that sends a command **must re-read
`status` to report what actually happened**, never echo back what it asked
for. This is the same failure shape as the `played: true`-while-silent bug
tonearm shipped and had to fix.

### Reply shapes (captured live)

`status`:

```json
{"v":1,"status":"ok",
 "core":{"host":"192.168.50.118","http_port":9330,"name":"yavin"},
 "zone":{"id":"16015aef...","name":"sonos move","state":"stopped","pinned":true,
         "volume":{"value":70,"min":1,"max":100,"step":1,"muted":false},
         "position":0,"length":0,"now_playing":null},
 "zones":[{"id":"16012352...","name":"chimaera","state":"paused"},
          {"id":"16015aef...","name":"sonos move","state":"stopped"},
          {"id":"1601bdb5...","name":"Living Room Stereo","state":"stopped"}]}
```

`browse search`: keys are `v, ok, level_id, path, rows, count, offset`.
A row's keys are exactly `title, subtitle, image_key, can_play, can_descend`.

```
level_id=1  path=["Search"]
  0  Kind Of Blue    can_play=true can_descend=true
  1  Artists         can_play=true can_descend=true
  2  Albums          can_play=true can_descend=true
  3  Composers       can_play=true can_descend=true
  4  Tracks          can_play=true can_descend=true
  5  Works           can_play=true can_descend=true
```

**`can_play` is `true` on every row, including the category headers.** It
cannot be used to identify playable items. This is why the expansion policy
matches on category *title* and is not a convenience. *(measured; confirms
spec §2.4.)*

`browse enter 2 1` (the `Albums` category):

```
level_id=2  path=["Search","Albums"]  count=32
  0  Kind Of Blue      | Miles Davis
  1  Kind of Blue      | Swiss Blues Authority
  2  My Kind Of Blues  | B.B. King
```

A stale request returns, and **carries the refreshed level**:

```json
{"ok":false,"error":"stale","message":"the view is out of date; it has been refreshed","level_id":2}
```

---

## File structure

| File | Responsibility |
|---|---|
| `package.json` | ESM, deps, `test` script |
| `src/client.js` | Unix socket: one request, one line or EOF. Deadlines. No decisions. |
| `src/codec.js` | `encodeRef` / `decodeRef` |
| `src/zones.js` | Zone name → id |
| `src/candidates.js` | A search reply + category replies → candidate list |
| `src/library.js` | The two walks: `search` and `playRef` |
| `src/server.js` | The six tools; stdio wiring. Entry point. |
| `test/*.test.js` | One test file per source module |
| `test/fixtures/*.json` | Daemon replies captured from the live Core |

---

## Task 1: Repo scaffold and the socket client

**Files:**
- Create: `package.json`, `.gitignore`, `LICENSE`, `README.md`, `src/client.js`
- Test: `test/client.test.js`

**Interfaces:**
- Produces: `connect(socketPath)` is not exported. The module exports
  `request(payload, {socketPath, timeoutMs, expectReply})` returning
  `Promise<object|null>` — the parsed reply, or `null` when the daemon closed
  without one. Throws `DaemonDownError` / `DaemonSilentError`, both exported.

- [ ] **Step 1: Create the repo and scaffold**

```bash
mkdir -p ~/Src/tonearmd-mcp/src ~/Src/tonearmd-mcp/test/fixtures
cd ~/Src/tonearmd-mcp
git init
```

`package.json`:

```json
{
  "name": "tonearmd-mcp",
  "version": "0.1.0",
  "description": "MCP server for tonearmd: read Roon state, search the library, put music on.",
  "type": "module",
  "license": "MIT",
  "engines": { "node": ">=20" },
  "bin": { "tonearmd-mcp": "src/server.js" },
  "scripts": { "test": "node --test" },
  "dependencies": {
    "@modelcontextprotocol/sdk": "1.30.0",
    "zod": "^3.25.1"
  }
}
```

`.gitignore`:

```
node_modules/
```

`LICENSE`: MIT, `Copyright (c) 2026 Sean Sandys`. Copy the text verbatim from
`~/Src/tonearm/LICENSE` lines 1-21 (the MIT grant only — do **not** copy
tonearm's Dependencies section; this project vendors nothing).

`README.md`: a stub with a single line — "MCP server for tonearmd." Task 7
fills it in.

- [ ] **Step 2: Write the failing test**

`test/client.test.js`:

```js
import test from "node:test";
import assert from "node:assert";
import net from "node:net";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { request, DaemonDownError, DaemonSilentError } from "../src/client.js";

function tmpSocket() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tonearmd-mcp-"));
  return path.join(dir, "sock");
}

// Stands up a stub daemon. `handler(line, conn)` decides what to send back.
function stubDaemon(socketPath, handler) {
  const server = net.createServer((conn) => {
    let buf = "";
    conn.on("data", (chunk) => {
      buf += chunk;
      const nl = buf.indexOf("\n");
      if (nl === -1) return;
      handler(buf.slice(0, nl), conn);
    });
  });
  server.listen(socketPath);
  return server;
}

test("a reply-bearing request returns the parsed reply", async () => {
  const socketPath = tmpSocket();
  const server = stubDaemon(socketPath, (line, conn) => {
    assert.deepStrictEqual(JSON.parse(line), { cmd: "status" });
    conn.end(JSON.stringify({ v: 1, status: "ok" }) + "\n");
  });
  try {
    const reply = await request({ cmd: "status" }, { socketPath });
    assert.strictEqual(reply.status, "ok");
  } finally {
    server.close();
  }
});

test("a fire-and-forget command resolves null on EOF", async () => {
  // MEASURED: the daemon closes without writing for playpause/transfer/zone.
  const socketPath = tmpSocket();
  const server = stubDaemon(socketPath, (_line, conn) => conn.end());
  try {
    const reply = await request({ cmd: "playpause" }, { socketPath, expectReply: false });
    assert.strictEqual(reply, null);
  } finally {
    server.close();
  }
});

test("no daemon at the path raises DaemonDownError", async () => {
  await assert.rejects(
    () => request({ cmd: "status" }, { socketPath: tmpSocket() }),
    DaemonDownError,
  );
});

test("a daemon that accepts and never answers raises DaemonSilentError", async () => {
  // tonearmctl's lesson: a bare read with no deadline blocks forever.
  const socketPath = tmpSocket();
  const held = [];
  const server = stubDaemon(socketPath, (_line, conn) => held.push(conn));
  try {
    await assert.rejects(
      () => request({ cmd: "status" }, { socketPath, timeoutMs: 200 }),
      DaemonSilentError,
    );
  } finally {
    held.forEach((c) => c.destroy());
    server.close();
  }
});

test("the silent case gives up on the deadline, not much later", async () => {
  // Asserting only "it rejected" passes even if the deadline never fires and
  // something else ends the wait. Bound WHEN.
  const socketPath = tmpSocket();
  const held = [];
  const server = stubDaemon(socketPath, (_line, conn) => held.push(conn));
  try {
    const started = Date.now();
    await assert.rejects(() => request({ cmd: "status" }, { socketPath, timeoutMs: 200 }));
    const elapsed = Date.now() - started;
    assert.ok(elapsed < 1500, `gave up after ${elapsed}ms; the deadline is 200ms`);
  } finally {
    held.forEach((c) => c.destroy());
    server.close();
  }
});
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `cd ~/Src/tonearmd-mcp && npm install && node --test test/client.test.js`
Expected: FAIL — `Cannot find module '../src/client.js'`.

- [ ] **Step 4: Write the implementation**

`src/client.js`:

```js
import net from "node:net";
import os from "node:os";
import path from "node:path";

// The largest real request is a browse search whose term the user typed.
// A reply is a browse level: at most 100 rows (the daemon's PAGE), so 1 MiB
// is orders of magnitude clear of it while still bounding a wedged peer.
const MAX_REPLY_BYTES = 1024 * 1024;

// status is a snapshot the daemon already holds. browse goes out to Roon, and
// a play walks several levels; measured live, a browse search round-trips in
// about 0.9s. These are deliberately per-intent rather than one shared number.
export const TIMEOUTS = { status: 5000, browse: 25000, command: 5000 };

export class DaemonDownError extends Error {}
export class DaemonSilentError extends Error {}
export class DaemonProtocolError extends Error {}

export function defaultSocketPath() {
  const base = process.env.XDG_RUNTIME_DIR || path.join("/run/user", String(os.userInfo().uid));
  return path.join(base, "tonearm", "sock");
}

export function request(payload, opts = {}) {
  const socketPath = opts.socketPath || defaultSocketPath();
  const timeoutMs = opts.timeoutMs ?? TIMEOUTS.status;
  const expectReply = opts.expectReply !== false;

  return new Promise((resolve, reject) => {
    const conn = net.createConnection({ path: socketPath });
    let buf = "";
    let settled = false;

    const finish = (fn, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      conn.destroy();
      fn(value);
    };

    const timer = setTimeout(
      () => finish(reject, new DaemonSilentError(
        `tonearmd accepted the connection but did not answer within ${timeoutMs}ms`)),
      timeoutMs,
    );

    conn.on("error", () => finish(reject, new DaemonDownError(
      `tonearmd is not running (no socket at ${socketPath})`)));

    conn.on("connect", () => conn.write(JSON.stringify(payload) + "\n"));

    conn.on("data", (chunk) => {
      buf += chunk;
      if (buf.length > MAX_REPLY_BYTES) {
        return finish(reject, new DaemonProtocolError("reply exceeded 1 MiB"));
      }
      const nl = buf.indexOf("\n");
      if (nl === -1) return;
      try {
        finish(resolve, JSON.parse(buf.slice(0, nl)));
      } catch {
        finish(reject, new DaemonProtocolError("reply was not valid JSON"));
      }
    });

    // EOF. For a command this is success; for anything else it means the
    // daemon hung up without answering.
    conn.on("end", () => {
      if (!expectReply) return finish(resolve, null);
      if (buf.trim() === "") {
        return finish(reject, new DaemonSilentError("tonearmd closed without a reply"));
      }
    });
  });
}
```

- [ ] **Step 5: Run the tests**

Run: `node --test test/client.test.js`
Expected: PASS, 5 tests.

- [ ] **Step 6: Verify each test can fail**

```bash
# Delete the deadline -> the two silent-daemon tests must fail.
# Restore, then make `end` always resolve -> "closed without a reply" must fail.
```

Record the exact failure message for each. Restore after each.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat: socket client with per-intent deadlines"
```

---

## Task 2: The ref codec

**Files:**
- Create: `src/codec.js`
- Test: `test/codec.test.js`

**Interfaces:**
- Consumes: nothing.
- Produces: `encodeRef({ query, category, index, title }) -> string` and
  `decodeRef(string) -> { query, category, index, title }`. `decodeRef` throws
  `BadRefError` (exported) on anything it did not mint.

- [ ] **Step 1: Write the failing test**

`test/codec.test.js`:

```js
import test from "node:test";
import assert from "node:assert";
import { encodeRef, decodeRef, BadRefError } from "../src/codec.js";

const REF = { query: "kind of blue", category: "Albums", index: 0, title: "Kind Of Blue" };

test("a ref round-trips", () => {
  assert.deepStrictEqual(decodeRef(encodeRef(REF)), REF);
});

test("a ref is opaque", () => {
  // Claude should pass back what it was given, not construct one. Opacity is
  // not security -- the title check in library.js is -- but it keeps the tool
  // contract to "return the ref you received".
  const encoded = encodeRef(REF);
  assert.ok(!encoded.includes("kind of blue"));
  assert.ok(!encoded.includes("Albums"));
});

test("garbage is rejected, not guessed at", () => {
  for (const bad of ["", "not-a-ref", "!!!!", "eyJ"]) {
    assert.throws(() => decodeRef(bad), BadRefError, `accepted ${JSON.stringify(bad)}`);
  }
});

test("valid base64 of the wrong shape is rejected", () => {
  // The sharp case: decodes cleanly, is not a ref. Without a shape check this
  // reaches the walk and fails somewhere much less legible.
  const wrong = Buffer.from(JSON.stringify({ hello: "world" })).toString("base64url");
  assert.throws(() => decodeRef(wrong), BadRefError);
});

test("a non-integer index is rejected", () => {
  const bad = Buffer.from(JSON.stringify({ ...REF, index: "0" })).toString("base64url");
  assert.throws(() => decodeRef(bad), BadRefError);
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `node --test test/codec.test.js`
Expected: FAIL — `Cannot find module '../src/codec.js'`.

- [ ] **Step 3: Write the implementation**

`src/codec.js`:

```js
export class BadRefError extends Error {}

export function encodeRef({ query, category, index, title }) {
  return Buffer.from(JSON.stringify({ query, category, index, title })).toString("base64url");
}

export function decodeRef(encoded) {
  let parsed;
  try {
    parsed = JSON.parse(Buffer.from(String(encoded), "base64url").toString("utf8"));
  } catch {
    throw new BadRefError("that is not a ref this server issued");
  }
  const ok =
    parsed !== null && typeof parsed === "object" &&
    typeof parsed.query === "string" &&
    typeof parsed.category === "string" &&
    Number.isInteger(parsed.index) &&
    typeof parsed.title === "string";
  if (!ok) throw new BadRefError("that is not a ref this server issued");
  const { query, category, index, title } = parsed;
  return { query, category, index, title };
}
```

- [ ] **Step 4: Run the tests**

Run: `node --test test/codec.test.js`
Expected: PASS, 5 tests.

- [ ] **Step 5: Verify each test can fail**

Replace the shape check with `const ok = true` — the wrong-shape and
non-integer tests must fail. Replace `base64url` with plain JSON — the opacity
test must fail. Restore.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: opaque ref codec"
```

---

## Task 3: Zone name resolution

**Files:**
- Create: `src/zones.js`
- Test: `test/zones.test.js`

**Interfaces:**
- Consumes: a `status` reply's `zones` array — objects of
  `{ id, name, state }`.
- Produces: `resolveZone(zones, name) -> { id, name }` and the exported
  `UnknownZoneError`, whose `.available` is a `string[]` of zone names.

- [ ] **Step 1: Write the failing test**

`test/zones.test.js`:

```js
import test from "node:test";
import assert from "node:assert";
import { resolveZone, UnknownZoneError } from "../src/zones.js";

// Captured live 2026-08-30.
const ZONES = [
  { id: "16012352e4acb1f5e9bae8bec7bf5df87fa4", name: "chimaera", state: "paused" },
  { id: "16015aef4547fc69dbf0aea58d836c52153d", name: "sonos move", state: "stopped" },
  { id: "1601bdb56757fb6c57dedd8a2d4adcfcd486", name: "Living Room Stereo", state: "stopped" },
];

test("an exact name resolves to its id", () => {
  assert.strictEqual(resolveZone(ZONES, "chimaera").id, ZONES[0].id);
});

test("case does not matter", () => {
  // Claude will say "living room stereo"; Roon calls it "Living Room Stereo".
  assert.strictEqual(resolveZone(ZONES, "living room stereo").id, ZONES[2].id);
  assert.strictEqual(resolveZone(ZONES, "SONOS MOVE").id, ZONES[1].id);
});

test("surrounding whitespace does not matter", () => {
  assert.strictEqual(resolveZone(ZONES, "  chimaera ").id, ZONES[0].id);
});

test("an id also resolves, so a ref-free caller can pass one through", () => {
  assert.strictEqual(resolveZone(ZONES, ZONES[1].id).id, ZONES[1].id);
});

test("an unknown zone names the ones that exist", () => {
  // So Claude corrects itself in one turn instead of guessing again.
  try {
    resolveZone(ZONES, "kitchen");
    assert.fail("should have thrown");
  } catch (err) {
    assert.ok(err instanceof UnknownZoneError);
    assert.deepStrictEqual(err.available, ["chimaera", "sonos move", "Living Room Stereo"]);
    assert.ok(err.message.includes("kitchen"));
  }
});

test("no zones at all is still an UnknownZoneError, with an empty list", () => {
  // Node's assert.throws returns undefined -- it is NOT Python's assertRaises
  // context manager, and `const err = assert.throws(...)` yields undefined.
  // Inspect the error with a validation function instead.
  assert.throws(() => resolveZone([], "kitchen"), (err) => {
    assert.ok(err instanceof UnknownZoneError);
    assert.deepStrictEqual(err.available, []);
    return true;
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `node --test test/zones.test.js`
Expected: FAIL — `Cannot find module '../src/zones.js'`.

- [ ] **Step 3: Write the implementation**

`src/zones.js`:

```js
export class UnknownZoneError extends Error {
  constructor(name, available) {
    super(`no zone named ${JSON.stringify(name)}. Available: ${available.join(", ") || "none"}`);
    this.available = available;
  }
}

export function resolveZone(zones, name) {
  const list = Array.isArray(zones) ? zones : [];
  const wanted = String(name ?? "").trim().toLowerCase();
  const hit = list.find(
    (z) => String(z.id) === String(name ?? "").trim() ||
           String(z.name ?? "").toLowerCase() === wanted,
  );
  if (!hit) throw new UnknownZoneError(name, list.map((z) => z.name));
  return { id: hit.id, name: hit.name };
}
```

- [ ] **Step 4: Run the tests**

Run: `node --test test/zones.test.js`
Expected: PASS, 6 tests.

Note: pass test **files**, or run bare `node --test` for auto-discovery. Passing
the `test/` **directory** throws `MODULE_NOT_FOUND` -- the same trap tonearm's
own `bin/test` documents.

- [ ] **Step 5: Verify each test can fail**

Drop `.toLowerCase()` — the case test must fail. Drop `.trim()` — the
whitespace test must fail. Replace `err.available` with `[]` — the
unknown-zone test must fail. Restore after each.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: zone name resolution"
```

---

## Task 4: The expansion policy

**Files:**
- Create: `src/candidates.js`
- Test: `test/candidates.test.js`
- Test fixtures: `test/fixtures/search-kind-of-blue.json`,
  `test/fixtures/albums-kind-of-blue.json`, `test/fixtures/search-zero.json`

**Interfaces:**
- Consumes: `encodeRef` from `src/codec.js`.
- Produces: `EXPANDABLE` (the exported array `["Albums", "Tracks"]`),
  `PER_CATEGORY_CAP` (the exported number `10`),
  `categoryIndex(searchReply, category) -> number|null`, and
  `buildCandidates(query, byCategory) -> Candidate[]` where `byCategory` is
  `{ [category]: rows[] }` and a `Candidate` is
  `{ ref, kind, title, subtitle }`.

- [ ] **Step 1: Capture the fixtures from the live Core**

```bash
cd ~/Src/tonearm
./scripts/tonearmctl browse search "kind of blue" \
  > ~/Src/tonearmd-mcp/test/fixtures/search-kind-of-blue.json
# level_id comes from the reply above; Albums is index 2.
./scripts/tonearmctl browse enter 2 1 \
  > ~/Src/tonearmd-mcp/test/fixtures/albums-kind-of-blue.json
./scripts/tonearmctl browse reset > /dev/null
./scripts/tonearmctl browse search "zzzzznotathing" \
  > ~/Src/tonearmd-mcp/test/fixtures/search-zero.json
./scripts/tonearmctl browse reset > /dev/null
```

Open each and confirm it parses and has the shape in "Measured facts" above.
If `Albums` is not at index 2 in your capture, use the index it actually has —
do not edit the fixture.

- [ ] **Step 2: Write the failing test**

`test/candidates.test.js`:

```js
import test from "node:test";
import assert from "node:assert";
import fs from "node:fs";
import { EXPANDABLE, PER_CATEGORY_CAP, categoryIndex, buildCandidates } from "../src/candidates.js";
import { decodeRef } from "../src/codec.js";

const fixture = (n) => JSON.parse(fs.readFileSync(new URL(`./fixtures/${n}.json`, import.meta.url)));
const SEARCH = fixture("search-kind-of-blue");
const ALBUMS = fixture("albums-kind-of-blue");

test("only Albums and Tracks are expanded", () => {
  // Artists is two levels from anything playable; Composers and Works are
  // classical-specific and rarely what "play X" means.
  assert.deepStrictEqual(EXPANDABLE, ["Albums", "Tracks"]);
});

test("categoryIndex finds a category by title", () => {
  assert.strictEqual(typeof categoryIndex(SEARCH, "Albums"), "number");
  assert.strictEqual(SEARCH.rows[categoryIndex(SEARCH, "Albums")].title, "Albums");
});

test("categoryIndex returns null for a category the search did not return", () => {
  assert.strictEqual(categoryIndex(SEARCH, "Playlists"), null);
});

test("candidates carry title, subtitle and kind", () => {
  const out = buildCandidates("kind of blue", { Albums: ALBUMS.rows });
  assert.ok(out.length > 0);
  assert.strictEqual(out[0].kind, "album");
  assert.strictEqual(out[0].title, ALBUMS.rows[0].title);
  assert.strictEqual(out[0].subtitle, ALBUMS.rows[0].subtitle);
});

test("each ref round-trips to the row that produced it", () => {
  const out = buildCandidates("kind of blue", { Albums: ALBUMS.rows });
  const ref = decodeRef(out[3].ref);
  assert.strictEqual(ref.query, "kind of blue");
  assert.strictEqual(ref.category, "Albums");
  assert.strictEqual(ref.index, 3);
  assert.strictEqual(ref.title, ALBUMS.rows[3].title);
});

test("each category is capped at ten", () => {
  // The captured Albums level has 32 rows. Unbounded, one search would spend
  // Claude's context on 32 albums plus however many tracks.
  assert.ok(ALBUMS.rows.length > PER_CATEGORY_CAP, "fixture must exceed the cap to test it");
  const out = buildCandidates("kind of blue", { Albums: ALBUMS.rows });
  assert.strictEqual(out.length, PER_CATEGORY_CAP);
});

test("the cap is per category, not overall", () => {
  const out = buildCandidates("q", { Albums: ALBUMS.rows, Tracks: ALBUMS.rows });
  assert.strictEqual(out.length, PER_CATEGORY_CAP * 2);
});

test("a missing category contributes nothing rather than throwing", () => {
  assert.deepStrictEqual(buildCandidates("q", {}), []);
  assert.deepStrictEqual(buildCandidates("q", { Albums: [] }), []);
});
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `node --test test/candidates.test.js`
Expected: FAIL — `Cannot find module '../src/candidates.js'`.

- [ ] **Step 4: Write the implementation**

`src/candidates.js`:

```js
import { encodeRef } from "./codec.js";

// Albums and Tracks are the two categories that play. Artists is two levels
// from anything playable; Composers and Works are classical-specific.
export const EXPANDABLE = ["Albums", "Tracks"];
export const PER_CATEGORY_CAP = 10;

const KIND = { Albums: "album", Tracks: "track" };

// Matched on TITLE, not on can_play. Measured 2026-08-30: can_play is true on
// every row including the category headers, so it cannot identify a playable
// item. Confirms browse design §2.4.
export function categoryIndex(searchReply, category) {
  const rows = searchReply?.rows ?? [];
  const i = rows.findIndex((r) => r.title === category);
  return i === -1 ? null : i;
}

export function buildCandidates(query, byCategory) {
  const out = [];
  for (const category of EXPANDABLE) {
    const rows = byCategory?.[category] ?? [];
    rows.slice(0, PER_CATEGORY_CAP).forEach((row, index) => {
      out.push({
        ref: encodeRef({ query, category, index, title: row.title }),
        kind: KIND[category],
        title: row.title,
        subtitle: row.subtitle ?? null,
      });
    });
  }
  return out;
}
```

- [ ] **Step 5: Run the tests**

Run: `node --test test/candidates.test.js`
Expected: PASS, 8 tests.

- [ ] **Step 6: Verify each test can fail**

Remove `.slice(0, PER_CATEGORY_CAP)` — both cap tests must fail. Add
`"Artists"` to `EXPANDABLE` — the first test must fail. Match on
`r.can_play` instead of title — `categoryIndex` tests must fail. Restore.

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "feat: search expansion policy, capped per category"
```

---

## Task 5: The two walks

**Files:**
- Create: `src/library.js`
- Test: `test/library.test.js`

**Interfaces:**
- Consumes: `request`/`TIMEOUTS` from `src/client.js`, `decodeRef` from
  `src/codec.js`, `categoryIndex`/`buildCandidates`/`EXPANDABLE` from
  `src/candidates.js`.
- Produces: `searchLibrary(query, deps) -> Candidate[]` and
  `playRef(refString, deps) -> { title, subtitle }`, plus the exported
  `RefStaleError`. `deps` is `{ request }`, injected so tests need no socket.

- [ ] **Step 1: Write the failing test**

`test/library.test.js`:

```js
import test from "node:test";
import assert from "node:assert";
import fs from "node:fs";
import { searchLibrary, playRef, RefStaleError } from "../src/library.js";
import { encodeRef } from "../src/codec.js";

const fixture = (n) => JSON.parse(fs.readFileSync(new URL(`./fixtures/${n}.json`, import.meta.url)));
const SEARCH = fixture("search-kind-of-blue");
const ALBUMS = fixture("albums-kind-of-blue");

// A fake daemon that records what it was asked and replays fixtures.
function fakeDaemon({ albums = ALBUMS, activate = { ok: true, played: true } } = {}) {
  const calls = [];
  const request = async (payload) => {
    calls.push(payload);
    if (payload.op === "search") return SEARCH;
    if (payload.op === "enter") return albums;
    if (payload.op === "back") return SEARCH;
    if (payload.op === "activate") return activate;
    if (payload.op === "reset") return { ok: true };
    throw new Error(`unexpected op ${payload.op}`);
  };
  return { request, calls };
}

test("search sends the mcp session key on every browse call", () => {
  // FOLLOWUPS 9: a consumer minting a fresh key per request leaks sessions in
  // the daemon's unbounded dict.
  const d = fakeDaemon();
  return searchLibrary("kind of blue", d).then(() => {
    assert.ok(d.calls.length > 0);
    for (const c of d.calls) assert.strictEqual(c.session, "mcp");
  });
});

test("search descends into Albums and returns candidates", async () => {
  const d = fakeDaemon();
  const out = await searchLibrary("kind of blue", d);
  assert.ok(out.length > 0);
  assert.strictEqual(out[0].title, ALBUMS.rows[0].title);
  assert.ok(d.calls.some((c) => c.op === "enter"));
});

test("search carries the level_id the previous reply returned", async () => {
  // Index-addressed ops are rejected as stale against the wrong level.
  const d = fakeDaemon();
  await searchLibrary("kind of blue", d);
  const enter = d.calls.find((c) => c.op === "enter");
  assert.strictEqual(enter.level_id, SEARCH.level_id);
});

test("the second category is entered with the level_id back() returned", async () => {
  // MEASURED: back() bumps the generation counter (7 -> 8 -> 9), it does not
  // restore it. Reusing the original search level_id makes the second enter
  // stale, so Tracks would silently never be expanded.
  const levels = { search: 7, enter: 8, back: 9 };
  const calls = [];
  const request = async (payload) => {
    calls.push(payload);
    if (payload.op === "search") return { ...SEARCH, level_id: levels.search };
    if (payload.op === "enter") return { ...ALBUMS, level_id: levels.enter };
    if (payload.op === "back") return { ...SEARCH, level_id: levels.back };
    throw new Error(`unexpected op ${payload.op}`);
  };
  await searchLibrary("kind of blue", { request });
  const enters = calls.filter((c) => c.op === "enter");
  assert.strictEqual(enters.length, 2, "both Albums and Tracks should be entered");
  assert.strictEqual(enters[0].level_id, levels.search);
  assert.strictEqual(enters[1].level_id, levels.back, "second enter must use the post-back level");
});

test("play re-walks from the query rather than reusing a held level", async () => {
  const d = fakeDaemon();
  const ref = encodeRef({ query: "kind of blue", category: "Albums", index: 0, title: ALBUMS.rows[0].title });
  await playRef(ref, d);
  const ops = d.calls.map((c) => c.op);
  assert.deepStrictEqual(ops.slice(0, 3), ["search", "enter", "activate"]);
});

test("play refuses when the row at that index no longer has the ref's title", async () => {
  // THE safety property. Roon's ordering can shift between search and play;
  // without this the failure is playing the wrong album silently.
  const d = fakeDaemon();
  const ref = encodeRef({ query: "kind of blue", category: "Albums", index: 0, title: "Something Else Entirely" });
  await assert.rejects(() => playRef(ref, d), RefStaleError);
  assert.ok(!d.calls.some((c) => c.op === "activate"), "must not activate after a mismatch");
});

test("play refuses when the index is now out of range", async () => {
  const d = fakeDaemon({ albums: { ...ALBUMS, rows: ALBUMS.rows.slice(0, 2) } });
  const ref = encodeRef({ query: "kind of blue", category: "Albums", index: 9, title: "Kind Of Blue" });
  await assert.rejects(() => playRef(ref, d), RefStaleError);
});

test("play surfaces a daemon refusal rather than claiming success", async () => {
  // The daemon reports no_zone when nothing is selected to play into.
  const d = fakeDaemon({ activate: { ok: false, error: "no_zone", message: "no Roon zone is selected to play into" } });
  const ref = encodeRef({ query: "kind of blue", category: "Albums", index: 0, title: ALBUMS.rows[0].title });
  await assert.rejects(() => playRef(ref, d), /no Roon zone is selected/);
});

test("play refuses when the daemon reports it did not actually play", async () => {
  // activate descends instead of playing when the row is a category. Claude
  // must not be told music started.
  const d = fakeDaemon({ activate: { ok: true, played: false } });
  const ref = encodeRef({ query: "kind of blue", category: "Albums", index: 0, title: ALBUMS.rows[0].title });
  await assert.rejects(() => playRef(ref, d), /did not start/);
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `node --test test/library.test.js`
Expected: FAIL — `Cannot find module '../src/library.js'`.

- [ ] **Step 3: Write the implementation**

`src/library.js`:

```js
import { TIMEOUTS } from "./client.js";
import { decodeRef } from "./codec.js";
import { EXPANDABLE, categoryIndex, buildCandidates } from "./candidates.js";

export class RefStaleError extends Error {}
export class DaemonRefusedError extends Error {}

const SESSION = "mcp";
const browse = (deps, op, extra = {}) =>
  deps.request({ cmd: "browse", session: SESSION, op, ...extra },
               { timeoutMs: TIMEOUTS.browse });

function assertOk(reply, what) {
  if (reply && reply.ok === false) {
    throw new DaemonRefusedError(reply.message || `${what} failed (${reply.error})`);
  }
  return reply;
}

// search -> for each expandable category, enter it, read its rows, come back.
//
// `level` is reassigned from every reply and never cached. MEASURED
// 2026-08-30: back() BUMPS the generation counter rather than restoring the
// old one -- search gave level_id 7, entering Albums 8, and coming back 9, not
// 7. Reusing the original search level_id for the second category returns
// `stale`, so this loop would have yielded albums and never tracks.
export async function searchLibrary(query, deps) {
  let level = assertOk(await browse(deps, "search", { term: query }), "search");
  const byCategory = {};
  for (const category of EXPANDABLE) {
    const index = categoryIndex(level, category);
    if (index === null) continue;
    const entered = assertOk(
      await browse(deps, "enter", { index, level_id: level.level_id }), "enter");
    byCategory[category] = entered.rows ?? [];
    level = assertOk(await browse(deps, "back"), "back");
  }
  return buildCandidates(query, byCategory);
}

// A fresh walk every time. Holding a level across a Claude turn -- which can
// be minutes -- is the staleness that caused real widget bugs.
export async function playRef(refString, deps) {
  const ref = decodeRef(refString);
  const search = assertOk(await browse(deps, "search", { term: ref.query }), "search");

  const categoryAt = categoryIndex(search, ref.category);
  if (categoryAt === null) {
    throw new RefStaleError(`the ${ref.category} results are no longer there; search again`);
  }
  const level = assertOk(
    await browse(deps, "enter", { index: categoryAt, level_id: search.level_id }), "enter");

  const row = (level.rows ?? [])[ref.index];
  if (!row || row.title !== ref.title) {
    throw new RefStaleError(
      `the results moved: expected ${JSON.stringify(ref.title)} at position ${ref.index}, ` +
      `found ${JSON.stringify(row?.title ?? "nothing")}. Search again and retry.`);
  }

  const played = assertOk(
    await browse(deps, "activate", { index: ref.index, level_id: level.level_id }), "play");
  if (played.played !== true) {
    throw new DaemonRefusedError(`${ref.title} did not start playing`);
  }
  return { title: row.title, subtitle: row.subtitle ?? null };
}
```

- [ ] **Step 4: Run the tests**

Run: `node --test test/library.test.js`
Expected: PASS, 8 tests.

- [ ] **Step 5: Verify each test can fail — the title check especially**

Delete the `row.title !== ref.title` clause. The mismatch test must fail, and
its message must show that `activate` was reached. Record it. Restore. Then
delete the `played !== true` check and confirm that test fails too.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: search and play walks, with the ref title guard"
```

---

## Task 6: The six tools

**Files:**
- Create: `src/server.js`
- Test: `test/server.test.js`

**Interfaces:**
- Consumes: everything above.
- Produces: `buildServer(deps) -> McpServer`, exported so a test can construct
  one without stdio. The module also runs as an entry point when executed
  directly.

- [ ] **Step 1: Write the failing test**

`test/server.test.js`:

```js
import test from "node:test";
import assert from "node:assert";
import { buildServer, TOOL_NAMES } from "../src/server.js";

test("exactly the six tools in the spec are registered", () => {
  assert.deepStrictEqual([...TOOL_NAMES].sort(), [
    "tonearm_control", "tonearm_pin", "tonearm_play",
    "tonearm_search", "tonearm_status", "tonearm_transfer",
  ]);
});

test("the server constructs without touching a socket", () => {
  const server = buildServer({ request: async () => ({ v: 1, status: "ok", zones: [] }) });
  assert.ok(server);
});

test("a mutating tool re-reads status instead of echoing its request", async () => {
  // MEASURED: commands are fire-and-forget -- the daemon returns EOF and never
  // confirms. _command_locked silently drops on an unknown verb or no zone. A
  // tool that echoed its own request would report success for a no-op, which
  // is the played:true-while-silent bug tonearm already had to fix.
  const calls = [];
  const deps = {
    request: async (payload) => {
      calls.push(payload.cmd);
      if (payload.cmd === "status") {
        return { v: 1, status: "ok", zone: { name: "chimaera", state: "playing", now_playing: null }, zones: [] };
      }
      return null;
    },
  };
  const server = buildServer(deps);
  await server._testInvoke("tonearm_control", { action: "playpause" });
  assert.deepStrictEqual(calls, ["playpause", "status"]);
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `node --test test/server.test.js`
Expected: FAIL — `Cannot find module '../src/server.js'`.

- [ ] **Step 3: Write the implementation**

`src/server.js`:

```js
#!/usr/bin/env node
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { request as socketRequest, TIMEOUTS } from "./client.js";
import { searchLibrary, playRef } from "./library.js";
import { resolveZone } from "./zones.js";

export const TOOL_NAMES = [
  "tonearm_status", "tonearm_search", "tonearm_play",
  "tonearm_control", "tonearm_transfer", "tonearm_pin",
];

const text = (s) => ({ content: [{ type: "text", text: s }] });
const fail = (s) => ({ content: [{ type: "text", text: s }], isError: true });

const readStatus = (deps) =>
  deps.request({ cmd: "status" }, { timeoutMs: TIMEOUTS.status });

// Commands are fire-and-forget; the daemon never confirms one. Always report
// from a fresh status read, never from what we asked for.
async function command(deps, payload) {
  await deps.request(payload, { timeoutMs: TIMEOUTS.command, expectReply: false });
  return readStatus(deps);
}

function describe(status) {
  if (!status || status.status !== "ok") {
    return `tonearm is not ready: ${status?.status ?? "no reply"}`;
  }
  const z = status.zone;
  if (!z) return "No zone is selected.";
  const np = z.now_playing;
  const what = np ? `${np.title}${np.artist ? ` — ${np.artist}` : ""}` : "nothing";
  return `${z.name}: ${z.state}, playing ${what}`;
}

export function buildServer(deps) {
  const server = new McpServer({ name: "tonearmd-mcp", version: "0.1.0" });
  const handlers = {};
  // Wrap at registration, not afterwards. An earlier draft of this plan had a
  // separate wrapErrors() that only wrapped the test seam -- so a thrown
  // UnknownZoneError would have reached the MCP transport in production while
  // every test saw a tidy tool error.
  const add = (name, config, fn) => {
    const guarded = async (args) => {
      try { return await fn(args); } catch (err) { return fail(err.message); }
    };
    handlers[name] = guarded;
    server.registerTool(name, config, guarded);
  };
  // Test seam: exercise a handler without a transport.
  server._testInvoke = (name, args) => handlers[name](args);

  add("tonearm_status",
    { description: "What is playing on Roon right now, and which zones exist.", inputSchema: {} },
    async () => {
      const status = await readStatus(deps);
      const zones = (status.zones ?? []).map((z) => `${z.name} (${z.state})`).join(", ");
      return text(`${describe(status)}\nZones: ${zones || "none"}`);
    });

  add("tonearm_search",
    { description: "Search the Roon library. Returns albums and tracks with refs to pass to tonearm_play.",
      inputSchema: { query: z.string().describe("What to look for, e.g. an album or track name") } },
    async ({ query }) => {
      const found = await searchLibrary(query, deps);
      if (found.length === 0) return text(`No results for ${JSON.stringify(query)}.`);
      return text(found.map((c) =>
        `[${c.kind}] ${c.title}${c.subtitle ? ` — ${c.subtitle}` : ""}\n  ref: ${c.ref}`).join("\n"));
    });

  add("tonearm_play",
    { description: "Play a search result in the zone the widget is following.",
      inputSchema: { ref: z.string().describe("A ref from tonearm_search. Pass it back unchanged.") } },
    async ({ ref }) => {
      const started = await playRef(ref, deps);
      const status = await readStatus(deps);
      return text(`Playing ${started.title}${started.subtitle ? ` — ${started.subtitle}` : ""}.\n${describe(status)}`);
    });

  add("tonearm_control",
    { description: "Transport control for the followed zone.",
      inputSchema: { action: z.enum(["playpause", "pause", "next", "previous"]) } },
    async ({ action }) => text(describe(await command(deps, { cmd: action }))));

  add("tonearm_transfer",
    { description: "Move what is playing to another zone, keeping the track and position.",
      inputSchema: { to_zone: z.string().describe("Destination zone name") } },
    async ({ to_zone }) => {
      const status = await readStatus(deps);
      const zone = resolveZone(status.zones, to_zone);
      return text(describe(await command(deps, { cmd: "transfer", arg: zone.id })));
    });

  add("tonearm_pin",
    { description: "Change which zone the bar widget follows. Pass 'unpin' to resume auto-follow.",
      inputSchema: { zone: z.string().describe("A zone name, or 'unpin'") } },
    async ({ zone }) => {
      if (String(zone).trim().toLowerCase() === "unpin") {
        return text(describe(await command(deps, { cmd: "zone", arg: "unpin" })));
      }
      const status = await readStatus(deps);
      const target = resolveZone(status.zones, zone);
      return text(describe(await command(deps, { cmd: "zone", arg: target.id })));
    });

  return server;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const server = buildServer({ request: socketRequest });
  await server.connect(new StdioServerTransport());
}
```

- [ ] **Step 4: Run the tests**

Run: `node --test test/server.test.js`
Expected: PASS, 3 tests.

- [ ] **Step 5: Run the whole suite**

Run: `npm test`
Expected: PASS — 30 tests across five files.

- [ ] **Step 6: Verify the re-read test can fail**

In `command()`, return the request payload instead of calling `readStatus`.
The "re-reads status" test must fail showing `["playpause"]` rather than
`["playpause", "status"]`. Restore.

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "feat: the six MCP tools over stdio"
```

---

## Task 7: Live verification and the README

**Files:**
- Modify: `README.md`
- No source changes. If this task needs one, it is a bug found by the task —
  fix it with a test first.

- [ ] **Step 1: Confirm the daemon is up**

```bash
systemctl --user is-active tonearmd.service
~/Src/tonearm/scripts/tonearmctl status | head -3
```

Expected: `active`, and a status line with `"status": "ok"`.

- [ ] **Step 2: Drive every tool against the real daemon**

```bash
cd ~/Src/tonearmd-mcp
node -e '
import("./src/server.js").then(async ({ buildServer }) => {
  const { request } = await import("./src/client.js");
  const s = buildServer({ request });
  const show = async (n, a) => console.log(n, "->", (await s._testInvoke(n, a)).content[0].text);
  await show("tonearm_status", {});
  await show("tonearm_search", { query: "kind of blue" });
});'
```

Expected: a real zone list, and a candidate list with `[album]` entries and
refs. Record the output.

- [ ] **Step 3: Play something, for real**

Take a `ref` from step 2 and run `tonearm_play` with it. Confirm by ear and by
`tonearmctl status` that the named album is what started, in the followed
zone. **This is the step that would catch a wrong-album bug**; do not skip it
or substitute a fixture.

- [ ] **Step 4: Verify the title guard against reality**

Search, take a ref, then search for something else so the session moves, then
play the first ref. It must re-walk and still play the right thing — the guard
only fires if Roon's own ordering changed, which is the point.

- [ ] **Step 5: Verify the failure paths**

```bash
systemctl --user stop tonearmd.service
# tonearm_status must report that tonearmd is not running, not a stack trace.
systemctl --user start tonearmd.service
# tonearm_transfer with a nonsense zone must list the real zone names.
```

- [ ] **Step 6: Write the README**

Cover: what it is; that it requires a running `tonearmd` and links to tonearm's
own install; the MCP client config block (`command: node`, `args:
["<abs path>/src/server.js"]`); the six tools in a table; that it needs no Roon
credentials of its own because `tonearmd` owns the only Roon connection; MIT,
and that the only dependencies are `@modelcontextprotocol/sdk` and `zod`.

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "docs: README, after live verification against the Core"
```
