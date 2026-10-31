"""Argument parsing for tonearmctl. Pure, so it is unit-testable."""

from __future__ import annotations

import json
from . import targeted, browse_play, queue_wire

BARE_VERBS = ("playpause", "play", "pause", "next", "previous",
              "mute", "unmute", "subscribe", "status")
INT_VERBS = ("seek", "volume")

# The browse session key this client claims when `--session` is not given.
# NOT "widget": the key names a distinct browse cursor in the daemon, and
# sending "widget" from here put every hand-typed `tonearmctl browse` on the
# cursor the bar is rendering from -- a search in a terminal moved the popup.
# server.py's own comment already said the real keys are "widget", "mcp" and
# "cli"; this is the line that makes "cli" true.
DEFAULT_SESSION = "cli"


def to_request(argv: list[str]) -> dict:
    if not argv:
        raise ValueError("usage: tonearmctl <verb> [arg]")
    verb = argv[0]

    if verb == "queue":
        if len(argv) != 2:
            raise ValueError("usage: tonearmctl queue '<request JSON>'")
        try:
            req = json.loads(argv[1])
        except ValueError:
            raise ValueError("invalid queue request") from None
        if not queue_wire.valid_request(req):
            raise ValueError("invalid queue request")
        return req

    if verb == "player":
        if len(argv) != 2:
            raise ValueError("usage: tonearmctl player '<request JSON>'")
        try:
            req = json.loads(argv[1])
        except ValueError:
            raise ValueError("invalid player request") from None
        if not targeted.valid_player_request(req):
            raise ValueError("invalid player request")
        return req

    if verb in BARE_VERBS:
        return {"cmd": verb}

    if verb in INT_VERBS:
        if len(argv) < 2:
            raise ValueError("%s needs a numeric argument" % verb)
        try:
            return {"cmd": verb, "arg": int(argv[1])}
        except ValueError:
            raise ValueError("%s needs a number, got %r" % (verb, argv[1]))

    if verb == "zone":
        if len(argv) < 2:
            raise ValueError("usage: tonearmctl zone pin <id> | zone unpin")
        if argv[1] == "unpin":
            return {"cmd": "zone", "arg": "unpin"}
        if argv[1] == "pin" and len(argv) > 2:
            return {"cmd": "zone", "arg": argv[2]}
        raise ValueError("usage: tonearmctl zone pin <id> | zone unpin")

    if verb == "transfer":
        # The destination stays a STRING. Roon zone ids are opaque and can look
        # numeric, so this must not go through the int() coercion the seek and
        # volume verbs use above.
        if len(argv) < 2:
            raise ValueError("usage: tonearmctl transfer <zone_id>")
        return {"cmd": "transfer", "arg": argv[1]}

    if verb == "browse":
        request = browse_request(argv)
        if request is None:
            raise ValueError(
                "usage: tonearmctl browse [--session <key>] search <term>"
                " | enter <i> <level_id> | activate <i> <level_id>"
                " | play <i> <level_id>"
                " | play_selected <i> <level_id> <zone_id> <request_id>"
                " <play_now|play_next|add_to_queue>"
                " | back | page <offset> | reset")
        return request

    raise ValueError("unknown verb %r" % verb)


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

    tokens = argv[1:]
    session = DEFAULT_SESSION
    # Consumed BEFORE the op, never after: `search` joins everything following
    # its op with spaces, so a flag accepted on the tail would silently become
    # part of the search term instead of the session key.
    if tokens[0] == "--session":
        # An empty key is refused rather than passed on. The daemon reads
        # `payload.pop("session", None) or "widget"`, so "" there falls back to
        # the WIDGET's cursor -- the exact collision this flag exists to stop.
        if len(tokens) < 2 or not tokens[1]:
            return None
        session = tokens[1]
        tokens = tokens[2:]
        if not tokens:
            return None

    op = tokens[0]
    base = {"cmd": "browse", "session": session, "op": op}
    rest = tokens[1:]
    if op == "play_selected":
        # The action token is required. Omitting it must not default to Play
        # Now, and a Core menu title such as "Queue" is not a token.
        if len(rest) != 5:
            return None
        req = dict(base, index=_int(rest[0]), level_id=_int(rest[1]),
                   zone_id=rest[2], request_id=rest[3], action=rest[4])
        return req if browse_play.valid_request(req) else None
    if op == "search":
        # Joined, so an unquoted multi-word term works from a shell.
        return dict(base, term=" ".join(rest))
    if op in ("enter", "enter_nav", "activate", "play"):
        if len(rest) < 2:
            return None
        index, level = _int(rest[0]), _int(rest[1])
        if index is None or level is None:
            return None
        return dict(base, index=index, level_id=level)

    if op in ("page_nav", "back_nav"):
        if len(rest) != 2:
            return None
        offset, level = _int(rest[0]), _int(rest[1])
        if offset is None or offset < 0 or level is None:
            return None
        return dict(base, offset=offset, level_id=level)
    if op == "library" and not rest:
        return base
    if op == "page":
        offset = _int(rest[0]) if rest else None
        if offset is None:
            return None
        return dict(base, offset=offset)
    if op in ("back", "reset"):
        return base
    return None
