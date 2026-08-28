# tonearm Library Search and Browse — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Search the Roon library from the tonearm popup, navigate into results, and play or queue what you find — keyboard-first, album-oriented.

**Architecture:** `tonearmd` gains a `BrowseSession` that owns a Roon browse cursor per `multi_session_key`, normalizes Roon's raw items into display-ready rows, and hides Roon's uneven wrapper levels behind an `activate` op. The widget holds no Roon state: it addresses rows by index paired with a `level_id` generation counter, and renders a new `BrowsePane.qml` beneath the existing now-playing view.

**Tech Stack:** Python 3.14 stdlib (`unittest`, no pytest), vendored `roonapi`, QML/Quickshell 0.3.1, Omarchy shell `qs.Ui` components, `Model.js` under node `--test`.

**Spec:** `docs/superpowers/specs/2026-08-28-tonearm-browse-design.md`

## Global Constraints

Copied verbatim from the spec. Every task's requirements implicitly include this section.

- **Never use `list_media` or `play_media`.** Both open with `pop_all: True`, which resets the browse session to root as a side effect, and both linearly scan every page for a title match. All browse work goes through `browse_browse`/`browse_load` directly. (spec §2.8)
- **Never re-walk from root to go back.** `pop_levels: 1` is the only correct back. A stale `item_key` returns the hierarchy root **with no error**. (spec §2.5)
- **No reply may ever contain an `item_key`,** or any other Roon-internal identifier. Asserted by a test. (spec §5.3)
- **Every browse call carries `multi_session_key`.** (spec §2.3)
- **`browse` must never take `Server._lock`.** Each `BrowseSession` carries its own lock. (spec §7.5)
- **Search is reached by looking for `input_prompt`, never by matching the title "Search".** (spec §11)
- **`image_key` must never be used to infer playability.** (spec §2.4)
- `Model.js` is loaded by both node and Qt's V4: ES3-subset only, no `.pragma library`, no mutable module state, `module.exports` guarded by `typeof module !== "undefined"`.
- Python runs under `/usr/bin/python` (3.14.7), stdlib `unittest` only. Tests live in `tests/python/`, run via `bin/test`.
- Fixtures must be built from the probe output recorded in spec §2, not invented. A fixture value that coincides with an implementation default proves nothing — six such tests were found during the MVP.

## Verified QML idiom

Measured against the live shell on 2026-08-28. Do not substitute anything from the Quickshell docs.

| Need | Correct form | Notes |
|---|---|---|
| Popup window | `KeyboardPanel` from `qs.Ui` | Already used at `Panel.qml:193`. Not `PopupWindow`. |
| Keyboard focus | `KeyboardPanel.focusTarget: keyCatcher` | Already wired at `Panel.qml:199`. |
| Key dispatch | `PanelKeyCatcher` from `qs.Ui` | Already present at `Panel.qml:203`. Signals: `moveRequested(int dx, int dy)`, `activateRequested()`, `returnRequested()`, `closeRequested()`, `deleteRequested()`, `tabRequested(int direction)`, `textKey(string text)`. |
| Suppress key handling while typing | `PanelKeyCatcher.blocked: <editor is active>` | Gate on state, not `activeFocus`. Precedent: `plugins/panels/network/Panel.qml:996` uses `blocked: root.passwordSsid !== ""`. |
| Text input | `TextField` from `qs.Ui` | `/usr/share/omarchy/shell/Ui/TextField.qml`, 57 lines. |
| Spacing / sizes | `Style.space(N)`, `Style.font.title` | Never hardcode pixels. |
| Colours | `Color.foreground`, `Color.muted`, `Color.background` | `Color.muted` is `#707880`. |
| Panel sizing | `panel.fittedContentWidth(...)` / `fittedContentHeight(...)` | As at `Panel.qml:200-201`. |
| Column inside panel | Anchor `left`/`right`/`top` only | Anchoring `bottom` while `contentHeight` binds back to `implicitHeight` is a binding loop. See `Panel.qml:211-215`. |
| Spawning a process | One `Process` per invocation | `Process.command` assigned mid-run is **silently ignored**. See `Service.qml:82-89`. |

---

## File Structure

| File | Responsibility |
|---|---|
| `scripts/tonearm_lib/browse.py` | **New.** Pure row helpers + `BrowseSession` (nav stack, search/enter/back/page/play/activate). |
| `scripts/tonearm_lib/core.py` | Add `browse_session(key)`; expose the `RoonApi`. |
| `scripts/tonearm_lib/server.py` | Add the `browse` verb and its reply path. |
| `scripts/tonearmctl` | Browse subcommands — the reference client. |
| `Model.js` | Extract `imageUrl(core, imageKey, px)`; add `moveCursor`, `rowSubtitle`. |
| `Service.qml` | Request/response RPC over `tonearmctl`. |
| `BrowsePane.qml` | **New.** Search field, breadcrumb, row list, key handling. |
| `Panel.qml` | Split layout; instantiate `BrowsePane`. Must not absorb browse logic. |

---

## Task 1: Pure row helpers

**Files:**
- Create: `scripts/tonearm_lib/browse.py`
- Test: `tests/python/test_browse_rows.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `strip_markup(text: str) -> str`, `capabilities_from_hint(item: dict) -> tuple[bool, bool]` returning `(can_descend, can_play)`, `row_from_item(item: dict) -> dict`, `normalize_rows(items: list) -> list`.

- [ ] **Step 1: Write the failing test**

```python
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import browse


class TestStripMarkup(unittest.TestCase):
    def test_strips_a_roon_link(self):
        # Measured from yavin: album subtitles arrive as link markup.
        self.assertEqual(
            browse.strip_markup("[[827514|Oingo Boingo]]"), "Oingo Boingo")

    def test_strips_several_links_and_keeps_the_text_between(self):
        self.assertEqual(
            browse.strip_markup("[[1|Danny Elfman]], [[2|Oingo Boingo]]"),
            "Danny Elfman, Oingo Boingo")

    def test_plain_text_is_unchanged(self):
        self.assertEqual(browse.strip_markup("3 Results"), "3 Results")

    def test_none_becomes_empty_string_not_none(self):
        # spec 5.3: subtitle is never null.
        self.assertEqual(browse.strip_markup(None), "")


class TestCapabilities(unittest.TestCase):
    def test_action_list_is_playable_and_not_descendable(self):
        # Measured: track rows in a search result carry hint action_list.
        self.assertEqual(
            browse.capabilities_from_hint({"hint": "action_list"}),
            (False, True))

    def test_list_is_both_because_an_album_is_both(self):
        # spec 2.4: an album is descendable AND playable, and a category is
        # indistinguishable from it here. can_play is optimistic.
        self.assertEqual(
            browse.capabilities_from_hint({"hint": "list"}), (True, True))

    def test_an_item_with_no_hint_is_neither(self):
        # The "No Results" sentinel arrives with hint absent.
        self.assertEqual(browse.capabilities_from_hint({}), (False, False))

    def test_image_key_never_affects_playability(self):
        # spec 2.4: category rows have image_key None and albums may too.
        # Using it as a proxy misclassifies art-less albums.
        with_art = browse.capabilities_from_hint(
            {"hint": "list", "image_key": "abc"})
        without = browse.capabilities_from_hint(
            {"hint": "list", "image_key": None})
        self.assertEqual(with_art, without)


class TestRowFromItem(unittest.TestCase):
    def setUp(self):
        # Verbatim from the probe output recorded in spec 2.6.
        self.album = {
            "title": "Dead Man's Party",
            "subtitle": "[[827514|Oingo Boingo]]",
            "image_key": "48f5b5fe1ee1dcd0f89bf0f6babcc93a",
            "item_key": "65:0",
            "hint": "list",
        }

    def test_shapes_the_row_the_widget_renders(self):
        row = browse.row_from_item(self.album)
        self.assertEqual(row["title"], "Dead Man's Party")
        self.assertEqual(row["subtitle"], "Oingo Boingo")
        self.assertEqual(row["image_key"], "48f5b5fe1ee1dcd0f89bf0f6babcc93a")
        self.assertTrue(row["can_descend"])
        self.assertTrue(row["can_play"])

    def test_never_leaks_item_key(self):
        # spec 5.3 invariant. This is the guard that makes the stale-key
        # trap unreachable from the widget.
        self.assertNotIn("item_key", browse.row_from_item(self.album))

    def test_a_missing_title_becomes_empty_string(self):
        self.assertEqual(browse.row_from_item({"hint": "list"})["title"], "")


class TestNormalizeRows(unittest.TestCase):
    def test_no_results_sentinel_becomes_an_empty_list(self):
        # spec 2.7: Roon returns count 1 and one item titled "No Results".
        # Passed through, the user could arrow onto it and try to play it.
        self.assertEqual(
            browse.normalize_rows([{"title": "No Results"}]), [])

    def test_a_real_row_named_no_results_is_kept_if_it_has_a_key(self):
        # Only the keyless sentinel is dropped. A genuine library item that
        # happens to be titled "No Results" has an item_key and survives.
        rows = browse.normalize_rows(
            [{"title": "No Results", "item_key": "9:0", "hint": "list"}])
        self.assertEqual(len(rows), 1)

    def test_ordinary_rows_pass_through_in_order(self):
        rows = browse.normalize_rows([
            {"title": "A", "item_key": "1:0", "hint": "list"},
            {"title": "B", "item_key": "1:1", "hint": "list"},
        ])
        self.assertEqual([r["title"] for r in rows], ["A", "B"])
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `/usr/bin/python -m unittest discover -s tests/python -t . -v -k Browse`
Expected: FAIL with `ModuleNotFoundError: No module named 'tonearm_lib.browse'`

- [ ] **Step 3: Write the implementation**

```python
"""Roon library browse: a navigation cursor per session, and row normalization.

The widget never sees a Roon item_key (spec 5.3). It addresses rows by index
paired with a level_id generation counter, which is what makes the stale-key
trap -- a stale key silently returns the hierarchy ROOT with no error, spec
2.5 -- unreachable from the widget rather than merely unlikely.
"""

from __future__ import annotations

import logging
import re
import threading

LOG = logging.getLogger("tonearmd.browse")

# Roon embeds links in subtitles as [[id|Display Text]]. Measured on yavin:
# album subtitles arrive as "[[827514|Oingo Boingo]]". Rendered raw, the
# brackets and the numeric id are visible to the user.
_LINK = re.compile(r"\[\[\d+\|([^\]]*)\]\]")


def strip_markup(text) -> str:
    """Display-ready subtitle. Never None -- spec 5.3."""
    if not text:
        return ""
    return _LINK.sub(r"\1", str(text))


def capabilities_from_hint(item: dict) -> tuple[bool, bool]:
    """(can_descend, can_play) for one raw Roon item.

    `can_play` is OPTIMISTIC for hint "list" and that is deliberate. Measured
    (spec 2.4): a category row ("Albums") and an album row are both hint
    "list" and cannot be told apart before descending. The only structural
    difference is that categories carry image_key None -- but an album with no
    cover art does too, so using it here would silently misclassify art-less
    albums as unplayable. The ambiguity is resolved by descending, in
    BrowseSession.activate(), never by guessing from a field.
    """
    hint = item.get("hint")
    if hint == "list":
        return (True, True)
    if hint in ("action_list", "action"):
        return (False, True)
    return (False, False)


def row_from_item(item: dict) -> dict:
    """One raw Roon item -> one display-ready row.

    Deliberately constructs a NEW dict with an explicit field list rather than
    copying and deleting: a copy-then-delete would leak any field Roon adds
    later, and the item_key invariant (spec 5.3) must not depend on us
    remembering to delete a newly-introduced identifier.
    """
    can_descend, can_play = capabilities_from_hint(item)
    return {
        "title": str(item.get("title") or ""),
        "subtitle": strip_markup(item.get("subtitle")),
        "image_key": item.get("image_key"),
        "can_descend": can_descend,
        "can_play": can_play,
    }


def normalize_rows(items) -> list:
    """Raw items -> rows, with Roon's empty-result sentinel removed.

    A search with no matches returns count 1 and a single item titled
    "No Results" with NO item_key (spec 2.7). Passed through it would render
    as a row the user can select and try to play. Keyed on the absence of
    item_key, not on the title alone, so a genuine library item that happens
    to be called "No Results" survives.
    """
    rows = []
    for item in items or []:
        if not item.get("item_key") and (item.get("title") or "") == "No Results":
            continue
        rows.append(row_from_item(item))
    return rows
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/usr/bin/python -m unittest discover -s tests/python -t . -v`
Expected: PASS, all existing tests still green.

- [ ] **Step 5: Verify each new test can actually fail**

For each of the four `TestStripMarkup` / `TestCapabilities` / `TestRowFromItem` / `TestNormalizeRows` classes, temporarily break the corresponding implementation line and confirm the test fails, then revert. Specifically:
- Make `strip_markup` return its input unchanged → `test_strips_a_roon_link` must fail.
- Make `capabilities_from_hint` return `(True, True)` for every hint → `test_an_item_with_no_hint_is_neither` must fail.
- Add `"item_key": item.get("item_key")` to `row_from_item` → `test_never_leaks_item_key` must fail.
- Drop the `item_key` condition in `normalize_rows` → `test_a_real_row_named_no_results_is_kept_if_it_has_a_key` must fail.

Record the four observed failure messages in the task report. A test that cannot fail is worth nothing, and this project has shipped six of them.

- [ ] **Step 6: Commit**

```bash
git add scripts/tonearm_lib/browse.py tests/python/test_browse_rows.py
git commit -m "feat(browse): pure row normalization helpers"
```

---

## Task 2: BrowseSession — search and level state

**Files:**
- Modify: `scripts/tonearm_lib/browse.py`
- Test: `tests/python/test_browse_session.py`
- Create: `tests/python/fakes.py`

**Interfaces:**
- Consumes: `normalize_rows`, `row_from_item` from Task 1.
- Produces: `class BrowseSession(api, key)` with `search(term) -> dict`, `current() -> dict`, and attribute `level_id: int`. Reply dicts have keys `ok`, `level_id`, `path`, `count`, `offset`, `rows`.

- [ ] **Step 1: Write the fake Roon API**

```python
"""Test doubles. FakeRoon replays the level structure measured on yavin."""


class FakeRoon:
    """Minimal stand-in for RoonApi's browse surface.

    Records every opts dict it is handed so tests can assert on
    multi_session_key, pop_levels and input -- the three things that are
    invisible in the return value but decide correctness.
    """

    def __init__(self, levels=None):
        self.calls = []
        self.load_calls = []
        # level_key -> {"list": {...}, "items": [...]}
        self.levels = levels or {}
        self.current = "root"
        self.stack = []

    def browse_browse(self, opts):
        self.calls.append(dict(opts))
        if opts.get("pop_all"):
            self.current = "root"
            self.stack = []
            return {"list": self.levels[self.current]["list"]}
        if "pop_levels" in opts:
            for _ in range(opts["pop_levels"]):
                if self.stack:
                    self.current = self.stack.pop()
            return {"list": self.levels[self.current]["list"]}
        key = opts.get("item_key")
        if key is None:
            return {"list": self.levels[self.current]["list"]}
        target = self._target_for(key, opts.get("input"))
        if target is None:
            # Roon's real behaviour for a stale key: silently return the ROOT
            # with no error (spec 2.5).
            self.current = "root"
            self.stack = []
            return {"list": self.levels["root"]["list"]}
        self.stack.append(self.current)
        self.current = target
        return {"list": self.levels[self.current]["list"]}

    def browse_load(self, opts):
        self.load_calls.append(dict(opts))
        level = self.levels[self.current]
        offset = opts.get("offset", 0)
        count = opts.get("count", 100)
        return {
            "list": level["list"],
            "items": level["items"][offset:offset + count],
        }

    def _target_for(self, item_key, text):
        for name, level in self.levels.items():
            for item in level["items"]:
                if item.get("item_key") == item_key:
                    dest = item.get("_goes_to")
                    if callable(dest):
                        return dest(text)
                    return dest
        return None


def yavin_levels():
    """The structure measured on yavin, spec 2.1/2.2/2.6."""
    return {
        "root": {
            "list": {"title": "Explore", "count": 6, "level": 0},
            "items": [
                {"title": "Library", "item_key": "1:0", "hint": "list",
                 "image_key": None, "_goes_to": "library"},
            ],
        },
        "library": {
            "list": {"title": "Library", "count": 6, "level": 1},
            "items": [
                {"title": "Search", "item_key": "2:0", "hint": "list",
                 "image_key": None,
                 "input_prompt": {"prompt": "Search", "action": "Go"},
                 "_goes_to": lambda text: (
                     "results" if text == "oingo boingo" else "empty")},
                {"title": "Artists", "item_key": "2:1", "hint": "list",
                 "image_key": None, "_goes_to": None},
            ],
        },
        "results": {
            "list": {"title": "Search", "subtitle": "Oingo Boingo",
                     "count": 3, "level": 2},
            "items": [
                {"title": "Oingo Boingo", "subtitle": "0 Albums",
                 "image_key": "fe39", "item_key": "68:0", "hint": "list",
                 "_goes_to": "artist"},
                {"title": "Albums", "subtitle": "21 Results",
                 "image_key": None, "item_key": "68:2", "hint": "list",
                 "_goes_to": "albums"},
                {"title": "Tracks", "subtitle": "44 Results",
                 "image_key": None, "item_key": "68:4", "hint": "list",
                 "_goes_to": "tracks"},
            ],
        },
        "empty": {
            "list": {"title": "Search", "count": 1, "level": 2},
            "items": [{"title": "No Results"}],
        },
        "albums": {
            "list": {"title": "Albums", "subtitle": "21 Results",
                     "count": 2, "level": 3},
            "items": [
                {"title": "Dead Man's Party",
                 "subtitle": "[[827514|Oingo Boingo]]",
                 "image_key": "48f5", "item_key": "65:0", "hint": "list",
                 "_goes_to": "album_detail"},
                {"title": "Nothing To Fear",
                 "subtitle": "[[827514|Oingo Boingo]]",
                 "image_key": "a8be", "item_key": "65:1", "hint": "list",
                 "_goes_to": None},
            ],
        },
        "album_detail": {
            "list": {"title": "Dead Man's Party", "count": 2, "level": 4},
            "items": [
                {"title": "Play Album", "item_key": "70:0",
                 "hint": "action_list", "_goes_to": "album_actions"},
                {"title": "1. Just Another Day", "item_key": "70:1",
                 "hint": "action_list", "_goes_to": "track_actions"},
            ],
        },
        "album_actions": {
            "list": {"title": "Play Album", "count": 4, "level": 5,
                     "hint": "action_list"},
            "items": [
                {"title": "Play Now", "item_key": "71:0", "hint": "action"},
                {"title": "Add Next", "item_key": "71:1", "hint": "action"},
                {"title": "Queue", "item_key": "71:2", "hint": "action"},
                {"title": "Start Radio", "item_key": "71:3", "hint": "action"},
            ],
        },
        "tracks": {
            "list": {"title": "Tracks", "count": 1, "level": 3},
            "items": [
                {"title": "Dead Man's Party",
                 "subtitle": "Danny Elfman, Oingo Boingo",
                 "image_key": "48f5", "item_key": "66:0",
                 "hint": "action_list", "_goes_to": "track_actions"},
            ],
        },
        "track_actions": {
            "list": {"title": "Dead Man's Party", "count": 4, "level": 5,
                     "hint": "action_list"},
            "items": [
                {"title": "Play Now", "item_key": "72:0", "hint": "action"},
                {"title": "Add Next", "item_key": "72:1", "hint": "action"},
                {"title": "Queue", "item_key": "72:2", "hint": "action"},
                {"title": "Start Radio", "item_key": "72:3", "hint": "action"},
            ],
        },
        "artist": {
            "list": {"title": "Oingo Boingo", "count": 1, "level": 3},
            "items": [
                {"title": "Play Artist", "item_key": "69:0",
                 "hint": "action_list", "_goes_to": "album_actions"},
            ],
        },
    }
```

- [ ] **Step 2: Write the failing test**

```python
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import browse
from fakes import FakeRoon, yavin_levels


def session():
    api = FakeRoon(yavin_levels())
    return api, browse.BrowseSession(api, "widget")


class TestSearch(unittest.TestCase):
    def test_returns_the_grouped_result_rows(self):
        api, s = session()
        reply = s.search("oingo boingo")
        self.assertTrue(reply["ok"])
        self.assertEqual([r["title"] for r in reply["rows"]],
                         ["Oingo Boingo", "Albums", "Tracks"])

    def test_reaches_search_by_input_prompt_not_by_title(self):
        # spec 11: the Library -> Search path is discovered, not hardcoded.
        levels = yavin_levels()
        levels["library"]["items"][0]["title"] = "Suche"
        api = FakeRoon(levels)
        s = browse.BrowseSession(api, "widget")
        self.assertTrue(s.search("oingo boingo")["ok"])

    def test_passes_the_term_as_input_alongside_the_item_key(self):
        # spec 2.1: the query rides with the Search item's item_key.
        api, s = session()
        s.search("oingo boingo")
        submit = [c for c in api.calls if "input" in c]
        self.assertEqual(len(submit), 1)
        self.assertEqual(submit[0]["input"], "oingo boingo")
        self.assertEqual(submit[0]["item_key"], "2:0")

    def test_every_call_carries_the_session_key(self):
        # spec 2.3: isolation depends on this being on EVERY call.
        api, s = session()
        s.search("oingo boingo")
        self.assertTrue(api.calls)
        for call in api.calls:
            self.assertEqual(call.get("multi_session_key"), "widget")
        for call in api.load_calls:
            self.assertEqual(call.get("multi_session_key"), "widget")

    def test_an_empty_search_returns_zero_rows_not_a_sentinel(self):
        # spec 2.7
        api, s = session()
        reply = s.search("nothing at all")
        self.assertEqual(reply["rows"], [])
        self.assertEqual(reply["count"], 0)

    def test_path_is_the_display_breadcrumb(self):
        api, s = session()
        self.assertEqual(s.search("oingo boingo")["path"], ["Search"])

    def test_no_reply_contains_an_item_key(self):
        # spec 5.3 invariant, asserted on a whole reply not just one row.
        api, s = session()
        self.assertNotIn("item_key", repr(s.search("oingo boingo")))


class TestLevelId(unittest.TestCase):
    def test_starts_at_zero_and_increments_on_each_level_change(self):
        api, s = session()
        self.assertEqual(s.level_id, 0)
        first = s.search("oingo boingo")["level_id"]
        self.assertEqual(first, 1)

    def test_two_searches_produce_different_level_ids(self):
        # spec 5.1.1: a reused id would let a stale index address a new level.
        api, s = session()
        a = s.search("oingo boingo")["level_id"]
        b = s.search("oingo boingo")["level_id"]
        self.assertNotEqual(a, b)
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `/usr/bin/python -m unittest discover -s tests/python -t . -v -k Search`
Expected: FAIL with `AttributeError: module 'tonearm_lib.browse' has no attribute 'BrowseSession'`

- [ ] **Step 4: Write the implementation**

Append to `scripts/tonearm_lib/browse.py`:

```python
PAGE = 100


class BrowseError(Exception):
    """Carries a stable machine token from spec 5.2."""

    def __init__(self, token: str, message: str) -> None:
        super().__init__(message)
        self.token = token
        self.message = message


class BrowseSession:
    """One Roon browse cursor, isolated by multi_session_key.

    Isolation is real and measured (spec 2.3): two session keys hold
    independent positions, so the widget and any future consumer cannot
    disturb each other. This needs no change to the vendored library --
    browse_browse passes its opts dict through verbatim.

    Holds its OWN lock. It must never take Server._lock: a browse round-trip
    is far slower than a snapshot, and holding that lock would stall every
    subscriber (spec 7.5, and docs/FOLLOWUPS.md item 3).
    """

    def __init__(self, api, key: str) -> None:
        self._api = api
        self._key = key
        self._lock = threading.RLock()
        self.level_id = 0
        self._path: list[str] = []
        self._rows: list[dict] = []
        self._keys: list[str] = []
        self._count = 0
        self._offset = 0

    # -- Roon plumbing -------------------------------------------------

    def _opts(self, **extra) -> dict:
        opts = {"hierarchy": "browse", "multi_session_key": self._key}
        opts.update(extra)
        return opts

    def _browse(self, **extra):
        result = self._api.browse_browse(self._opts(**extra))
        if result is None:
            raise BrowseError("roon_error", "Roon returned no response")
        return result

    def _load(self, offset: int = 0):
        result = self._api.browse_load(
            self._opts(offset=offset, count=PAGE))
        if result is None:
            raise BrowseError("roon_error", "Roon returned no response")
        return result

    def _adopt(self, loaded, offset: int) -> dict:
        """Record a freshly loaded level as the current one, and bump the id."""
        items = loaded.get("items") or []
        lst = loaded.get("list") or {}
        self._rows = normalize_rows(items)
        # Parallel to _rows and dropped from every reply. The widget addresses
        # rows by index; this is where the real keys stay.
        self._keys = [
            i.get("item_key") for i in items
            if i.get("item_key") or (i.get("title") or "") != "No Results"
        ]
        self._count = 0 if not self._rows else int(lst.get("count") or 0)
        self._offset = offset
        self.level_id += 1
        return self.current()

    def current(self) -> dict:
        return {
            "ok": True,
            "level_id": self.level_id,
            "path": list(self._path),
            "count": self._count,
            "offset": self._offset,
            "rows": list(self._rows),
        }

    # -- Operations ----------------------------------------------------

    def search(self, term: str) -> dict:
        """Walk root -> Library -> Search, submit `term`, adopt the results.

        The Search item is found by its `input_prompt`, never by matching the
        title (spec 11): the title is localized, the prompt is structural.
        """
        with self._lock:
            self._browse(pop_all=True)
            root = self._load()
            library = self._pick(root, lambda i: (i.get("title") or "").lower()
                                 == "library")
            if library is None:
                raise BrowseError("roon_error", "no Library in the browse root")
            self._browse(item_key=library)
            lib = self._load()
            search_key = self._pick(lib, lambda i: bool(i.get("input_prompt")))
            if search_key is None:
                raise BrowseError("roon_error", "no searchable item in Library")
            self._browse(item_key=search_key, input=term)
            self._path = ["Search"]
            return self._adopt(self._load(), 0)

    @staticmethod
    def _pick(loaded, predicate):
        for item in (loaded.get("items") or []):
            if predicate(item) and item.get("item_key"):
                return item["item_key"]
        return None
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `/usr/bin/python -m unittest discover -s tests/python -t . -v`
Expected: PASS.

- [ ] **Step 6: Verify the discriminating tests can fail**

- Change `_opts` to omit `multi_session_key` → `test_every_call_carries_the_session_key` must fail.
- Change `search` to find the Search item by `title == "Search"` → `test_reaches_search_by_input_prompt_not_by_title` must fail.
- Make `level_id` constant → `test_two_searches_produce_different_level_ids` must fail.

Revert each and record the failure messages in the report.

- [ ] **Step 7: Commit**

```bash
git add scripts/tonearm_lib/browse.py tests/python/test_browse_session.py tests/python/fakes.py
git commit -m "feat(browse): BrowseSession search and level state"
```

---

## Task 3: BrowseSession — enter, back, page, and staleness

**Files:**
- Modify: `scripts/tonearm_lib/browse.py`
- Test: `tests/python/test_browse_nav.py`

**Interfaces:**
- Consumes: `BrowseSession`, `BrowseError` from Task 2.
- Produces: `enter(index, level_id) -> dict`, `back() -> dict`, `page(offset) -> dict`, `reset() -> dict`.

- [ ] **Step 1: Write the failing test**

```python
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import browse
from fakes import FakeRoon, yavin_levels


def searched():
    api = FakeRoon(yavin_levels())
    s = browse.BrowseSession(api, "widget")
    reply = s.search("oingo boingo")
    return api, s, reply


class TestEnter(unittest.TestCase):
    def test_descends_into_the_addressed_row(self):
        api, s, reply = searched()
        out = s.enter(1, reply["level_id"])          # "Albums"
        self.assertEqual([r["title"] for r in out["rows"]],
                         ["Dead Man's Party", "Nothing To Fear"])

    def test_extends_the_breadcrumb(self):
        api, s, reply = searched()
        self.assertEqual(s.enter(1, reply["level_id"])["path"],
                         ["Search", "Albums"])

    def test_a_mismatched_level_id_is_stale_and_performs_no_action(self):
        # spec 5.1.1: THE guard against playing the wrong album. The index is
        # still valid; it just means something else now.
        api, s, reply = searched()
        before = len(api.calls)
        with self.assertRaises(browse.BrowseError) as caught:
            s.enter(1, reply["level_id"] + 99)
        self.assertEqual(caught.exception.token, "stale")
        self.assertEqual(len(api.calls), before,
                         "a stale request must not touch Roon at all")

    def test_an_out_of_range_index_is_bad_index(self):
        api, s, reply = searched()
        with self.assertRaises(browse.BrowseError) as caught:
            s.enter(99, reply["level_id"])
        self.assertEqual(caught.exception.token, "bad_index")


class TestBack(unittest.TestCase):
    def test_uses_pop_levels_and_never_re_walks_from_root(self):
        # spec 2.5: re-walking invalidates every captured key, and a stale key
        # silently returns the ROOT with no error.
        api, s, reply = searched()
        s.enter(1, reply["level_id"])
        api.calls.clear()
        s.back()
        self.assertTrue(any("pop_levels" in c for c in api.calls))
        self.assertFalse(any(c.get("pop_all") for c in api.calls))

    def test_returns_to_the_previous_level(self):
        api, s, reply = searched()
        s.enter(1, reply["level_id"])
        self.assertEqual([r["title"] for r in s.back()["rows"]],
                         ["Oingo Boingo", "Albums", "Tracks"])

    def test_shortens_the_breadcrumb(self):
        api, s, reply = searched()
        s.enter(1, reply["level_id"])
        self.assertEqual(s.back()["path"], ["Search"])

    def test_back_at_the_top_is_a_no_op_that_still_returns_the_level(self):
        api, s, reply = searched()
        out = s.back()
        self.assertTrue(out["ok"])
        self.assertEqual(out["path"], ["Search"])


class TestPage(unittest.TestCase):
    def test_loads_at_the_requested_offset(self):
        api, s, reply = searched()
        s.enter(1, reply["level_id"])
        api.load_calls.clear()
        s.page(100)
        self.assertEqual(api.load_calls[-1]["offset"], 100)

    def test_reports_the_new_offset(self):
        api, s, reply = searched()
        s.enter(1, reply["level_id"])
        self.assertEqual(s.page(100)["offset"], 100)


class TestReset(unittest.TestCase):
    def test_clears_the_stack_and_the_rows(self):
        api, s, reply = searched()
        s.enter(1, reply["level_id"])
        out = s.reset()
        self.assertEqual(out["rows"], [])
        self.assertEqual(out["path"], [])
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `/usr/bin/python -m unittest discover -s tests/python -t . -v -k Enter`
Expected: FAIL with `AttributeError: 'BrowseSession' object has no attribute 'enter'`

- [ ] **Step 3: Write the implementation**

Add to `BrowseSession`:

```python
    def _check(self, index: int, level_id) -> str:
        """Validate an index/level_id pair and return the real item_key.

        The level_id check comes FIRST and short-circuits before any Roon
        call. spec 5.1.1: if the session moved between the widget rendering a
        page and the user pressing a key, the index is still *valid* -- it just
        addresses a different row. Acting on it would play the wrong album,
        silently, in a way indistinguishable from a mis-click. That is the one
        failure this design must not have.
        """
        if level_id is not None and int(level_id) != self.level_id:
            raise BrowseError(
                "stale", "the view is out of date; it has been refreshed")
        if not isinstance(index, int) or index < 0 or index >= len(self._keys):
            raise BrowseError("bad_index", "no such row")
        key = self._keys[index]
        if not key:
            raise BrowseError("bad_index", "that row cannot be opened")
        return key

    def enter(self, index: int, level_id=None) -> dict:
        with self._lock:
            key = self._check(index, level_id)
            title = self._rows[index]["title"]
            self._browse(item_key=key)
            self._path = self._path + [title]
            return self._adopt(self._load(), 0)

    def back(self) -> dict:
        """One level up, via pop_levels -- never a re-walk (spec 2.5)."""
        with self._lock:
            if len(self._path) <= 1:
                return self.current()
            self._browse(pop_levels=1)
            self._path = self._path[:-1]
            return self._adopt(self._load(), 0)

    def page(self, offset: int) -> dict:
        with self._lock:
            return self._adopt(self._load(int(offset)), int(offset))

    def reset(self) -> dict:
        with self._lock:
            self._path = []
            self._rows = []
            self._keys = []
            self._count = 0
            self._offset = 0
            self.level_id += 1
            return self.current()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/usr/bin/python -m unittest discover -s tests/python -t . -v`
Expected: PASS.

- [ ] **Step 5: Verify the staleness guard can fail**

Delete the `level_id` comparison in `_check` and run `test_a_mismatched_level_id_is_stale_and_performs_no_action`. It must fail on the `token == "stale"` assertion. Revert, and record the message. This is the single most important guard in the feature.

Then change `back()` to use `pop_all=True` followed by a re-walk and confirm `test_uses_pop_levels_and_never_re_walks_from_root` fails. Revert.

- [ ] **Step 6: Commit**

```bash
git add scripts/tonearm_lib/browse.py tests/python/test_browse_nav.py
git commit -m "feat(browse): enter, back, page, reset with staleness guard"
```

---

## Task 4: BrowseSession — play and activate

**Files:**
- Modify: `scripts/tonearm_lib/browse.py`
- Test: `tests/python/test_browse_play.py`

**Interfaces:**
- Consumes: everything from Tasks 2–3.
- Produces: `play(index, action, level_id) -> dict`, `activate(index, level_id) -> dict`. `action` is one of `play_now`, `queue`, `add_next`, `start_radio`.

- [ ] **Step 1: Write the failing test**

```python
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import browse
from fakes import FakeRoon, yavin_levels


def at_albums():
    api = FakeRoon(yavin_levels())
    s = browse.BrowseSession(api, "widget")
    reply = s.search("oingo boingo")
    reply = s.enter(1, reply["level_id"])      # Albums
    return api, s, reply


def at_tracks():
    api = FakeRoon(yavin_levels())
    s = browse.BrowseSession(api, "widget")
    reply = s.search("oingo boingo")
    reply = s.enter(2, reply["level_id"])      # Tracks
    return api, s, reply


def invoked_titles(api, levels):
    """Titles of every item_key the session browsed into."""
    seen = []
    for call in api.calls:
        key = call.get("item_key")
        if not key:
            continue
        for level in levels.values():
            for item in level["items"]:
                if item.get("item_key") == key:
                    seen.append(item["title"])
    return seen


class TestPlayAlbum(unittest.TestCase):
    def test_walks_two_levels_and_invokes_play_now(self):
        # spec 2.4: an album is 2 descents from its row to the action list.
        api, s, reply = at_albums()
        api.calls.clear()
        s.play(0, "play_now", reply["level_id"])
        titles = invoked_titles(api, yavin_levels())
        self.assertIn("Play Album", titles)
        self.assertIn("Play Now", titles)

    def test_queue_invokes_queue_not_play_now(self):
        api, s, reply = at_albums()
        api.calls.clear()
        s.play(0, "queue", reply["level_id"])
        titles = invoked_titles(api, yavin_levels())
        self.assertIn("Queue", titles)
        self.assertNotIn("Play Now", titles)

    def test_returns_to_the_level_the_user_was_on(self):
        # The user must not be teleported into the action list.
        api, s, reply = at_albums()
        out = s.play(0, "play_now", reply["level_id"])
        self.assertEqual(out["path"], ["Search", "Albums"])
        self.assertEqual([r["title"] for r in out["rows"]],
                         ["Dead Man's Party", "Nothing To Fear"])

    def test_a_stale_level_id_plays_nothing(self):
        api, s, reply = at_albums()
        api.calls.clear()
        with self.assertRaises(browse.BrowseError) as caught:
            s.play(0, "play_now", reply["level_id"] + 99)
        self.assertEqual(caught.exception.token, "stale")
        self.assertEqual(api.calls, [])

    def test_an_unknown_action_is_rejected_before_touching_roon(self):
        api, s, reply = at_albums()
        api.calls.clear()
        with self.assertRaises(browse.BrowseError):
            s.play(0, "delete_everything", reply["level_id"])
        self.assertEqual(api.calls, [])


class TestPlayTrack(unittest.TestCase):
    def test_walks_one_level_for_a_track(self):
        # spec 2.4: a track is 1 descent, an album is 2. Same code path.
        api, s, reply = at_tracks()
        api.calls.clear()
        s.play(0, "play_now", reply["level_id"])
        self.assertIn("Play Now", invoked_titles(api, yavin_levels()))


class TestNoAction(unittest.TestCase):
    def test_a_row_with_no_reachable_action_reports_no_action(self):
        api, s, reply = at_albums()
        # "Nothing To Fear" has _goes_to None -- a dead end.
        with self.assertRaises(browse.BrowseError) as caught:
            s.play(1, "play_now", reply["level_id"])
        self.assertEqual(caught.exception.token, "no_action")

    def test_a_failed_play_leaves_the_user_where_they_were(self):
        api, s, reply = at_albums()
        try:
            s.play(1, "play_now", reply["level_id"])
        except browse.BrowseError:
            pass
        self.assertEqual(s.current()["path"], ["Search", "Albums"])


class TestActivate(unittest.TestCase):
    def test_plays_an_album(self):
        api, s, reply = at_albums()
        api.calls.clear()
        s.activate(0, reply["level_id"])
        self.assertIn("Play Now", invoked_titles(api, yavin_levels()))

    def test_reports_played_true_when_it_played(self):
        # The widget closes the popup on a play but not on a descend
        # (spec 7.3), and activate does both. Without this flag, Enter on a
        # category descends and instantly closes the popup.
        api, s, reply = at_albums()
        self.assertIs(s.activate(0, reply["level_id"])["played"], True)

    def test_descends_into_a_category_when_play_is_impossible(self):
        # spec 2.4: "Albums" and an album row are indistinguishable by hint,
        # so activate resolves it by trying, not by guessing.
        api = FakeRoon(yavin_levels())
        s = browse.BrowseSession(api, "widget")
        reply = s.search("oingo boingo")
        out = s.activate(1, reply["level_id"])       # "Albums" category
        self.assertEqual(out["path"], ["Search", "Albums"])
        self.assertEqual([r["title"] for r in out["rows"]],
                         ["Dead Man's Party", "Nothing To Fear"])

    def test_reports_played_false_when_it_descended(self):
        api = FakeRoon(yavin_levels())
        s = browse.BrowseSession(api, "widget")
        reply = s.search("oingo boingo")
        self.assertIs(s.activate(1, reply["level_id"])["played"], False)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `/usr/bin/python -m unittest discover -s tests/python -t . -v -k Play`
Expected: FAIL with `AttributeError: 'BrowseSession' object has no attribute 'play'`

- [ ] **Step 3: Write the implementation**

Add to `BrowseSession`:

```python
# Roon's action titles, measured on yavin (spec 2.4). Identical for albums
# and tracks.
ACTIONS = {
    "play_now": "Play Now",
    "add_next": "Add Next",
    "queue": "Queue",
    "start_radio": "Start Radio",
}

# How deep to hunt for an action list before giving up. Measured: a track is
# 1 descent from its row, an album is 2 (album -> "Play Album" -> actions).
# 3 leaves headroom without letting a pathological hierarchy walk forever.
MAX_ACTION_DEPTH = 3
```

```python
    def play(self, index: int, action: str, level_id=None) -> dict:
        """Resolve the row's action list, invoke `action`, return to the level.

        Resolution is LAZY (spec 4.3): precomputing which rows are playable
        would cost a descent per row per page. The cost is 2-3 extra
        round-trips before audio starts, ~100-300ms.
        """
        title = ACTIONS.get(action)
        if title is None:
            raise BrowseError("bad_index", "unknown action %r" % (action,))
        with self._lock:
            key = self._check(index, level_id)
            # Bound BEFORE the try: if the first _browse raises, the finally
            # clause and the check below both still need these names.
            depth = 0
            descents = []
            try:
                self._browse(item_key=key)
                depth = 1
                descents = self._descend_to_action(title)
                depth += len(descents)
            finally:
                # Unwind to exactly where the user was, whatever happened.
                # Leaving them inside an action list -- or worse, at the root
                # after a silent stale-key reset -- would be a visible bug.
                self._unwind(depth)
            if not descents:
                raise BrowseError(
                    "no_action", "nothing here can be played")
            reply = self.current()
            # The widget closes the popup on a play but NOT on a descend
            # (spec 7.3). `activate` can do either, and the reply is a level
            # in both cases, so it must say which happened -- otherwise Enter
            # on a category descends and instantly closes the popup.
            reply["played"] = True
            return reply

    def activate(self, index: int, level_id=None) -> dict:
        """Play if possible, descend if not -- one round-trip for the widget.

        spec 2.4 measured that a category row and an album row are both
        hint "list" and cannot be told apart without descending. Rather than
        make the widget guess from image_key (which misclassifies art-less
        albums), the ambiguity is resolved here.
        """
        with self._lock:
            self._check(index, level_id)
            try:
                return self.play(index, "play_now", level_id)
            except BrowseError as exc:
                if exc.token != "no_action":
                    raise
                reply = self.enter(index, level_id)
                reply["played"] = False
                return reply

    def _descend_to_action(self, title: str) -> list:
        """Walk down looking for an action of exactly `title`.

        Returns the list of descents performed, so the caller can pop exactly
        that many. Returning a count rather than a bool is what makes the
        unwind exact: comparing list titles to decide when to stop would break
        on any hierarchy where a child level shares its parent's title, and
        Roon does exactly that -- an album's detail level is titled after the
        album (measured, spec 2.4).
        """
        descents = []
        for _ in range(MAX_ACTION_DEPTH):
            loaded = self._load()
            items = loaded.get("items") or []
            for item in items:
                if item.get("title") == title and item.get("hint") == "action":
                    self._browse(item_key=item["item_key"])
                    descents.append(item["item_key"])
                    return descents
            nxt = None
            for item in items:
                if item.get("hint") in ("action_list", "list") and item.get("item_key"):
                    nxt = item["item_key"]
                    break
            if nxt is None:
                return []
            self._browse(item_key=nxt)
            descents.append(nxt)
        return []

    def _unwind(self, depth: int) -> None:
        """Pop exactly `depth` levels, back to where the user was.

        pop_levels, never a re-walk (spec 2.5). Guarded so a failure here
        cannot mask the BrowseError the caller is already raising.
        """
        if depth <= 0:
            return
        try:
            self._browse(pop_levels=depth)
            self._load()
        except BrowseError:
            LOG.warning("could not unwind %d level(s) after a play", depth)
```

`play`'s body above already tracks the depth it reached and passes it to
`_unwind`; there is nothing further to add here.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/usr/bin/python -m unittest discover -s tests/python -t . -v`
Expected: PASS.

- [ ] **Step 5: Verify the guards can fail**

- Remove the `ACTIONS.get` guard → `test_an_unknown_action_is_rejected_before_touching_roon` must fail.
- Make `activate` always call `enter` → `test_plays_an_album` must fail.
- Make `activate` re-raise every `BrowseError` → `test_descends_into_a_category_when_play_is_impossible` must fail.

Revert each; record the messages.

- [ ] **Step 6: Commit**

```bash
git add scripts/tonearm_lib/browse.py tests/python/test_browse_play.py
git commit -m "feat(browse): lazy play resolution and activate"
```

---

## Task 5: Wire BrowseSession into RoonSession

**Files:**
- Modify: `scripts/tonearm_lib/core.py`
- Test: `tests/python/test_core_browse.py`

**Interfaces:**
- Consumes: `browse.BrowseSession`, `browse.BrowseError`.
- Produces: `RoonSession.browse_session(key: str) -> BrowseSession` and `RoonSession.browse(key, op, **kwargs) -> dict`.

- [ ] **Step 1: Write the failing test**

```python
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import browse, core
from fakes import FakeRoon, yavin_levels


class FakeSession(core.RoonSession):
    def __init__(self, api, status="ok"):
        super().__init__(lambda *a: None)
        self._api = api
        self._status = status


class TestBrowseSessionAccessor(unittest.TestCase):
    def test_returns_the_same_session_for_the_same_key(self):
        s = FakeSession(FakeRoon(yavin_levels()))
        self.assertIs(s.browse_session("widget"), s.browse_session("widget"))

    def test_different_keys_get_different_sessions(self):
        # spec 2.3: this is what keeps a future MCP server from disturbing
        # the widget's cursor.
        s = FakeSession(FakeRoon(yavin_levels()))
        self.assertIsNot(s.browse_session("widget"), s.browse_session("mcp"))

    def test_the_session_carries_its_key_to_roon(self):
        api = FakeRoon(yavin_levels())
        s = FakeSession(api)
        s.browse_session("mcp").search("oingo boingo")
        self.assertTrue(all(c.get("multi_session_key") == "mcp"
                            for c in api.calls))


class TestUnreachable(unittest.TestCase):
    def test_browse_is_refused_when_the_core_is_unreachable(self):
        # spec 7.4: reply immediately, never hang.
        api = FakeRoon(yavin_levels())
        s = FakeSession(api, status="unreachable")
        with self.assertRaises(browse.BrowseError) as caught:
            s.browse("widget", "search", term="oingo boingo")
        self.assertEqual(caught.exception.token, "unreachable")

    def test_no_roon_call_is_attempted_when_unreachable(self):
        api = FakeRoon(yavin_levels())
        s = FakeSession(api, status="unreachable")
        try:
            s.browse("widget", "search", term="oingo boingo")
        except browse.BrowseError:
            pass
        self.assertEqual(api.calls, [])


class TestDispatch(unittest.TestCase):
    def test_search_routes_through(self):
        s = FakeSession(FakeRoon(yavin_levels()))
        reply = s.browse("widget", "search", term="oingo boingo")
        self.assertEqual([r["title"] for r in reply["rows"]],
                         ["Oingo Boingo", "Albums", "Tracks"])

    def test_an_unknown_op_is_rejected(self):
        s = FakeSession(FakeRoon(yavin_levels()))
        with self.assertRaises(browse.BrowseError):
            s.browse("widget", "teleport")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `/usr/bin/python -m unittest discover -s tests/python -t . -v -k Browse`
Expected: FAIL with `AttributeError: 'FakeSession' object has no attribute 'browse_session'`

- [ ] **Step 3: Write the implementation**

Add to `core.py` — import `browse` at the top alongside the existing `from . import config, sood, state, zones`, then add to `RoonSession`:

```python
    def browse_session(self, key: str):
        """One BrowseSession per multi_session_key, created on first use.

        Sessions are in-memory and lost on restart (spec 7.4); the widget's
        next request rebuilds from root. Not bounded today because only the
        widget uses one -- add an LRU cap if consumers multiply (spec 11).
        """
        with self._browse_lock:
            existing = self._browse_sessions.get(key)
            if existing is None:
                existing = browse.BrowseSession(self._api, key)
                self._browse_sessions[key] = existing
            return existing

    def browse(self, key: str, op: str, **kwargs) -> dict:
        """Dispatch one browse op. Never takes Server._lock (spec 7.5)."""
        if self._status != "ok":
            raise browse.BrowseError("unreachable", "Roon Core unreachable")
        session = self.browse_session(key)
        if op == "search":
            return session.search(kwargs.get("term") or "")
        if op == "enter":
            return session.enter(kwargs.get("index"), kwargs.get("level_id"))
        if op == "activate":
            return session.activate(kwargs.get("index"), kwargs.get("level_id"))
        if op == "play":
            return session.play(kwargs.get("index"),
                                kwargs.get("action") or "play_now",
                                kwargs.get("level_id"))
        if op == "back":
            return session.back()
        if op == "page":
            return session.page(kwargs.get("offset") or 0)
        if op == "reset":
            return session.reset()
        raise browse.BrowseError("bad_index", "unknown browse op %r" % (op,))
```

And in `RoonSession.__init__`, alongside the existing attributes:

```python
        # Separate from every other lock in this class. A browse round-trip is
        # far slower than a snapshot; sharing a lock with the publish path
        # would stall subscribers (spec 7.5).
        self._browse_lock = threading.Lock()
        self._browse_sessions: dict = {}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/usr/bin/python -m unittest discover -s tests/python -t . -v`
Expected: PASS.

- [ ] **Step 5: Verify the unreachable guard can fail**

Remove the `self._status != "ok"` check and confirm `test_no_roon_call_is_attempted_when_unreachable` fails. Revert; record the message.

- [ ] **Step 6: Commit**

```bash
git add scripts/tonearm_lib/core.py tests/python/test_core_browse.py
git commit -m "feat(browse): per-key browse sessions on RoonSession"
```

---

## Task 6: The `browse` socket verb

**Files:**
- Modify: `scripts/tonearm_lib/server.py:70-129`
- Test: `tests/python/test_server_browse.py`

**Interfaces:**
- Consumes: `RoonSession.browse(key, op, **kwargs)`, `browse.BrowseError`.
- Produces: the wire protocol of spec §5. Request `{"cmd":"browse","session":...,"op":...}`; reply one JSON line.

- [ ] **Step 1: Write the failing test**

```python
import json
import os
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import browse, server


class StubSession:
    def __init__(self, reply=None, error=None):
        self.reply = reply or {"ok": True, "level_id": 3, "path": ["Search"],
                               "count": 0, "offset": 0, "rows": []}
        self.error = error
        self.seen = []

    def snapshot(self):
        return {"v": 1, "status": "ok"}

    def browse(self, key, op, **kwargs):
        self.seen.append((key, op, kwargs))
        if self.error:
            raise self.error
        return self.reply

    def command(self, verb, arg=None):
        pass


def roundtrip(session, request):
    """Serve exactly one connection and return the parsed reply."""
    srv = server.Server(session)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        listener.listen(1)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(path)
        conn, _ = listener.accept()
        client.sendall((json.dumps(request) + "\n").encode())
        thread = threading.Thread(target=srv._handle, args=(conn,))
        thread.start()
        line = client.makefile("r").readline()
        thread.join(timeout=5)
        client.close()
        listener.close()
    return json.loads(line) if line else None


class TestBrowseVerb(unittest.TestCase):
    def test_replies_with_the_level(self):
        s = StubSession()
        reply = roundtrip(s, {"cmd": "browse", "session": "widget",
                              "op": "search", "term": "oingo boingo"})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["level_id"], 3)
        self.assertEqual(reply["v"], 1)

    def test_forwards_the_op_and_arguments(self):
        s = StubSession()
        roundtrip(s, {"cmd": "browse", "session": "widget", "op": "enter",
                      "index": 2, "level_id": 7})
        key, op, kwargs = s.seen[0]
        self.assertEqual(key, "widget")
        self.assertEqual(op, "enter")
        self.assertEqual(kwargs["index"], 2)
        self.assertEqual(kwargs["level_id"], 7)

    def test_a_missing_session_defaults_to_widget(self):
        s = StubSession()
        roundtrip(s, {"cmd": "browse", "op": "back"})
        self.assertEqual(s.seen[0][0], "widget")

    def test_a_browse_error_becomes_a_token_reply(self):
        s = StubSession(error=browse.BrowseError("stale", "out of date"))
        reply = roundtrip(s, {"cmd": "browse", "op": "enter", "index": 0})
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "stale")
        self.assertEqual(reply["message"], "out of date")

    def test_an_unexpected_exception_becomes_roon_error_not_a_hang(self):
        s = StubSession(error=RuntimeError("boom"))
        reply = roundtrip(s, {"cmd": "browse", "op": "back"})
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "roon_error")

    def test_no_reply_ever_contains_an_item_key(self):
        # spec 5.3 invariant, asserted at the wire.
        s = StubSession(reply={
            "ok": True, "level_id": 1, "path": [], "count": 1, "offset": 0,
            "rows": [{"title": "X", "subtitle": "", "image_key": None,
                      "can_descend": True, "can_play": True}]})
        reply = roundtrip(s, {"cmd": "browse", "op": "back"})
        self.assertNotIn("item_key", json.dumps(reply))


class TestExistingVerbsStillWork(unittest.TestCase):
    def test_status_is_unaffected(self):
        s = StubSession()
        reply = roundtrip(s, {"cmd": "status"})
        self.assertEqual(reply["status"], "ok")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `/usr/bin/python -m unittest discover -s tests/python -t . -v -k BrowseVerb`
Expected: FAIL — no reply is sent, so `json.loads` raises on an empty line.

- [ ] **Step 3: Write the implementation**

In `server.py`, insert **before** the final `self._session.command(...)` fallthrough at line 125:

```python
        if cmd == "browse":
            # Deliberately NOT under self._lock. A browse round-trip is far
            # slower than a snapshot, and holding the broadcast lock here
            # would stall every subscriber for its duration (spec 7.5, and
            # docs/FOLLOWUPS.md item 3). Nothing here touches the subscriber
            # list, so there is nothing for that lock to protect.
            payload = dict(request)
            payload.pop("cmd", None)
            key = payload.pop("session", None) or "widget"
            op = payload.pop("op", None) or ""
            try:
                reply = self._session.browse(key, op, **payload)
                reply = dict(reply)
                reply["v"] = 1
            except browse.BrowseError as exc:
                reply = {"v": 1, "ok": False, "error": exc.token,
                         "message": exc.message}
            except Exception:
                # A browse failure must never take the daemon down or leave
                # the widget waiting on a line that never arrives.
                LOG.exception("browse %r failed", op)
                reply = {"v": 1, "ok": False, "error": "roon_error",
                         "message": "browse failed"}
            try:
                conn.sendall((json.dumps(reply) + "\n").encode())
            except OSError:
                pass
            conn.close()
            return
```

Add `from . import browse` to `server.py`'s imports.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./bin/test`
Expected: all suites green.

- [ ] **Step 5: Verify the invariant test can fail**

Change `StubSession`'s reply in `test_no_reply_ever_contains_an_item_key` to include `"item_key": "65:0"` in the row and confirm the test fails. Revert. This test guards spec §5.3 at the wire, and it must be able to see a violation.

- [ ] **Step 6: Commit**

```bash
git add scripts/tonearm_lib/server.py tests/python/test_server_browse.py
git commit -m "feat(browse): browse verb on the socket, outside Server._lock"
```

---

## Task 7: `tonearmctl` browse subcommands

**Files:**
- Modify: `scripts/tonearmctl`, `scripts/tonearm_lib/cli.py`
- Test: `tests/python/test_cli_browse.py`

**Interfaces:**
- Consumes: the wire protocol from Task 6.
- Produces: `tonearmctl browse search <term>`, `browse enter <index> <level_id>`, `browse activate <index> <level_id>`, `browse play <index> <action> <level_id>`, `browse back`, `browse page <offset>`, `browse reset`. Each prints one JSON line to stdout.

- [ ] **Step 1: Write the failing test**

```python
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

from tonearm_lib import cli


class TestBrowseArgv(unittest.TestCase):
    def test_search_builds_the_request(self):
        self.assertEqual(
            cli.browse_request(["browse", "search", "oingo boingo"]),
            {"cmd": "browse", "session": "widget", "op": "search",
             "term": "oingo boingo"})

    def test_search_joins_multiple_words(self):
        # `tonearmctl browse search oingo boingo` without quotes must work.
        self.assertEqual(
            cli.browse_request(["browse", "search", "oingo", "boingo"])["term"],
            "oingo boingo")

    def test_enter_carries_index_and_level_id_as_integers(self):
        request = cli.browse_request(["browse", "enter", "2", "7"])
        self.assertEqual(request["index"], 2)
        self.assertEqual(request["level_id"], 7)

    def test_play_carries_the_action(self):
        request = cli.browse_request(["browse", "play", "0", "queue", "9"])
        self.assertEqual(request["op"], "play")
        self.assertEqual(request["action"], "queue")
        self.assertEqual(request["index"], 0)
        self.assertEqual(request["level_id"], 9)

    def test_back_needs_no_arguments(self):
        self.assertEqual(cli.browse_request(["browse", "back"])["op"], "back")

    def test_an_unknown_subcommand_returns_none(self):
        self.assertIsNone(cli.browse_request(["browse", "teleport"]))

    def test_a_non_numeric_index_returns_none_rather_than_raising(self):
        self.assertIsNone(cli.browse_request(["browse", "enter", "x", "7"]))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `/usr/bin/python -m unittest discover -s tests/python -t . -v -k BrowseArgv`
Expected: FAIL with `AttributeError: module 'tonearm_lib.cli' has no attribute 'browse_request'`

- [ ] **Step 3: Write the implementation**

Add to `cli.py`:

```python
def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def browse_request(argv):
    """argv -> a browse request dict, or None if it does not parse.

    Returns None rather than raising: tonearmctl is the reference client and
    a usage error should print usage, not a traceback.
    """
    if len(argv) < 2 or argv[0] != "browse":
        return None
    op = argv[1]
    base = {"cmd": "browse", "session": "widget", "op": op}
    rest = argv[2:]
    if op == "search":
        # Joined, so an unquoted multi-word term works from a shell.
        return dict(base, term=" ".join(rest))
    if op in ("enter", "activate"):
        if len(rest) < 2:
            return None
        index, level = _int(rest[0]), _int(rest[1])
        if index is None or level is None:
            return None
        return dict(base, index=index, level_id=level)
    if op == "play":
        if len(rest) < 3:
            return None
        index, level = _int(rest[0]), _int(rest[2])
        if index is None or level is None:
            return None
        return dict(base, index=index, action=rest[1], level_id=level)
    if op == "page":
        offset = _int(rest[0]) if rest else None
        if offset is None:
            return None
        return dict(base, offset=offset)
    if op in ("back", "reset"):
        return base
    return None
```

`cli.py` already exposes a single entry point, `to_request(argv)`, which raises
`ValueError` on anything it cannot parse (`cli.py:35`). Keep that contract: add a
`browse` branch to `to_request` that delegates to `browse_request` and converts a
`None` into the same kind of error, so `tonearmctl` keeps exactly one parse path
and one failure mode.

```python
    if verb == "browse":
        request = browse_request(argv)
        if request is None:
            raise ValueError(
                "usage: tonearmctl browse search <term> | enter <i> <level_id>"
                " | activate <i> <level_id> | play <i> <action> <level_id>"
                " | back | page <offset> | reset")
        return request
```

Place it before the final `raise ValueError("unknown verb %r" % verb)`.

In `scripts/tonearmctl`, `browse` needs a **reply**, unlike every existing
command verb — the current client sends and exits. Read one line back from the
socket and print it verbatim to stdout. Exit 0 when `ok` is true, 1 otherwise.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./bin/test`
Expected: green.

- [ ] **Step 5: Verify against the running daemon**

With `tonearmd` running and paired:

```bash
scripts/tonearmctl browse search oingo boingo | jq '.rows[].title'
```

Expected: the grouped categories from spec §2.2. Record the actual output in the report — this is the first end-to-end proof the protocol works against a real Core.

- [ ] **Step 6: Commit**

```bash
git add scripts/tonearmctl scripts/tonearm_lib/cli.py tests/python/test_cli_browse.py
git commit -m "feat(browse): tonearmctl browse subcommands"
```

---

## Task 8: `Model.js` — image URLs and cursor math

**Files:**
- Modify: `Model.js:433-441`
- Test: `tests/model-browse.test.js`

**Interfaces:**
- Consumes: nothing.
- Produces: `imageUrl(core, imageKey, px)`, `rowArtUrl(state, row, px)`, `moveCursor(current, delta, count)`. `artUrl(state, px)` keeps its existing signature and behaviour.

- [ ] **Step 1: Write the failing test**

```javascript
const test = require("node:test")
const assert = require("node:assert")
const M = require("../Model.js")

const CORE = { host: "192.168.50.118", http_port: 9330, name: "yavin" }
const STATE = { v: 1, status: "ok", core: CORE, zone: null, zones: [] }
const ROW = {
  title: "Dead Man's Party", subtitle: "Oingo Boingo",
  image_key: "48f5b5fe1ee1dcd0f89bf0f6babcc93a",
  can_descend: true, can_play: true
}

test("imageUrl builds a Core image URL from a bare key", () => {
  assert.strictEqual(
    M.imageUrl(CORE, "abc", 64),
    "http://192.168.50.118:9330/api/image/abc?scale=fit&width=64&height=64")
})

test("imageUrl returns empty string with no core", () => {
  assert.strictEqual(M.imageUrl(null, "abc", 64), "")
})

test("imageUrl returns empty string with no key", () => {
  assert.strictEqual(M.imageUrl(CORE, null, 64), "")
})

test("imageUrl falls back to port 9330", () => {
  assert.ok(M.imageUrl({ host: "h" }, "k", 32).indexOf(":9330/") > 0)
})

test("rowArtUrl uses the row's own image_key", () => {
  assert.ok(M.rowArtUrl(STATE, ROW, 48)
    .indexOf("48f5b5fe1ee1dcd0f89bf0f6babcc93a") > 0)
})

test("rowArtUrl is empty for a row with no art -- a category", () => {
  // Measured: category rows carry image_key null (spec 2.4).
  assert.strictEqual(
    M.rowArtUrl(STATE, { title: "Albums", image_key: null }, 48), "")
})

test("artUrl still works and still reads now_playing", () => {
  const playing = {
    v: 1, status: "ok", core: CORE,
    zone: { now_playing: { image_key: "zzz" } }, zones: []
  }
  assert.ok(M.artUrl(playing, 256).indexOf("/api/image/zzz") > 0)
})

test("moveCursor steps down and stops at the last row", () => {
  assert.strictEqual(M.moveCursor(0, 1, 3), 1)
  assert.strictEqual(M.moveCursor(2, 1, 3), 2)
})

test("moveCursor steps up and stops at the first row", () => {
  assert.strictEqual(M.moveCursor(1, -1, 3), 0)
  assert.strictEqual(M.moveCursor(0, -1, 3), 0)
})

test("moveCursor on an empty list stays at -1", () => {
  assert.strictEqual(M.moveCursor(-1, 1, 0), -1)
})

test("moveCursor clamps a cursor left past the end by a shorter page", () => {
  assert.strictEqual(M.moveCursor(9, 0, 3), 2)
})

// `rowLabel` was cut in the pre-flight scan: nothing consumed it. An exported
// helper with no caller is the kind of thing this project's own review rubric
// treats as a defect, and it is trivial to add back if a tooltip ever wants it.
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `node --test tests/model-browse.test.js`
Expected: FAIL with `M.imageUrl is not a function`.

- [ ] **Step 3: Write the implementation**

Replace `artUrl` in `Model.js` with the extracted pair, and add the cursor helpers:

```javascript
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

```

Add `imageUrl`, `rowArtUrl`, `moveCursor` to the `module.exports` block, keeping `artUrl` in place.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./bin/test`
Expected: green, including every pre-existing `artUrl` test.

- [ ] **Step 5: Verify the clamp can fail**

Change `moveCursor` to wrap (`next % count`) and confirm `moveCursor steps down and stops at the last row` fails. Revert.

- [ ] **Step 6: Commit**

```bash
git add Model.js tests/model-browse.test.js
git commit -m "feat(browse): imageUrl extraction and cursor helpers"
```

---

## Task 9: `Service.qml` request/response RPC

**Files:**
- Modify: `Service.qml`

**Interfaces:**
- Consumes: `tonearmctl browse ...` from Task 7.
- Produces: `Service.browse(args, callback)` where `args` is an array of strings appended after `browse`, and `callback(reply)` receives the parsed object or `null` on failure.

- [ ] **Step 1: Add the RPC component**

`Process.command` assigned mid-run is silently ignored (`Service.qml:82-89`), so each call gets its own `Process` created from a `Component` and destroyed on completion.

```qml
  // One Process per call, created here and destroyed in onExited. Reassigning
  // command on a running Process is silently ignored (galley trap #11), and a
  // single shared Process would drop every overlapping browse request --
  // which is exactly what happens when a keypress lands while a search is
  // still in flight.
  Component {
    id: rpcComponent

    Process {
      id: rpc
      property var callback: null
      property string buffer: ""

      stdout: SplitParser {
        onRead: function (line) {
          if (line && line.length > 0 && rpc.buffer === "") rpc.buffer = line
        }
      }

      // onRunningChanged, NOT onExited. A failed spawn never emits exited()
      // -- measured, and documented at Service.qml:52-56: the process goes
      // straight to running=false without ever passing through true. Firing
      // the callback from onExited would mean a failed spawn never calls back
      // at all, so BrowsePane's `busy` flag would stay true forever and the
      // pane would freeze with no error anywhere. onRunningChanged is the one
      // drain signal that covers both a failed spawn and a normal exit.
      //
      // `done` guards against a double fire: onRunningChanged also runs on the
      // false->true transition when the process starts, and a callback invoked
      // twice would clear `busy` before the real reply arrives.
      property bool done: false

      onRunningChanged: {
        if (rpc.running || rpc.done) return
        rpc.done = true
        var parsed = null
        if (rpc.buffer.length > 0) {
          try {
            parsed = JSON.parse(rpc.buffer)
          } catch (e) {
            console.warn("tonearm: unparseable browse reply")
          }
        }
        if (rpc.callback) rpc.callback(parsed)
        rpc.destroy()
      }
    }
  }

  // Fire a browse op and hand the parsed reply to `callback`. The callback is
  // guaranteed to run exactly once -- on a reply, on a crash, or on a failed
  // spawn -- because the Process above drains on onRunningChanged rather than
  // onExited. Callers rely on that guarantee to clear their `busy` flag; a
  // path that can skip the callback freezes the pane silently.
  function browse(args, callback) {
    var argv = [root.ctlPath, "browse"]
    for (var i = 0; i < args.length; i++) argv.push(String(args[i]))
    var proc = rpcComponent.createObject(root, {
      command: argv,
      callback: callback
    })
    if (proc === null) {
      console.warn("tonearm: could not create browse process")
      if (callback) callback(null)
      return
    }
    proc.running = true
  }
```

- [ ] **Step 2: Verify it parses**

Run: `./bin/test`
Expected: the `qml syntax` section passes. `qmllint` catches parse errors only — it does not resolve Quickshell imports, so this proves the file parses and nothing more.

- [ ] **Step 3: Verify live**

Deploy and exercise it from the running shell:

```bash
./bin/dev deploy
```

Then, with the popup open, confirm via `journalctl --user -u tonearmd.service -f` that a `browse search` reaches the daemon. If the session is locked and `bin/dev up` refuses to restart the shell, note that plugin code hot-reloads and the restart is optional.

- [ ] **Step 4: Commit**

```bash
git add Service.qml
git commit -m "feat(browse): request/response RPC in Service.qml"
```

---

## Task 10: `BrowsePane.qml`

**Files:**
- Create: `BrowsePane.qml`

**Interfaces:**
- Consumes: `Service.browse(args, callback)` (Task 9); `Model.moveCursor`, `Model.rowArtUrl` (Task 8).
- Produces: properties `service`, `state`, `active`, `editing`, `rowCount`; signals `playStarted()`, `closeRequested()`; functions `handleMove(dx, dy)`, `handleActivate()`, `handleQueue()`, `handleBack()`, `focusSearch()`.

- [ ] **Step 1: Write the component**

```qml
import QtQuick
import Quickshell
import qs.Commons
import qs.Ui
// NOT QtQuick.Controls: it also exports a TextField, and which one wins is
// decided by import order rather than by anything visible at the use site.
// The shell's own qs.Ui TextField is the one that carries the theme. Nothing
// else here needs Controls -- ListView and Text are QtQuick.
// REQUIRED: this file calls Model.moveCursor and Model.rowArtUrl.
// Without this import each is a ReferenceError raised inside
// a signal handler -- invisible to qmllint, and it fails as a pane whose
// arrow keys silently do nothing.
import "Model.js" as Model

// Search + results. Owns all browse state; Panel.qml owns only the split
// layout. Extracted rather than folded into Panel.qml because that file is
// already 552 lines and this would put it past 800 (spec 6).
Item {
  id: root

  property var service: null
  property var state: null
  property int artPx: Style.space(30)
  property string fontFamily: ""

  // Browse state. `levelId` is the generation counter from spec 5.1.1 -- it
  // MUST accompany every index-addressed op, or a session reset between
  // render and keypress plays the wrong album silently.
  property var rows: []
  property int levelId: -1
  property var path: []
  property int cursor: -1
  property bool busy: false
  property string errorText: ""
  property bool editing: false

  readonly property int rowCount: rows.length
  readonly property bool hasResults: rows.length > 0

  signal playStarted()
  signal closeRequested()

  implicitHeight: column.implicitHeight

  function focusSearch() {
    root.editing = true
    field.forceActiveFocus()
  }

  function _apply(reply) {
    root.busy = false
    if (!reply) { root.errorText = "tonearmd is not answering"; return }
    if (reply.ok === false) {
      // A stale reply is not a user-visible failure: the screen was simply out
      // of date. Re-render and discard the keystroke rather than replaying it
      // against a level the user never saw (spec 5.2).
      root.errorText = reply.error === "stale" ? "" : (reply.message || "error")
      if (reply.rows !== undefined) {
        root.rows = reply.rows
        root.levelId = reply.level_id
        root.path = reply.path || []
        root.cursor = Model.moveCursor(root.cursor, 0, root.rows.length)
      }
      return
    }
    root.errorText = ""
    root.rows = reply.rows || []
    root.levelId = reply.level_id
    root.path = reply.path || []
    root.cursor = Model.moveCursor(root.cursor, 0, root.rows.length)
  }

  // `after` runs once the reply has been applied, so a caller can react to
  // what the daemon actually did rather than to what it hoped would happen.
  function _send(args, after) {
    if (!root.service || root.busy) return
    root.busy = true
    root.service.browse(args, function (reply) {
      root._apply(reply)
      if (after) after(reply)
    })
  }

  function search(term) {
    if (!term || term.length === 0) return
    root.cursor = -1
    root.editing = false
    _send(["search", term])
  }

  // Horizontal movement is navigation, not cursor movement: right descends,
  // left goes back (spec 7.2). PanelKeyCatcher delivers both axes through one
  // signal, so they are separated here rather than in Panel.qml.
  function handleMove(dx, dy) {
    if (root.editing) return
    if (dx > 0) { root.handleDescend(); return }
    if (dx < 0) { root.handleBack(); return }
    if (dy === 0) return
    root.cursor = Model.moveCursor(root.cursor, dy, root.rows.length)
    list.positionViewAtIndex(root.cursor, ListView.Contain)
  }

  // Enter: activate -- plays if playable, descends if not. The widget must NOT
  // decide which: spec 2.4 measured that a category row and an album row are
  // both hint "list" and indistinguishable before descending, so any rule here
  // would be a guess that fails on art-less albums.
  //
  // The popup closes only when the daemon reports it actually PLAYED
  // (spec 7.3). Emitting playStarted() unconditionally would close the popup
  // on Enter over a category -- which descends -- so the user would see the
  // right thing happen and the window vanish on top of it.
  function handleActivate() {
    if (root.editing || root.cursor < 0) return
    _send(["activate", String(root.cursor), String(root.levelId)],
          function (reply) {
            if (reply && reply.ok !== false && reply.played === true) {
              root.playStarted()
            }
          })
  }

  function handleDescend() {
    if (root.editing || root.cursor < 0) return
    if (!root.rows[root.cursor].can_descend) return
    _send(["enter", String(root.cursor), String(root.levelId)])
  }

  function handleQueue() {
    if (root.editing || root.cursor < 0) return
    _send(["play", String(root.cursor), "queue", String(root.levelId)])
  }

  // Returns true when it consumed the key. Panel.qml uses that to decide
  // whether Esc should close the popup instead (spec 7.2).
  function handleBack() {
    if (root.editing) { root.editing = false; return true }
    if (root.path.length > 1) { _send(["back"]); return true }
    if (root.path.length === 1) {
      root.rows = []
      root.path = []
      root.cursor = -1
      if (root.service) root.service.browse(["reset"], function () {})
      return true
    }
    return false
  }

  Column {
    id: column
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.top: parent.top
    spacing: Style.space(8)

    TextField {
      id: field
      width: parent.width
      placeholderText: "Search library"
      onAccepted: root.search(text)
      // Esc leaves the field without closing the popup; Panel.qml's key
      // catcher is blocked while this has focus, so it never sees this key.
      Keys.onEscapePressed: root.editing = false
      onActiveFocusChanged: if (activeFocus) root.editing = true
    }

    Text {
      width: parent.width
      visible: root.path.length > 0
      text: "‹  " + root.path.join("  ›  ")
      color: Color.muted
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      elide: Text.ElideRight
    }

    Text {
      width: parent.width
      visible: root.errorText.length > 0
      text: root.errorText
      color: Model.COLOR_ERROR
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      wrapMode: Text.WordWrap
    }

    Text {
      width: parent.width
      visible: !root.busy && root.path.length > 0 && root.rows.length === 0
               && root.errorText.length === 0
      text: "No results"
      color: Color.muted
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }

    ListView {
      id: list
      width: parent.width
      // Fixed maximum so the popup grows once and then stops, rather than
      // tracking result count into an unusable column (spec 7.1).
      height: Math.min(contentHeight, Style.space(8 * 38))
      visible: root.rows.length > 0
      clip: true
      model: root.rows
      boundsBehavior: Flickable.StopAtBounds

      delegate: Rectangle {
        width: list.width
        height: Style.space(38)
        // Util.alpha(Color.foreground, 0.08) is the shell's own selected-row
        // colour (Ui/ConfirmDialog.qml:15). Color.surfaceVariant does NOT exist.
        color: index === root.cursor ? Util.alpha(Color.foreground, 0.08) : "transparent"
        radius: Style.space(4)

        Row {
          anchors.fill: parent
          anchors.leftMargin: Style.space(6)
          anchors.rightMargin: Style.space(6)
          spacing: Style.space(9)

          Rectangle {
            anchors.verticalCenter: parent.verticalCenter
            width: root.artPx
            height: root.artPx
            radius: Style.space(3)
            color: Color.muted
            clip: true
            visible: modelData.image_key !== null
                     && modelData.image_key !== undefined

            Image {
              anchors.fill: parent
              source: Model.rowArtUrl(root.state, modelData, root.artPx * 2)
              sourceSize.width: root.artPx * 2
              sourceSize.height: root.artPx * 2
              fillMode: Image.PreserveAspectCrop
              asynchronous: true
            }
          }

          Column {
            anchors.verticalCenter: parent.verticalCenter
            width: parent.width - root.artPx - Style.space(15)
            spacing: Style.space(1)

            Text {
              width: parent.width
              text: modelData.title
              color: Color.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              elide: Text.ElideRight
            }

            Text {
              width: parent.width
              visible: modelData.subtitle.length > 0
              text: modelData.subtitle
              color: Color.muted
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
            }
          }
        }

        MouseArea {
          anchors.fill: parent
          onClicked: { root.cursor = index; root.handleActivate() }
        }
      }
    }
  }
}
```

- [ ] **Step 2: Verify it parses**

Run: `./bin/test`
Expected: `qml syntax` passes.

- [ ] **Step 3: Confirm every `Style` and `Color` role used actually resolves**

These were verified against the live shell before this plan was dispatched, and
two were wrong in an earlier draft: `Color.surfaceVariant` does not exist, and
the size token is `caption`, not `small`. The corrected set is:

| Used | Verified as |
|---|---|
| `Util.alpha(Color.foreground, 0.08)` | `Ui/ConfirmDialog.qml:15` uses exactly this for a selected row. `Util` is a `qs.Commons` singleton (`Commons/qmldir:5`). |
| `Color.foreground`, `Color.muted` | Top-level roles in `Commons/Color.qml`. |
| `Style.font.body`, `Style.font.caption` | Both in live use across this repo's own `Panel.qml`. |
| `Style.space(N)` | In live use at `Panel.qml:200`. |

Re-run the check anyway and confirm, because `qmllint` cannot see this class of
error and it is how six MVP defects reached review:

```bash
grep -rnE "property color (foreground|muted)" /usr/share/omarchy/shell/Commons/Color.qml
grep -rhoE "Style\.font\.[a-zA-Z]+" /home/sean/Src/tonearm/*.qml | sort -u
```

**If any role does not resolve, substitute one that does and record it in the report.**

- [ ] **Step 4: Commit**

```bash
git add BrowsePane.qml
git commit -m "feat(browse): BrowsePane search and results component"
```

---

## Task 11: Wire the pane into `Panel.qml`

**Files:**
- Modify: `Panel.qml:189-220`

**Interfaces:**
- Consumes: `BrowsePane` (Task 10).
- Produces: the split popup of spec §7.1 with the key map of spec §7.2.

- [ ] **Step 1: Add the pane below the existing content**

Inside `contentColumn`, after the existing now-playing rows and transport, add:

```qml
      PanelSeparator {
        width: parent.width
        visible: browsePane.rowCount > 0 || browsePane.editing
      }

      BrowsePane {
        id: browsePane
        width: parent.width
        service: service
        state: root.st
        fontFamily: root.fontFamily
        onPlayStarted: root.close()
        onCloseRequested: root.close()
      }
```

- [ ] **Step 2: Route the keys**

Replace the existing `PanelKeyCatcher` block at `Panel.qml:203-207`:

```qml
    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      // Gate on the pane's own editing flag, not on activeFocus: that is the
      // pattern the shell's own network panel uses for its passphrase field
      // (plugins/panels/network/Panel.qml:996, `blocked: root.passwordSsid !== ""`).
      // Unblocked while typing, every letter would be swallowed as a shortcut.
      blocked: browsePane.editing

      onMoveRequested: function (dx, dy) { browsePane.handleMove(dx, dy) }
      onActivateRequested: browsePane.handleActivate()

      // Esc backs out one level and only closes at the top (spec 7.2). One
      // stray Esc must not discard a whole navigation.
      onCloseRequested: { if (!browsePane.handleBack()) root.close() }

      onTextKey: function (text) {
        if (text === "q") { browsePane.handleQueue(); return }
        if (text === "/") { browsePane.focusSearch(); return }
        // Any other printable key starts a search with that character, so
        // typing goes straight into the field without a preparatory keystroke.
        if (text && text.length === 1 && text >= " ") {
          browsePane.focusSearch()
        }
      }
    }
```

- [ ] **Step 3: Verify it parses and the panel still sizes correctly**

Run: `./bin/test`

Then deploy and open the popup:

```bash
./bin/dev deploy
```

Expected: the popup renders exactly as before when no search has run — `BrowsePane` contributes zero height with no rows and no editing. `contentHeight` binds to `contentColumn.implicitHeight` (`Panel.qml:201`), so a pane with a non-zero implicit height when empty would grow the popup permanently. Confirm visually that it has not.

- [ ] **Step 4: Commit**

```bash
git add Panel.qml
git commit -m "feat(browse): split popup layout and key routing"
```

---

## Task 12: Live verification and documentation

**Files:**
- Modify: `README.md`, `AGENTS.md`, `docs/FOLLOWUPS.md`

**Interfaces:**
- Consumes: everything.
- Produces: no code. A recorded live run and the traps written down.

- [ ] **Step 1: Verify the whole flow against `yavin`**

With `tonearmd` running and the plugin deployed, exercise each of these and record the observed result in the report:

1. Open the popup, type `oingo boingo`, press Enter. Expect the grouped categories from spec §2.2.
2. Arrow down to `Albums`, press Enter. Expect it to **descend** (the `activate` fallback), showing 21 albums.
3. Arrow to an album, press Enter. Expect music to start and the popup to close.
4. Reopen, search, descend to an album, press `q`. Expect it queued and the popup to stay open.
5. Press `←` and `Esc` at each level. Expect one level of backing out per press, and a close only at the top.
6. Stop `tonearmd`, then search. Expect the pane to report unreachable rather than hang.
7. Restart `tonearmd` mid-navigation, then press Enter on a row. Expect **no playback** and a silent re-render — this is the `stale` path from spec §5.1.1 and the one failure the design exists to prevent.

- [ ] **Step 2: Add the measured traps to `AGENTS.md`**

Append to the existing trap list, in the same style:

```markdown
- **Roon's search is not a hierarchy.** It is an item inside `Library` carrying
  `input_prompt`, and the query rides with that item's `item_key`. Every attempt
  to use a top-level `search` hierarchy returns one item titled `No Results` and
  never errors — indistinguishable from an empty library.
- **A stale `item_key` returns the browse ROOT with no error.** Never re-walk to
  go back; use `pop_levels: 1`. This is why replies carry no `item_key` at all.
- **A category row and an album row are both `hint: "list"`.** They cannot be told
  apart before descending. `image_key` is null on categories but also null on
  art-less albums, so it must never be used to infer playability — that is what
  the `activate` op is for.
- **Depth to a playable action is uneven**: 1 descent for a track, 2 for an album.
- **`multi_session_key` works through the vendored library untouched**, because
  `browse_browse` passes its opts dict to `_request` verbatim. Two consumers do
  not clobber each other.
- **Never use `list_media`/`play_media`.** Both open with `pop_all: True` and
  reset the browse session as a side effect.
```

- [ ] **Step 3: Document search in `README.md`**

Add a short section covering the key map from spec §7.2 and the fact that search is library-wide. Keep the existing removal instructions intact — they are a marketplace submission requirement.

- [ ] **Step 4: Update `docs/FOLLOWUPS.md`**

Item 3 (`sendall` under `Server._lock`) is now load-bearing rather than theoretical: browse makes concurrent socket traffic routine. Re-word it to say so. Do not close it — this work does not fix it.

Then add one new follow-up, because this plan knowingly leaves it open:

```markdown
## Paging is implemented in the protocol but unreachable from the UI

`BrowseSession.page(offset)`, the `page` op and `tonearmctl browse page` all
work and are tested. `BrowsePane.qml` never calls them, so only the first 100
rows of a level are reachable from the widget.

This is invisible for search results, which are narrow — the measured
`"Oingo Boingo"` case returns 21 albums and 44 tracks. It becomes visible on a
common single-word search against a large library, where `Tracks` could exceed
100. The fix is to call `page` when the `ListView` nears its end and append,
which also needs the daemon to return rows for an offset without resetting the
cursor — `page` already does exactly that.
```

- [ ] **Step 5: Run the full suite and validate the plugin**

```bash
./bin/test && omarchy plugin validate .
```

Expected: green, exit 0.

- [ ] **Step 6: Commit**

```bash
git add README.md AGENTS.md docs/FOLLOWUPS.md
git commit -m "docs: browse traps, key map, and follow-up reprioritisation"
```
