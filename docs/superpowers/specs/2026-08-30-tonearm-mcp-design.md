# tonearm MCP server — design

An MCP server letting an LLM read tonearm's state, search the Roon library, and
put music on. Lives in its own repository, **`tonearmd-mcp`**; consumes
`tonearmd`'s unix socket and nothing else. The name is deliberate — it consumes
the *daemon*, not the plugin, which is exactly the boundary this design draws.

This document lives in the `tonearm` repo because the protocol it depends on is
documented here and because `2026-08-27-tonearm-design.md` §10a is its direct
antecedent. **It moves to the MCP repo when that repo is created.**

Facts below marked *(measured)* were read from the running daemon or its source
on 2026-08-30, not recalled. §10a's central claim has since been overturned;
see "What §10a got wrong".

## 1. Scope

**In:** read state, search the library, play a result, basic transport, move
the stream between zones, change which zone the bar follows.

**Out of v1:** `seek`, `volume`, `mute`, streaming subscription, arbitrary
browse navigation, queueing. Each is either fiddly to express in language
("turn it up a bit"), has no consumer, or adds a way to get real speakers in
real rooms wrong for no request anyone has made.

**Audience:** personal use, built so publishing is cheap later — clean
boundaries, real error handling, a LICENSE and README from day one; no CI,
packaging or marketplace work until it is actually wanted.

## 2. Constraints inherited from tonearm

- **`tonearmd` stays the only thing that talks to Roon.** Roon pairing is
  per-extension, so a second Roon client means a second approval and a second
  token. Every other consumer goes through the socket. From §10a, unchanged.
- **Separate repo.** `omarchy plugin add` clones the *whole repo* into the
  plugin directory, so an MCP server shipped inside `tonearm` would land on
  every widget user's disk. From §10a, unchanged.

### What §10a got wrong

§10a named the blocking problem as concurrency:

> The vendored `roonapi` has no `multi_session_key` … two concurrent consumers
> *will* clobber each other's navigation — you are part-way down an album list
> in the popup, the LLM runs a search, your list resets under you.

That is no longer true. `2026-08-28-tonearm-browse-design.md` §2.3 measured
`multi_session_key` working, and `core.browse_session(key)` keys sessions by
it. **The wire protocol already exposes this** *(measured:
`server.py:151`, `key = payload.pop("session", None) or "widget"`)* — any
consumer picks its own key. This server sends `"session": "mcp"`, a constant,
which also avoids `FOLLOWUPS` item 9: the session dict is unbounded, and a
consumer minting a fresh key per request would leak.

**No daemon change is required for v1.**

## 3. Architecture

The server opens `$XDG_RUNTIME_DIR/tonearm/sock` directly and speaks the
protocol — one JSON line in, one out. It does **not** shell out to
`tonearmctl`: no subprocess per call, no CLI parsing in the path. It does
borrow that client's lessons — connect and reply deadlines, and a distinct
error for "accepted the connection but never answered."

Decisions live in pure modules; I/O holds none. This mirrors `tonearm`'s own
layer map, and for the same reason: it is what makes the interesting parts
testable.

| Module | Holds |
|---|---|
| `codec.js` | ref encode/decode |
| `candidates.js` | expansion policy: a daemon search reply → a candidate list |
| `zones.js` | zone name → id resolution |
| `client.js` | socket, deadlines, error mapping |
| `server.js` | MCP tool definitions; thin by construction |

### Language: plain JavaScript, no build step

Node, `@modelcontextprotocol/sdk`, and `node --test`. Not TypeScript: this
matches `tonearm`'s existing JavaScript exactly — `Model.js` is plain, its 82
tests are `node:test` with `node:assert`, and neither repo has a build step.
What you read is what executes.

Not Python either, despite the daemon being Python, and the reasoning is worth
recording because the opposite looks obvious:

- The server does **not** reuse `tonearmctl`; §3 has it speaking the socket
  directly. It is a fresh client either way, and what carries over from
  `tonearmctl` is its *design* — connect and reply deadlines, and separating
  "not running" from "accepted but wedged" — not its code.
- It needs **no Roon libraries**. `websocket-client` and `dbus-next` belong to
  the daemon. This is a unix socket client, JSON shaping, and tool definitions.
- Node is already a hard dependency of `tonearm` (`bin/test` runs
  `node --test`). A Python server here would be the thing introducing a second
  runtime story, not the reverse.
- `net.createConnection({ path })` handles the unix socket natively.

## 4. Tool surface

| Tool | Args | Returns |
|---|---|---|
| `tonearm_status` | — | now playing, and the zone list |
| `tonearm_search` | `query` | flat candidate list; each carries an opaque `ref` and a `kind` |
| `tonearm_play` | `ref` | what actually started |
| `tonearm_control` | `action` | `playpause` \| `pause` \| `next` \| `previous` |
| `tonearm_transfer` | `to_zone` | moves the queue; track and position preserved |
| `tonearm_pin` | `zone` \| `unpin` | changes which zone the bar follows |

`tonearm_status` is named for what it returns. An earlier draft called it
`tonearm_now_playing` and returned the zone list as well — which made the name
a lie and, worse, hid the fact that rooms are addressable at all. Claude reads
tool names and descriptions to learn what is possible; a capability buried in
another tool's response payload is not discoverable.

## 5. Search expansion and the `ref` token

**A Roon search does not return albums.** *(measured, browse design §2)* It
returns a top match and then category rows — `Artists`, `Albums`, `Composers`,
`Tracks`, `Works` — each with a count. `"oingo boingo"` gave 21 albums and 44
tracks behind two of those rows. Categories and albums are **both**
`hint: "list"` and cannot be told apart without descending (§2.4). So something
must walk the tree for Claude to see anything playable.

**Expansion policy.** Descend into `Albums` and `Tracks` only, **capped at 10
each**. Those are the two categories that play; `Artists` is two levels from
anything playable, and `Composers`/`Works` are classical-specific and rarely
what "play X" means. 21 + 44 rows is a lot of context to spend on one question,
and the cap is the difference between a cheap tool and an expensive one.

Refs are emitted **only** for rows from those two expansions, so every ref
Claude holds is known-playable.

**The ref encodes a walk, not a position:** `{query, category, index, title}`,
base64'd into an opaque string. `tonearm_play(ref)` decodes it and re-runs
`search(query)` → `enter <category>` → `activate <index>`.

**Why re-walk rather than hold the level.** A Claude turn can be minutes.
Holding a browse level across that is exactly the staleness that produced real
bugs in the widget — `level_id` drift and silent wrong-album plays. A fresh
walk has no state to go stale. It costs one extra search round trip; a live
browse search measured ~0.9s *(measured)*, so a play lands around 1–2s.

**The title in the ref is the safety property.** Before activating, the server
checks the row at that index still carries the title the ref was minted with.
Roon's ordering can shift between search and play, and without this check the
failure mode is *playing the wrong album silently* — the same class of bug the
widget's `level_id` guard exists to prevent. With it, a shifted result is an
explicit error Claude can report.

## 6. Zone targeting

**v1 takes no zone argument on `play` or `control`.** Those go to the zone the
widget follows. `transfer` and `pin` do take a zone, because naming a
destination is their entire purpose — the distinction is that neither one
changes *where a play lands*, which is the thing v1 leaves alone.

This is a deliberate narrowing of an earlier draft, which added a per-call zone
to the daemon so "play Kind of Blue in the kitchen" could be one call. Three
things argued against it:

- **`transfer` already covers the need**, in two moves: play, then move. Claude
  can chain both inside one turn.
- **Chaining has an audible wart** — it plays in the current room for a beat
  before moving. The alternative, pinning the target first, has no audible wart
  but flickers the bar, writes config twice, and leaves the pin moved if it
  fails midway. Neither is good enough to hide from the user.
- **It would have coupled v1 to a `tonearm` release** while marketplace
  submission #3414 is pending. Publication is pinned to an exact commit
  (`d4a3513`); moving HEAD means the listing arrives showing
  `Update unverified` and needs a `verify-plugin` round to clear.

**v2, once #3414 resolves,** adds an optional zone id to `browse play/activate`
and an optional source to `transfer`. Both are the same small edit — an
explicit id, falling back to the arbiter — and the plumbing exists:
`BrowseSession` already takes a `zone_id_provider` callable and `_opts()`
already carries `zone_or_output_id` *(measured: `browse.py:160,193`;
`core.py:590`)*. v2 then collapses two steps into one, with the case for it
made by real use rather than speculation.

Zone **names** are resolved to ids in this server, from `status`'s `zones[]`,
case-insensitively. The daemon takes ids everywhere and continues to.

## 7. Error handling

| Condition | Behaviour |
|---|---|
| Daemon not running | Say so plainly, and that `setup.sh` installs it — not a traceback |
| Daemon accepts but never answers | Deadline, then a distinct error |
| Zero results | Empty candidate list, query echoed — not an error; Claude rephrases |
| Zone name unmatched | Return the available names, so Claude corrects in one turn |
| Ref title mismatch | Explicit failure, never a fallback play |
| Roon unreachable | The daemon reports `status: unreachable`; surface it verbatim |

Two deliberate omissions. **No retry on failure** — an LLM retrying a play
against real speakers is how a room ends up playing twice. **No caching of
search results** — the ref re-walk *is* the cache, and a stale cache
reintroduces exactly the wrong-album failure §5 exists to prevent.

## 8. Testing

Pure modules get `node --test` coverage with no fixtures beyond captured daemon
replies — stdlib only, `node:test` and `node:assert`, matching `tonearm`'s JS
suite. `client.js` gets a stub unix-socket server; `tonearm`'s `test_cli.py`
already uses that pattern in Python and it ports directly, since
`net.createServer({ path })` is the same shape.

**Fixtures are captured from the live Core, not invented** — a common search, a
one-hit search, a zero-result search, and something classical where
`Composers`/`Works` are populated. This is how `tonearm`'s browse tests work,
and the reason they caught behaviour invented fixtures would have missed.

**The break-test that matters most is the title guard.** Shift a captured reply
so the row at the ref's index carries a different title, and confirm `play`
refuses rather than plays. Every test must be verified able to fail; this one
especially.

## 9. Shipping order

1. `client.js` and the pure modules, against captured fixtures. Nothing
   user-visible; everything tested.
2. The six tools.
3. Live verification — actually asking Claude to put music on, and to move it
   to another room.
4. v2's zone argument, after #3414 resolves.

## 10. Open questions

None blocking. Two worth revisiting after real use:

- Whether 10+10 is the right expansion cap, or whether it should vary by how
  many categories came back non-empty.
- Whether `Artists` deserves listing without refs, so Claude can offer "I see
  Miles Davis; want his albums?" rather than silently dropping the category.
