# tonearm — library search and browse

**Date:** 2026-08-28
**Status:** approved, not yet implemented
**Scope:** search the Roon library from the widget, navigate into results, and play
or queue what you find.
**Amends:** `2026-08-27-tonearm-design.md` — see §0.

---

## 0. What this changes in the MVP spec

The MVP spec says, in §1: "**Not** a library browser." That line is now wrong and
must be amended when this ships. §10 ("Out of scope") excluded library browse on the
grounds that Roon's Browse service "is session-stateful and not addressable" and
"forces a navigation-stack state machine". Both statements are **true and remain
true** — this design does not dispute them. It accepts the navigation stack and puts
it in the daemon, where it is testable, rather than in QML.

§10a's analysis is superseded in one specific and important respect, recorded in §2.3
below: it claimed the single-browse-session collision could only be fixed by
serialising access or patching the vendored library. That is false. It is fixed by a
field the existing code already passes through.

What stays out of scope is unchanged and listed in §10.

---

## 1. What this is

A search field in the popup, results beneath the now-playing view, arrow keys to move
through them, Enter to play, `q` to queue. Descending into an album shows its tracks.

The user's own framing, which decided the shape: **"this isn't about discovery."** You
already know what you want to hear. This is the shortest path from that thought to it
playing, without picking up a phone or switching windows. It is not a Roon remote and
it is not trying to become one.

A second framing decided the rest: **"I have a tendency to play albums."** The design
optimises for reaching an album and pressing play.

---

## 2. Environment, verified

Everything in this section was measured against `yavin` (Roon 2.71 build 1683) on
2026-08-28 using throwaway probes, with `tonearmd` stopped so the probe reused
tonearm's own `extension_id` and token and needed no new pairing approval. Nothing
here is inferred from documentation.

### 2.1 Search is not a hierarchy — it is an item inside `Library`

This cost three probes to find and is the single most important measurement here.

Every attempt to use a top-level `search` hierarchy returned a list containing exactly
one item titled `No Results`, for every input field name tried (`input`,
`search_input`, `query`), with and without `pop_all`. It never errors. It just
returns nothing, which is indistinguishable from a genuinely empty library.

The actual path:

```
browse_browse({hierarchy: "browse", pop_all: true})     -> "Explore"
  -> item "Library"
browse_browse({hierarchy: "browse", item_key: <Library>})
  -> "Library": [Search, Artists, Albums, Tracks, Composers, Tags]
```

The `Search` item carries `input_prompt: {"prompt": "Search", "action": "Go"}`. The
query is passed **with that item's `item_key`**:

```
browse_browse({hierarchy: "browse", item_key: <Search>, input: "oingo boingo"})
```

`input_prompt.action == "Go"` is also why search submits on Enter rather than
incrementally: Roon models it as a submitted form, not a live filter.

### 2.2 Search results are grouped, with counts

For `"Oingo Boingo"`:

| Row | `hint` | `subtitle` |
|---|---|---|
| `Oingo Boingo` (top match) | `list` | `0 Albums` |
| `Artists` | `list` | `3 Results` |
| `Albums` | `list` | `21 Results` |
| `Composers` | `list` | `3 Results` |
| `Tracks` | `list` | `44 Results` |
| `Works` | `list` | `1 Result` |

**The `Albums` category is the primary target.** It lists all 21 albums directly. The
artist hop is *not* a reliable route to albums: the artist detail for all three
matched artists contained only a `Play Artist` action and reported `0 Albums`, because
those artists are streaming rather than library entries. Designing around
artist → albums would produce a feature that works on some libraries and silently
does nothing on others.

### 2.3 `multi_session_key` works — §10a's concurrency problem is solved

Measured directly. Session `sessA` descended into `Library`; session `sessB` then did
its own `pop_all` to the root. `sessA` remained at `Library`, `sessB` at `Explore`.

`browse_browse(opts)` is a pass-through — `roonapi.py:479` sends its dict verbatim via
`_request` — so `multi_session_key` reaches the Core with **no change to the vendored
library**. §10a asserted this required either serialising browse access or adding
session-key support to the vendor. Both are unnecessary.

Consequence: the widget, a future MCP server, and anything else each take their own
key and cannot disturb one another. Transport and now-playing use no browse session at
all, so browsing cannot disturb the bar.

### 2.4 Depth to a playable action is uneven

| From | Descents to actions |
|---|---|
| Track row | 1 |
| Album row | 2 (album → its tracks + `Play Album` → actions) |

The action list itself is uniform for both:

```
Play Now · Add Next · Queue · Start Radio
```

An album row is therefore *both* descendable and playable, which is why row
capabilities are two independent booleans rather than one enum.

**A category row and an album row are indistinguishable by `hint`.** Measured — the
search-results level returns:

```json
{"title":"Oingo Boingo","subtitle":"0 Albums","image_key":"fe39…","item_key":"68:0","hint":"list"}
{"title":"Artists",     "subtitle":"3 Results","image_key":null,  "item_key":"68:1","hint":"list"}
{"title":"Albums",      "subtitle":"21 Results","image_key":null, "item_key":"68:2","hint":"list"}
```

`Albums` (a category, not playable) and `Dead Man's Query` (an album, playable) both
carry `hint: "list"`. The only structural difference is that category rows have
`image_key: null` — but that is a **proxy, not a rule**: an album with no cover art
would be misclassified as unplayable, and the failure would be silent and
library-dependent.

Therefore `can_play` is **optimistic** for `hint: "list"` rows and the true answer is
only known by descending. This is what motivates the `activate` op in §5.1: the daemon
resolves the ambiguity in one round-trip rather than making the widget guess, and
`image_key` is never used to infer playability.

### 2.5 `item_key` is ephemeral, and staleness fails silently

Keys are positional strings of the form `"65:0"`, `"65:1"` — level and index. They are
invalidated by navigation. Browsing with a stale key **returns the hierarchy root with
no error**. A probe that re-walked from root and then reused previously captured keys
silently landed at `Explore` three times in a row before the cause was identified.

`pop_levels: 1` is the correct and reliable way back. Round-tripped
Tracks → Search → Artists → Search → Albums with all keys remaining valid.

### 2.6 Items carry `image_key`, and it matches the existing cache

A raw album item:

```json
{
  "title": "Dead Man's Party",
  "subtitle": "[[827514|Oingo Boingo]]",
  "image_key": "48f5b5fe1ee1dcd0f89bf0f6babcc93a",
  "item_key": "65:0",
  "hint": "list"
}
```

That `image_key` is byte-identical to the one already cached for the currently playing
track. Two consequences:

- **Subtitles carry Roon link markup** (`[[id|Text]]`) and must be stripped before
  display. This is not optional; it renders as literal brackets otherwise.
- Row art needs no daemon work at all — see §4.4.

### 2.7 An empty search returns a fake row, not an empty list

A search with no matches returns `count: 1` and a single item titled `No Results` with
no `item_key`. The daemon must normalise this to **zero rows**. Passed through
verbatim, the user could arrow onto it and attempt to play it.

### 2.8 The vendored `roonapi` has no search support

`browse_browse`, `browse_load`, `list_media` and `play_media` exist. There is no
search primitive; the only string matching in the library is a linear title scan
inside `list_media`.

`list_media` and `play_media` are additionally **unusable for this feature**. Both
take human-readable title paths and linearly scan every page for a match — O(library)
round-trips — and both open with `pop_all: True`, which resets the browse session to
root on every call. Using `play_media` to play an album would destroy the user's
browse position as a side effect.

**All browse work goes through `browse_browse`/`browse_load` directly.**

---

## 3. Decisions

| Decision | Rationale |
|---|---|
| Search-first, not a library tree | "This isn't about discovery." No full-library entry points. |
| Search is the only entry point | Keeps the state machine shallow; §10's objection was about entry breadth, not descent. |
| Descent is general, not special-cased | Roon's API is uniform; hand-coding "artist→albums" is *more* code than the general mechanism. |
| Daemon normalizes; widget stays dumb | Roon's wrapper levels and uneven depths must not become QML problems. |
| Rows addressed by index, never `item_key` | Makes §2.5's silent-reset trap structurally unreachable from the widget. |
| Play resolved lazily at invoke time | Precomputing playability costs a descent per row per page. |
| Split popup: now-playing stays visible | You queue behind what's playing; hiding it hides the information that decision needs. |
| Keyboard-first | Already supported — the popup is a `KeyboardPanel` with a `PanelKeyCatcher`. |
| Esc backs out one level | One stray Esc must not discard a whole navigation. |
| Play Now closes the popup; Queue does not | The intents differ: playing is done, queuing is usually repeated. |

---

## 4. Architecture

```
Roon Core (yavin)
      │  MOO/WS 9330            HTTP 9330 (images)
      │                                  │
 ┌────┴──────────────────────────────┐   │
 │ tonearmd                          │   │
 │   core.py     transport + zones   │   │
 │   browse.py   nav stack per key   │   │
 │   server.py   unix socket         │   │
 └────┬──────────────────────────────┘   │
      │ $XDG_RUNTIME_DIR/tonearm/sock    │
      │                                  │
 ┌────┴──────────────────────────────┐   │
 │ widget                            │   │
 │   Service.qml    relay + RPC      │   │
 │   Panel.qml      split layout     │   │
 │   BrowsePane.qml search + rows  ──┼───┘  row art loaded direct
 └───────────────────────────────────┘
```

### 4.1 One Roon client, still

Unchanged from §10a of the MVP spec and still load-bearing: `tonearmd` is the only
thing that talks to Roon, because pairing is per-extension. Browse does not change
this. It makes it more valuable — §2.3 means additional consumers are now cheap.

### 4.2 The daemon owns the cursor

`BrowseSession` holds a navigation stack per `multi_session_key`. Back is
`pop_levels: 1`, never a re-walk (§2.5). The widget holds no Roon state whatsoever.

### 4.3 Lazy play resolution

On `play`, the daemon descends from the addressed row to its action list, invokes the
named action, then pops back to where the user was. Expect ~100–300ms before audio
starts: an album is 2 descents plus the invoke.

If no action list is reachable, this is reported as an error. It is never silently
ignored.

### 4.4 Row art bypasses the daemon

Rows carry `image_key`. The widget builds `http://<core>:<http_port>/api/image/<key>`
and loads it directly — §2.1 of the MVP spec measured that a plain `Image` loads
remote Core URLs successfully (only `ColorQuantizer` cannot). `Model.js` already
exports `artUrl` for this, and the widget already receives `core.host`/`core.http_port`
in every status payload.

This is lazy for free: a delegate's `Image` loads only when scrolled into view. The
daemon's art cache remains what it always was — a workaround for `ColorQuantizer` on
the now-playing path — and is **not** extended to rows. Caching 100 images per page
would be pure waste.

---

## 5. The interface

### 5.1 Requests

One JSON object in, one JSON line out, connection closes. This matches the existing
one-shot command model; the *session* lives in the daemon keyed by `session`, so it
survives across connections.

```json
{"cmd":"browse","session":"widget","op":"search","term":"oingo boingo"}
{"cmd":"browse","session":"widget","op":"enter","index":2,"level_id":7}
{"cmd":"browse","session":"widget","op":"back"}
{"cmd":"browse","session":"widget","op":"page","offset":100}
{"cmd":"browse","session":"widget","op":"play","index":0,"action":"play_now","level_id":9}
{"cmd":"browse","session":"widget","op":"play","index":0,"action":"queue","level_id":9}
{"cmd":"browse","session":"widget","op":"reset"}
```

| `op` | Meaning |
|---|---|
| `search` | Walk to `Library → Search`, submit `term`, return the results level. Implicitly resets the session first. |
| `enter` | Descend into the row at `index`. |
| `activate` | Try `play_now` on the row at `index`; if no action list is reachable, descend into it instead. One round-trip. This is what `Enter` sends, and it exists because §2.4 proves a category and an album cannot be told apart before descending. |
| `back` | `pop_levels: 1`. At the top level this is a no-op that returns the current level unchanged. |
| `page` | Re-load the current level at a different `offset`. Does not move the cursor. |
| `play` | Resolve and invoke `action` on the row at `index` (§4.3). Returns the current level unchanged on success. |
| `reset` | Discard the navigation stack and return to an empty state. Used when the popup closes, so a stale cursor is never carried into the next session. |

`action` is one of `play_now`, `queue`, `add_next`, `start_radio`. The widget uses the
first two; the others exist because Roon offers them uniformly (§2.4) and excluding
them from the protocol would be an arbitrary narrowing.

### 5.1.1 `level_id` — why index addressing needs a generation counter

Index addressing (§3) removes the `item_key` staleness trap, but on its own it
introduces a subtler one. Suppose the widget renders the `Albums` page, and before the
user presses `Enter` the session resets — a Roon error (§7.4), or a daemon restart.
The daemon's current level is now the root. Index 2 is still a *valid* index. It just
means something entirely different, and **the wrong album plays**.

That is the worst available failure for this feature: silent, plausible, and
indistinguishable from a mis-click.

So every reply carries a `level_id`: an integer that increments on every level change
within a session. Any request that addresses a row by index — `enter` and `play` —
**must** include the `level_id` it believes it is addressing. If it does not match the
daemon's current level, the daemon performs no action and replies `stale`, including
the current level so the widget can re-render and the user can retry against what is
actually on screen.

`back`, `page`, `search` and `reset` do not address a row and so do not require it.

### 5.2 Replies

```json
{
  "v": 1,
  "ok": true,
  "level_id": 9,
  "path": ["Search", "Albums"],
  "count": 21,
  "offset": 0,
  "rows": [
    {
      "title": "Dead Man's Party",
      "subtitle": "Oingo Boingo",
      "image_key": "48f5b5fe1ee1dcd0f89bf0f6babcc93a",
      "can_descend": true,
      "can_play": true
    }
  ]
}
```

Errors:

```json
{"v": 1, "ok": false, "error": "unreachable", "message": "Roon Core unreachable"}
```

`error` is a stable machine-readable token; `message` is for humans.

| Token | Emitted when |
|---|---|
| `unreachable` | Status is not `ok`; no Roon call is attempted (§7.4). |
| `stale` | The request's `level_id` does not match the session's current level (§5.1.1). The reply also carries the current level, so the widget can re-render. |
| `bad_index` | `index` is outside the current level's loaded rows. |
| `no_action` | Play was requested but no action list was reachable from that row (§4.3). |
| `no_zone` | Play was requested with no zone selected to play into. Roon browse actions play into the zone named by `zone_or_output_id` in the browse opts; with none, invoking `Play Now` succeeds at the protocol level and plays nothing (measured). Failing loudly is mandatory — reporting `played: true` over silence is indistinguishable from working. Navigation (`search`/`enter`/`back`/`page`) is unaffected and works with no zone. |
| `roon_error` | Roon returned an error or an unusable response. The session resets to root (§7.4). |

Every browse call carries `zone_or_output_id`, read from the daemon's
followed/pinned zone at call time (the widget never names it — §3 keeps the
pinned zone the single target, and the popup already has a zone switcher). The
`roon_error` reply also carries the current level, since the session has just
reset underneath the widget and the pane must be told.

A `stale` reply is the only error that is **not** a failure from the user's point of
view: nothing went wrong, the screen was simply out of date. The pane re-renders and
the keystroke is discarded rather than replayed — replaying it against a level the
user never saw is precisely the behaviour §5.1.1 exists to prevent.

### 5.3 Protocol invariants

Within `v: 1`:

- **No reply ever contains an `item_key`,** or any other Roon-internal identifier.
  This is the invariant that makes §2.5 unreachable from the widget. It is asserted by
  a test (§9).
- `rows[i]` is addressed by its index `i` within the current level and offset, and
  only ever together with the `level_id` that produced it (§5.1.1).
- `level_id` is strictly increasing within a session and never reused. A daemon
  restart starts a fresh session, so a `level_id` from before a restart cannot
  accidentally match.
- `subtitle` is always display-ready: markup stripped (§2.6), never null (empty string
  instead).
- A search with no matches returns `rows: []` and `count: 0` (§2.7).
- `path` is a display breadcrumb only. It is never used for navigation.

---

## 6. Module decomposition

### Daemon

| File | Responsibility | Est. |
|---|---|---|
| `scripts/tonearm_lib/browse.py` | **New.** `BrowseSession`: nav stack, search/enter/back/page/play. Plus pure helpers `strip_markup()`, `row_from_item()`, `capabilities_from_hint()`, `normalize_level()`. | ~250 |
| `scripts/tonearm_lib/server.py` | Add the `browse` verb and its reply path. | +40 |
| `scripts/tonearm_lib/core.py` | Add `browse_session(key)`; expose the `RoonApi` to it. | +30 |
| `scripts/tonearmctl` | Browse subcommands, so it remains the reference client. | +50 |

The pure helpers live at module level in `browse.py` rather than in a separate file:
they are small, they are only used here, and keeping them beside `BrowseSession` makes
the file readable as one unit. They are independently testable because they take and
return plain data.

### Widget

| File | Responsibility | Est. |
|---|---|---|
| `BrowsePane.qml` | **New.** Search field, breadcrumb, row list, row delegate, key handling. | ~280 |
| `Panel.qml` | Split layout; instantiate `BrowsePane`. Must not absorb browse logic. | +60 |
| `Service.qml` | **New capability:** request/response RPC. Spawn, collect one stdout line, parse, return. | +50 |
| `Model.js` | Cursor math, row-label helpers. Reuses existing `artUrl`. | +40 |

`Panel.qml` is already 552 lines. Folding a browse pane into it would put it past 800,
which the MVP spec's own decomposition guidance argues against. `BrowsePane.qml` is
therefore a hard requirement of this design, not a stylistic preference.

---

## 7. Behavior

### 7.1 Layout

Now-playing, art, seek bar and transport stay pinned at the top, unchanged. Below
them: a separator, a breadcrumb row, then result rows. Rows scroll internally against
a fixed maximum height (~8 visible), so the popup grows once and then stops rather
than tracking result count into an unusable column.

**The bar module does not change.** No new glyph, no new state. Browsing is entirely a
popup concern.

### 7.2 Keyboard

The popup is already a `KeyboardPanel` (`Panel.qml:193`) containing a
`PanelKeyCatcher` (`Panel.qml:203`), so this extends existing machinery rather than
introducing any.

| Key | Behaviour |
|---|---|
| `/` | Focus the search field |
| Any printable key except `h j k l x X` and Space | Focus the field, **seeded** with that character |
| `Enter` (in field) | Submit the search |
| `↑` `↓` | Move the row cursor |
| `Enter` | `activate` — plays if playable, descends if not |
| `→` | `enter` — always descends |
| `q` | Queue the selected row; with no row selected, start a search instead |
| `←` | Back one level |
| `Esc` | Back one level; at the top level, close the popup |

**`h j k l x X` and Space cannot open the search field.** `PanelKeyCatcher`
consumes them before `onTextKey` is emitted at all — `h`/`j`/`k`/`l` as
`moveRequested`, `x`/`X` as `deleteRequested`, Space as `activateRequested` —
so no widget-side handler can recover them. An earlier draft of this section
and of the README promised "any letter", which was wrong for seven of them
(no Queen, Kraftwerk, Led Zeppelin, Hendrix, Haim or XTC from an idle popup).
`q` is the one that *was* recoverable: it is now resolved by context rather
than claimed unconditionally.

**Focus must be returned symmetrically.** `Ui/TextField.qml` inherits QQC2
`TextField`, i.e. it *is* the `QQuickTextInput` and it **accepts** the keys it
understands. Clearing the `blocked` flag is therefore not enough: everywhere
the pane stops editing (search submitted, `Esc` in the field, back out of the
field) it must also hand active focus back to the `PanelKeyCatcher`, or
`Enter` re-runs the search, `q` types a `q`, and `←`/`→` move the text caret.

**The seed matters.** The key that opens the field is never `accepted` by the
catcher, and the focus grab is deferred a frame, so the triggering character
reaches nothing — the field must be seeded with it explicitly or the first
letter of every query is silently dropped.

`Enter` prefers **playing** over descending, and this is the single most important
interaction choice in the design. An album row is both playable and descendable
(§2.4); if `Enter` descended, reaching an album and playing it would take an extra
keystroke and an extra screen — directly against §1's stated goal. So `Enter` on an
album plays it, and `→` is how you look inside at its tracks.

`Enter` on a category row (`Albums`, `Tracks`, `Artists`) descends, because `activate`
falls back to descending when no action list is reachable. The widget does not decide
this and must not try to: §2.4 measured that categories and albums are indistinguishable
before descending, so any client-side rule would be a guess that fails on art-less
albums.

The cost is that `Enter` on a category spends a failed play resolution before
descending. That is one extra round-trip on a keystroke the user perceives as
navigation, not playback, and it is the deliberate price of never guessing wrong in
the audible direction.

While the search field has focus, `PanelKeyCatcher.blocked` is true so the `TextField`
receives keys normally. Gate it on *"the search field is active"*, following the
network panel's precedent (`blocked: root.passwordSsid !== ""`,
`plugins/panels/network/Panel.qml:996`) rather than on `activeFocus` directly.

### 7.3 After playing

`Play Now` closes the popup. `Queue` leaves it open. Deliberately asymmetric: playing
is a completed intent, queuing is usually repeated.

### 7.4 Failure states

| Condition | Behaviour |
|---|---|
| Status is `unreachable` | `browse` replies `unreachable` immediately. Never hangs. |
| Search matches nothing | `rows: []`; the pane shows "No results" (§2.7). |
| Row has no reachable action list | `no_action` error surfaced in the pane. Never a silent no-op. |
| Any Roon error mid-navigation | Session resets to root and says so. A half-broken cursor is worse than a known-good one. |
| Daemon restarts | Sessions are in-memory and lost. The widget's next request rebuilds from root. |

### 7.5 Concurrency

`browse` must not take `Server._lock`. A browse round-trip is far slower than a
snapshot, and holding that lock would stall every subscriber — the availability hazard
already recorded as follow-up 3 in `docs/FOLLOWUPS.md`, which browsing would turn from
theoretical into routine. Each `BrowseSession` carries its own lock.

---

## 8. Dependencies and install

None added. No new Python packages, no new QML imports, no vendored-library changes.
`multi_session_key` (§2.3) rides through the existing pass-through.

---

## 9. Testing

| Layer | How |
|---|---|
| `strip_markup`, `row_from_item`, `capabilities_from_hint` | Plain unit tests. No Core. |
| `BrowseSession` | Driven against a fake Roon object: nav depth, back, paging, `No Results`, error resets, lazy play resolution. This is where the real coverage is. |
| Protocol | Round-trips through `server.py` with a fake session. |
| **The `item_key` invariant** | An explicit assertion that no reply payload contains `item_key`. §5.3's invariant erodes silently without a guard. |
| **The `stale` path** | Render a level, reset the session behind it, then replay the original `enter`/`play` with its old `level_id`. Must return `stale` and must perform no action — *not* act on the row that now occupies that index. This test guards the one silent-wrong-result failure in the design (§5.1.1), so it must be written to fail if the check is removed. |
| `Model.js` additions | node tests, ES3 subset, no mutable module state. |
| Live | Against `yavin`, as its own step. None of the above proves a real Core behaves. |

Fixtures must be built from the **recorded probe output** in this spec, not invented.
Six un-failable tests were found during the MVP because fixture values coincided with
implementation defaults; a fixture whose `hint` or `count` happens to match a default
proves nothing.

---

## 10. Out of scope

Unchanged from the MVP spec except where §0 states otherwise. Specifically still out:

- Full-library browse entry points (Artists / Albums / Genres from the root). Search
  is the only entry point.
- Queue view and queue reordering.
- Shuffle, repeat, Roon Radio thumbs.
- Playlists and internet radio as browse targets.
- Multiple Cores.
- Search history or saved searches.

---

## 11. Risks

| Risk | Mitigation |
|---|---|
| Roon changes the `Library → Search` path | It is discovered at runtime by looking for `input_prompt`, not hardcoded by title. |
| A large library makes paging slow | `count: 100` per page, and search results are already narrow. |
| Lazy play adds latency before audio | Measured expectation is ~100–300ms; if it grates, precompute for the visible page only. |
| `Panel.qml` absorbs browse logic anyway | `BrowsePane.qml` is a stated requirement (§6), reviewable as a diff. |
| Sessions leak if many keys are used | Only the widget uses one today; add an LRU cap if consumers multiply. |

---

## 12. Estimate

Comparable to the MVP's daemon work: one new daemon module, one new QML component, a
protocol extension, and a real test suite. Not a weekend.

---

## 13. Follow-on

This unblocks two things recorded in the MVP spec's §10a:

- **Marketplace submission**, which the author gated on browse existing. The two
  remaining gaps there (README removal instructions, root licence documenting
  dependencies) are unrelated to this work and already recorded.
- **The MCP server**, which §10a argued should trigger a repo split. §2.3 removes the
  concurrency objection entirely: an MCP server takes its own `multi_session_key` and
  cannot disturb the widget's cursor. The repo-split argument — that
  `omarchy plugin add` clones the whole repo, so plugin users would clone an MCP
  server they never run — is unaffected and still stands.
