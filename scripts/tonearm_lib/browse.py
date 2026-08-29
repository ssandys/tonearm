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

# Roon's action titles, measured on yavin (spec 2.4). Identical for albums
# and tracks.
ACTIONS = {
    "play_now": "Play Now",
    "add_next": "Add Next",
    "queue": "Queue",
    "start_radio": "Start Radio",
}

# How deep to hunt for an action list before giving up. Measured live
# against a real Core (R12 fix round 3): a track is 1 descent from its row;
# an album is 3 -- row -> single-item wrapper -> contents (1), contents ->
# "Play Album" (2), "Play Album" -> Play Now (3) -- one level deeper than
# either fixture in this file modelled, which is exactly why the earlier
# value of 3 sat precisely on the limit with zero headroom and 13 passing
# tests still shipped a broken "play an album" feature. 4 gives one level of
# margin, since Roon has already surprised us once with a level our fixtures
# did not model and a silent, identical failure is worse than a slightly
# slower give-up.
MAX_ACTION_DEPTH = 4


class BrowseError(Exception):
    """Carries a stable machine token from spec 5.2.

    `level` is the optional level payload spec 5.1.1/5.2 promise alongside a
    `stale` reply ("including the current level so the widget can re-render").
    Without it the pane is left showing rows it can never act on: a `stale`
    reply deliberately clears errorText, so a widget holding a level_id the
    daemon has moved past -- the exact state a daemon restart leaves behind,
    spec 7.4's "Daemon restarts" row -- gets an empty error, no rows, and
    every subsequent keystroke returns `stale` forever with nothing on screen
    saying why. `roon_error` carries it too, for the same reason: the session
    has just reset to root underneath the widget (spec 7.4) and the pane must
    be told, or it keeps rendering the level that no longer exists.

    It is a plain `current()` dict (never None-by-default surprise): callers
    that have nothing useful to say simply leave it None, and server.py
    merges it into the error reply only when it is present.
    """

    def __init__(self, token: str, message: str, level: dict | None = None) -> None:
        super().__init__(message)
        self.token = token
        self.message = message
        self.level = level


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

    def __init__(self, api, key: str, zone_id_provider=None) -> None:
        self._api = api
        self._key = key
        # A CALLABLE, not a zone id, and deliberately so. The user can repin
        # between two browses (the popup has a zone switcher), and a value
        # captured at construction would send the next play to the room they
        # just left -- silently, since Roon reports success either way (see
        # _opts). A callable is also what makes this testable without a Core.
        self._zone_id_provider = zone_id_provider
        self._lock = threading.RLock()
        self.level_id = 0
        self._path: list[str] = []
        self._rows: list[dict] = []
        self._keys: list[str] = []
        self._count = 0
        self._offset = 0
        # Roon's OWN level number for the level currently adopted, straight
        # off loaded["list"]["level"]. Only ever compared RELATIVELY (see
        # _expect), never against an absolute depth this code computes: the
        # absolute number Roon assigns to a search-results level is not
        # something this codebase has measured, but "one descent goes down
        # exactly one level" is -- it is the same fact back() already depends
        # on when it pops exactly one.
        self._level: int | None = None

    # -- Roon plumbing -------------------------------------------------

    def _zone_id(self):
        """The zone every browse call must target, read at CALL time."""
        if self._zone_id_provider is None:
            return None
        return self._zone_id_provider()

    def _opts(self, **extra) -> dict:
        """Common browse opts. `zone_or_output_id` is not optional.

        MEASURED live against the Core, same album, same code path, with only
        this field differing:

            [nozone]   invoking Play Now -> zone state: paused  | Insanity
            [withzone] invoking Play Now -> zone state: playing | Speak to Me

        A browse action with no `zone_or_output_id` succeeds at the protocol
        level and plays into nothing -- so `play`/`activate` reported
        `played: true` while the zone never changed. That is the worst shape
        of failure available here: the widget closes the popup, reports
        success, and no music starts. The vendored library does the same thing
        for the same reason (roonapi.py:590-595 puts it in play_media's browse
        opts, and in its load opts too).

        Omitted entirely rather than sent as null when no zone is selected:
        navigation must still work with no zone, and sending an explicit null
        for a field Roon expects to be an id is a different request than not
        sending the field. `play` refuses outright instead -- see play().
        """
        opts = {"hierarchy": "browse", "multi_session_key": self._key}
        zone_id = self._zone_id()
        if zone_id:
            opts["zone_or_output_id"] = zone_id
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

    def _expect(self, delta: int):
        """The Roon `level` a descent/ascent of `delta` should land on.

        None until the first adopt has recorded a baseline (search establishes
        it), and None is "no expectation" -- the absolute level Roon assigns
        to a search-results level is not measured here, only the relative
        step, so the baseline is taken from Roon itself rather than computed.
        """
        return None if self._level is None else self._level + delta

    @staticmethod
    def _level_of(lst: dict):
        """Roon's own level number for a loaded list, or None if it did not say.

        The ONE place a missing or unparseable `level` is tolerated, so that
        tolerance is a single decision rather than the same guard repeated at
        every reader. A Core that does not report it must stay browsable: with
        None here, `_expect` yields None and `_verify_position` has no opinion,
        which is exactly the pre-existing behaviour.
        """
        try:
            return int(lst["level"])
        except (KeyError, TypeError, ValueError):
            return None

    def _verify_position(self, lst: dict, expected_level) -> None:
        """Catch Roon silently resetting to the browse ROOT (spec 2.5, 7.4).

        A stale item_key returns the hierarchy root WITH NO ERROR. Nothing
        used to notice: `_adopt` read only `count` from `loaded["list"]`, so
        the root's rows were adopted as if they were the requested level's,
        with `ok: true` and a bumped level_id. Reproduced with one injected
        load failure and a replayed keystroke:

            enter #2 ok. path = ['Search','Albums','Albums']
                        rows = ['Library']
                        level_id = 2   real position = root

        That is the "half-broken cursor" spec 7.4 forbids, reached without a
        single error. `loaded["list"]["level"]` is Roon's own answer to "where
        am I", and level 0 is the root.

        Only a drop to level 0 RESETS, deliberately. That is the one failure
        mode measured on a real Core, and a level of 0 where a descent was
        expected cannot be anything else. Any other mismatch is logged and
        tolerated: the relative arithmetic here has never been checked against
        a Core for hierarchies this code does not walk, and a false positive
        would turn working navigation into a hard error -- strictly worse than
        the warning it would replace. Missing or unparseable `level` is
        likewise tolerated -- see `_level_of`, the one place that decides it.
        """
        if expected_level is None:
            return
        level = self._level_of(lst)
        if level is None or level == expected_level:
            return
        if level == 0:
            self._reset_locked()
            raise BrowseError(
                "roon_error",
                "Roon returned to the browse root; the view has been reset",
                self.current())
        LOG.warning("Roon reported level %d where %d was expected",
                    level, expected_level)

    def _adopt(self, loaded, offset: int, path: list, expected_level=None) -> dict:
        """Record a freshly loaded level as the current one, and bump the id.

        `path` is the caller's PROPOSED breadcrumb, adopted here rather than
        assigned by the caller beforehand. enter() used to extend self._path
        before this ran; a _load() that then failed (roonapi gives up after
        ~2.5s) left _path describing the child while _rows/_keys/level_id
        still described the parent, and Roon genuinely in the child -- a
        divergence no error reported and the level_id check could not catch,
        because level_id had not moved either.
        """
        items = loaded.get("items") or []
        lst = loaded.get("list") or {}
        self._verify_position(lst, expected_level)
        self._rows = normalize_rows(items)
        # Parallel to _rows and dropped from every reply. The widget addresses
        # rows by index; this is where the real keys stay.
        self._keys = [
            i.get("item_key") for i in items
            if i.get("item_key") or (i.get("title") or "") != "No Results"
        ]
        self._count = 0 if not self._rows else int(lst.get("count") or 0)
        self._offset = offset
        self._path = list(path)
        self._level = self._level_of(lst)
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
            # No expected level: search walks to wherever Roon puts results
            # and that absolute number is the BASELINE every later relative
            # check is measured from, not something to check itself.
            return self._adopt(self._load(), 0, ["Search"])

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

        spec 5.1.1 requires index-addressed ops to carry level_id -- so a
        missing/None level_id is treated as stale, not as "unchecked", and a
        level_id that fails to parse as an int is stale too, never a bare
        ValueError. Both keep the same fail-safe response: the widget
        re-renders and discards the keystroke without ever reaching Roon.
        """
        try:
            matches = level_id is not None and int(level_id) == self.level_id
        except (TypeError, ValueError):
            matches = False
        if not matches:
            # The current level rides along (spec 5.1.1: "including the
            # current level so the widget can re-render"). current() is a
            # plain read of already-adopted state and takes no lock of its
            # own; _check only ever runs under self._lock already.
            raise BrowseError(
                "stale", "the view is out of date; it has been refreshed",
                self.current())
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
            # The new breadcrumb is a LOCAL until _adopt commits it. See
            # _adopt's docstring: assigning it here left the session
            # describing a level it had failed to load.
            return self._adopt(self._load(), 0, self._path + [title],
                               self._expect(1))

    def back(self) -> dict:
        """One level up, via pop_levels -- never a re-walk (spec 2.5)."""
        with self._lock:
            if len(self._path) <= 1:
                return self.current()
            self._browse(pop_levels=1)
            return self._adopt(self._load(), 0, self._path[:-1],
                               self._expect(-1))

    def page(self, offset: int) -> dict:
        with self._lock:
            return self._adopt(self._load(int(offset)), int(offset),
                               self._path, self._expect(0))

    def reset(self) -> dict:
        with self._lock:
            return self._reset_locked()

    def _reset_locked(self) -> dict:
        """Back to an empty session. Caller must hold self._lock."""
        self._path = []
        self._rows = []
        self._keys = []
        self._count = 0
        self._offset = 0
        self._level = None
        self.level_id += 1
        return self.current()

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
            # After the stale check, which is the fail-safe one and must keep
            # coming first, but before any Roon call. Without a zone, invoking
            # an action succeeds and plays into nothing (see _opts), so this
            # has to fail LOUDLY -- reporting `played: true` for silence is
            # exactly the lie this whole finding is about. Navigation
            # (search/enter/back/page) deliberately does not check: browsing
            # with no zone selected is perfectly meaningful.
            if not self._zone_id():
                raise BrowseError(
                    "no_zone",
                    "no Roon zone is selected to play into")
            # Bound BEFORE the try: if the first _browse raises, the finally
            # clause and the check below both still need these names.
            depth = 0
            descents = []
            found = False
            try:
                self._browse(item_key=key)
                depth = 1
                found = self._descend_to_action(title, descents)
                # `descents` is appended to on EVERY exit path of
                # _descend_to_action -- success, dead end, or depth
                # exhausted -- so depth is accurate even when found is
                # False. Computing it from a value that could go missing
                # on failure (a returned list discarded by an early
                # `return []`) was the exact bug this depends on not
                # reintroducing: it silently undercounted, and _unwind
                # then popped too few levels, stranding the user partway
                # down a branch they never asked to enter.
                depth += len(descents)
            finally:
                # Unwind to exactly where the user was, whatever happened.
                # Leaving them inside an action list -- or worse, at the root
                # after a silent stale-key reset -- would be a visible bug.
                self._unwind(depth)
            if not found:
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
                # ONLY no_action falls back to a descend. `no_zone` in
                # particular must propagate: descending into an album because
                # there is nowhere to play it would look like the widget did
                # something reasonable, and the user would never learn why no
                # music started. Enter with no zone says so instead.
                if exc.token != "no_action":
                    raise
                reply = self.enter(index, level_id)
                reply["played"] = False
                return reply

    def _descend_to_action(self, title: str, descents: list) -> bool:
        """Walk down looking for an action of exactly `title`.

        Appends every real descent to the CALLER-OWNED `descents` list as it
        happens -- on the success path, the dead-end path, and the
        depth-exhausted path alike -- and returns only whether the action was
        found. An out-param instead of a return value is what makes the
        unwind exact on every exit: a return value can be, and WAS, discarded
        by an early `return []` on a failure path, silently losing track of
        real _browse() calls already made and leaving the caller to pop too
        few levels. Mutating a list the caller already holds cannot be
        "forgotten" by a return statement.

        A count rather than a bool is also what makes the unwind exact in
        the first place: comparing list titles to decide when to stop would
        break on any hierarchy where a child level shares its parent's
        title, and Roon does exactly that -- an album's detail level is
        titled after the album (measured, spec 2.4).
        """
        for _ in range(MAX_ACTION_DEPTH):
            loaded = self._load()
            items = loaded.get("items") or []
            for item in items:
                if item.get("title") == title and item.get("hint") == "action":
                    self._browse(item_key=item["item_key"])
                    descents.append(item["item_key"])
                    return True
            nxt = None
            for item in items:
                # hint "action_list" preferred, never a "list" among SEVERAL:
                # "list" is a row of DIFFERENT sibling items (spec 2.4's
                # can_descend=True taxonomy in capabilities_from_hint above)
                # -- wandering into one would silently walk from a category
                # into its first child's own action list and invoke THAT
                # item's action instead of reporting no_action, which is
                # exactly the ambiguity activate() exists to resolve
                # deliberately, not by accident. "action_list" instead means
                # "leads onward to actions for THIS SAME item"
                # (can_descend=False) -- the one continuation that is always
                # safe to follow automatically.
                if item.get("hint") == "action_list" and item.get("item_key"):
                    nxt = item["item_key"]
                    break
            if nxt is None:
                # Measured live against a real Core (R12 fix round 3): a
                # SINGLE-ITEM "list" wrapper sits between an album row and
                # its contents (album row -> wrapper, count 1 -> contents,
                # count 10: "Play Album" + 9 tracks). Its one item has no
                # "action_list" hint to prefer above, yet it must still be
                # followed or a real album can never be played. A CATEGORY
                # level (e.g. "Albums", 21 items) has no such single item --
                # only that count distinguishes the two, since both are hint
                # "list" and neither carries any other structural signal
                # (spec 2.4). So: follow a "list" item, but ONLY when it is
                # the level's one and only keyed item -- never when there are
                # several, which is exactly the category-wandering bug this
                # method exists not to reintroduce.
                keyed = [i for i in items if i.get("item_key")]
                if len(keyed) == 1 and keyed[0].get("hint") == "list":
                    nxt = keyed[0]["item_key"]
            if nxt is None:
                return False
            self._browse(item_key=nxt)
            descents.append(nxt)
        return False

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
