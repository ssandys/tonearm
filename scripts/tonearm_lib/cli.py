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

    raise ValueError("unknown verb %r" % verb)
