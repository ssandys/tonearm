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

from . import state
from .browse_play import SELECTED_ACTIONS

LOG = logging.getLogger("tonearmd.browse")

# Row text is Core-supplied and rendered inside omarchy-shell, the shared
# process. PAGE bounds how MANY rows a level returns; this bounds how long
# one row's text can be. Shared with state.py so there is one answer to
# "how long may Core text be", not two that can drift.
MAX_TEXT = state.MAX_TEXT

# Roon embeds links in subtitles as [[id|Display Text]]. Measured on yavin:
# album subtitles arrive as "[[827514|Oingo Boingo]]". Rendered raw, the
# brackets and the numeric id are visible to the user.
_LINK = re.compile(r"\[\[\d+\|([^\]]*)\]\]")


def strip_markup(text) -> str:
    """Display-ready subtitle. Never None -- spec 5.3.

    Clipped AFTER substitution, so a truncated subtitle can never end
    mid-link with `[[` still on screen.
    """
    if not text:
        return ""
    return _LINK.sub(r"\1", str(text))[:MAX_TEXT]


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
        "title": str(item.get("title") or "")[:MAX_TEXT],
        "subtitle": strip_markup(item.get("subtitle")),
        "image_key": item.get("image_key"),
        "can_descend": can_descend,
        "can_play": can_play,
        "can_navigate": can_descend and not bool(item.get("input_prompt")),
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

# Measured Core menu titles (spec 2.4), identical for album and track action
# lists. Legacy play() and activate() stay on Play Now. The selected-browse
# contract adds Play Next and Add to Queue as tokens in SELECTED_ACTIONS,
# mapped here to "Add Next" and "Queue". Callers cannot pass a Core title or
# any other string: Start Radio stays unexposed, and a missing queue action
# is no_action rather than a silent Play Now. One request still resolves one
# row, so a whole album is the album row's own action, not a track loop.
PLAY_NOW = SELECTED_ACTIONS["play_now"]
_ACTION_TITLES = frozenset(SELECTED_ACTIONS.values())

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
        # Keys belong to the connection that loaded them, not just the SDK
        # object (which replaces its socket in place on reconnect).
        self._loaded_connection = getattr(api, "_roonsocket", None)
        self._cursor_connection = self._loaded_connection
        # A CALLABLE, not a zone id, and deliberately so. The user can repin
        # between two browses (the popup has a zone switcher), and a value
        # captured at construction would send the next play to the room they
        # just left -- silently, since Roon reports success either way (see
        # _opts). A callable is also what makes this testable without a Core.
        self._zone_id_provider = zone_id_provider
        self._lock = threading.RLock()
        self._play_in_progress = False
        self._bound_play = None
        self._prompt_keys = set()
        self._terminal_keys = set()
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

    def _check_play_connection(self):
        if (self._bound_play is not None and
                getattr(self._api, "_roonsocket", None) is not self._bound_play["connection"]):
            raise BrowseError("core_changed", "Roon connection changed; no retry sent")

    def _zone_id(self):
        """The zone every browse call must target, read at CALL time."""
        if self._bound_play is not None:
            self._check_play_connection()
            error = self._bound_play["validate"]()
            if error:
                raise BrowseError(error, "Selected output or Core changed; no retry sent")
            return self._bound_play["zone_id"]
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
        # Recheck before each round-trip: an endpoint can vanish while an
        # action is being resolved. Never send an untargeted playback step.
        if self._play_in_progress and not zone_id:
            raise BrowseError("no_zone", "no Roon zone is selected to play into")
        if zone_id:
            opts["zone_or_output_id"] = zone_id
        opts.update(extra)
        return opts

    def _browse(self, **extra):
        opts = self._opts(**extra)
        if (self._bound_play is not None
                and extra.get("item_key") == self._bound_play.get("terminal_key")
                and "item_key" in extra):
            self._bound_play["attempted"] = True
        result = self._api.browse_browse(opts)
        # A reply from an SDK whose socket changed while waiting cannot
        # establish this action's result. attempted was latched before entry.
        self._check_play_connection()
        if result is None:
            raise BrowseError("roon_error", "Roon returned no response")
        if self._bound_play is not None and (
                not isinstance(result, dict) or "message" in result
                or bool(result.get("input_prompt"))
                or result.get("action") not in (None, "list", "none")):
            raise BrowseError("action_response", "Unsupported action response; no retry sent")
        return result

    def _load(self, offset: int = 0):
        opts = self._opts(offset=offset, count=PAGE)
        connection = getattr(self._api, "_roonsocket", None)
        result = self._api.browse_load(opts)
        self._check_play_connection()
        if result is None:
            raise BrowseError("roon_error", "Roon returned no response")
        # Capture BEFORE I/O: a reconnect during navigation must not relabel
        # an old reply's keys as belonging to the replacement connection.
        self._loaded_connection = connection
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
        self._prompt_keys = {i.get("item_key") for i in items if i.get("input_prompt")}
        self._terminal_keys = {i.get("item_key") for i in items if i.get("hint") == "action"}
        self._count = 0 if not self._rows else int(lst.get("count") or 0)
        self._offset = offset
        self._path = list(path)
        self._level = self._level_of(lst)
        self._cursor_connection = self._loaded_connection
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

    def library(self) -> dict:
        """Open Library without search/input/play actions on this session."""
        with self._lock:
            self._browse(pop_all=True)
            root = self._load()
            key = self._pick(root, lambda i: (i.get("title") or "").lower() == "library")
            if key is None:
                raise BrowseError("roon_error", "no Library in the browse root")
            self._browse(item_key=key)
            return self._adopt(self._load(), 0, ["Library"])

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

    def _check_level(self, level_id):
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

    def _check(self, index, level_id):
        self._check_level(level_id)
        if not isinstance(index, int) or index < 0 or index >= len(self._keys):
            raise BrowseError("bad_index", "no such row")
        key = self._keys[index]
        if not key:
            raise BrowseError("bad_index", "that row cannot be opened")
        return key

    def enter(self, index: int, level_id=None, navigation_only=False) -> dict:
        with self._lock:
            if navigation_only and (type(index) is not int or type(level_id) is not int):
                raise BrowseError("bad_index", "invalid navigation address")
            key = self._check(index, level_id)
            if navigation_only and not self._rows[index].get("can_navigate"):
                raise BrowseError("not_navigable", "This row requires search or playback, not navigation")
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

    def back_navigation(self, offset, level_id) -> dict:
        """Return to a known parent page without losing its key window."""
        with self._lock:
            if type(level_id) is not int:
                raise BrowseError("stale", "the view is out of date", self.current())
            self._check_level(level_id)
            if type(offset) is not int or offset < 0:
                raise BrowseError("bad_index", "parent offset must be a non-negative integer")
            if len(self._path) <= 1:
                return self.current()
            self._browse(pop_levels=1)
            return self._adopt(self._load(offset), offset, self._path[:-1], self._expect(-1))

    def page_navigation(self, offset, level_id) -> dict:
        """Opt-in guarded read. Legacy page(offset) stays compatible."""
        with self._lock:
            if type(level_id) is not int:
                raise BrowseError("stale", "the view is out of date", self.current())
            self._check_level(level_id)
            if type(offset) is not int or offset < 0:
                raise BrowseError("bad_index", "page offset must be a non-negative integer")
            return self._adopt(self._load(offset), offset, self._path, self._expect(0))

    def reset(self) -> dict:
        with self._lock:
            return self._reset_locked()

    def _reset_locked(self) -> dict:
        """Back to an empty session. Caller must hold self._lock."""
        self._path = []
        self._rows = []
        self._keys = []
        self._prompt_keys = set()
        self._terminal_keys = set()
        self._count = 0
        self._offset = 0
        self._level = None
        self._cursor_connection = None
        self._loaded_connection = None
        self.level_id += 1
        return self.current()

    def play_selected(self, index, level_id, zone_id, request_id, validate,
                      action="play_now") -> dict:
        """Opt-in target-bound Play Now, Play Next, or Add to Queue.

        Never claims an audible or queue effect. `action` is one of
        SELECTED_ACTIONS; the default keeps in-process Play Now callers
        stable. The wire contract requires the token explicitly and does not
        use this default. Caller supplies a short, read-only Core/selection
        guard. The existing session lock serializes this operation; no global
        lock spans SDK I/O. Legacy play/activate keep their historical
        behavior and cannot select another action.
        """
        with self._lock:
            bound = {"zone_id": zone_id, "validate": validate, "attempted": False,
                     "connection": self._cursor_connection}
            outcome, error = "refused", None
            self._bound_play = bound
            self._play_in_progress = True
            try:
                if (type(index) is not int or type(level_id) is not int
                        or not isinstance(zone_id, str) or not 0 < len(zone_id) <= 128
                        or not isinstance(request_id, str) or not 0 < len(request_id) <= 64
                        or type(action) is not str or action not in SELECTED_ACTIONS):
                    raise BrowseError("bad_request", "Invalid selected browse request")
                key = self._check(index, level_id)
                self._zone_id()  # guard BEFORE any SDK call
                if key in self._prompt_keys:
                    raise BrowseError("unsupported_prompt", "This action requires unsupported input")
                if key in self._terminal_keys or not self._rows[index].get("can_play"):
                    raise BrowseError("unsupported_action", "Direct action rows are not supported here")
                self._play(index, level_id, SELECTED_ACTIONS[action])
                outcome = "attempted"
            except BrowseError as exc:
                outcome = "unknown" if bound["attempted"] else "refused"
                error = exc.token
            except Exception:
                outcome = "unknown" if bound["attempted"] else "refused"
                error = "dispatch_failed"
                LOG.warning("selected browse failed; no retry sent")
            finally:
                # Failed resolution/unsafe unwind must not leave local keys
                # claiming a known remote cursor. A terminal action may move
                # the Core cursor too: require a new read after any attempt.
                # Do not repair via a replacement Core.
                try:
                    context_error = validate()
                except Exception:
                    context_error = "context_unknown"
                if error or context_error or bound.get("cleanup_failed") or bound["attempted"]:
                    self._reset_locked()
                self._bound_play = None
                self._play_in_progress = False
            reply = self.current()
            reply.update(request_id=request_id if isinstance(request_id, str) and len(request_id) <= 64 else None,
                         zone_id=zone_id if isinstance(zone_id, str) and len(zone_id) <= 128 else None,
                         action=action if type(action) is str and action in SELECTED_ACTIONS else None,
                         outcome=outcome, error=error, ok=outcome == "attempted")
            return reply

    def play(self, index: int, level_id=None) -> dict:
        with self._lock:
            self._play_in_progress = True
            try:
                return self._play(index, level_id)
            finally:
                self._play_in_progress = False

    def _play(self, index: int, level_id=None, title: str = PLAY_NOW) -> dict:
        """Resolve the row's action list, invoke `title`, return to the level.

        Resolution is LAZY (spec 4.3): precomputing which rows are playable
        would cost a descent per row per page. The cost is 2-3 extra
        round-trips before the action is sent, ~100-300ms. `title` must be
        one measured allowlisted Core title. A missing title is no_action;
        it is never replaced with Play Now.
        """
        with self._lock:
            key = self._check(index, level_id)
            if type(title) is not str or title not in _ACTION_TITLES:
                raise BrowseError("bad_request", "Invalid selected browse action")
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
                    "no_action",
                    "nothing here can be played" if title == PLAY_NOW
                    else "the requested queue action is not offered here")
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
                return self.play(index, level_id)
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
                    if self._bound_play is not None:
                        if item.get("input_prompt"):
                            raise BrowseError("unsupported_prompt", "This action requires unsupported input")
                        self._bound_play["terminal_key"] = item["item_key"]
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
            if self._bound_play is not None and any(
                    i.get("item_key") == nxt and i.get("input_prompt") for i in items):
                raise BrowseError("unsupported_prompt", "This action requires unsupported input")
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
            if self._bound_play is not None:
                self._bound_play["cleanup_failed"] = True
            LOG.warning("could not unwind %d level(s) after a play", depth)
