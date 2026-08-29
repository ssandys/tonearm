"""Argument parsing for tonearmctl. Pure, so it is unit-testable."""

from __future__ import annotations

BARE_VERBS = ("playpause", "play", "pause", "next", "previous",
              "mute", "unmute", "subscribe", "status")
INT_VERBS = ("seek", "volume")


def to_request(argv: list[str]) -> dict:
    if not argv:
        raise ValueError("usage: tonearmctl <verb> [arg]")
    verb = argv[0]

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
                "usage: tonearmctl browse search <term> | enter <i> <level_id>"
                " | activate <i> <level_id> | play <i> <level_id>"
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
    op = argv[1]
    base = {"cmd": "browse", "session": "widget", "op": op}
    rest = argv[2:]
    if op == "search":
        # Joined, so an unquoted multi-word term works from a shell.
        return dict(base, term=" ".join(rest))
    if op in ("enter", "activate", "play"):
        if len(rest) < 2:
            return None
        index, level = _int(rest[0]), _int(rest[1])
        if index is None or level is None:
            return None
        return dict(base, index=index, level_id=level)

    if op == "page":
        offset = _int(rest[0]) if rest else None
        if offset is None:
            return None
        return dict(base, offset=offset)
    if op in ("back", "reset"):
        return base
    return None
