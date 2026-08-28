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
            # with no error (spec 2.5). Staleness is POSITIONAL -- a key is
            # only ever resolved against the level the fake is currently on
            # (see _target_for), so a real key left over from a level we have
            # since navigated away from takes this same path.
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
        # Only the CURRENT level's items are searched. Real Roon staleness is
        # positional (spec 2.5, measured): a key valid at a previous position
        # becomes invalid once you've navigated away, even if that exact key
        # string still belongs to some other, un-navigated-to level. Searching
        # every level here (rather than just self.current) would let such a
        # key resolve successfully, which is more forgiving than the real
        # server and would hide the bug this fake exists to catch.
        for item in self.levels[self.current]["items"]:
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
