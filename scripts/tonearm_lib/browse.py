"""Roon library browse: a navigation cursor per session, and row normalization.

`BrowseSession`, one per `multi_session_key`, is that cursor (spec 2.3): it
isolates a future consumer's browsing from the widget's own position. Its
first operation is `search()`, which walks root -> Library -> Search and
adopts the results; `current()` re-reads the adopted state with no
round-trip.

The widget never sees a Roon item_key (spec 5.3). Real keys live in a
private `_keys` array, parallel to the displayed rows and dropped from every
reply. Rows are meant to be addressed by index against `level_id`, a counter
bumped on every level change -- that pairing is what will make the stale-key
trap -- a stale key silently returns the hierarchy ROOT with no error, spec
2.5 -- unreachable rather than merely unlikely, once a later operation
accepts an index. `search()` needs no index itself: it always starts a
fresh level from root.
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
