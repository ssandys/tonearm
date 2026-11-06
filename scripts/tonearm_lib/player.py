"""Normal-player capability facts, and the ONE player request/result contract.

Pure: no I/O, no Core, no socket, no clock.

`controls(raw)` derives what a zone's RAW Core payload supports, and nothing
else: a malformed output or volume shape yields no fact rather than a
default. state.py publishes the facts as `zone.controls`; the daemon
re-derives them at dispatch (core.RoonSession.player_targeted) and refuses a
control the zone does not have. Facts are presentation, not authority.

`valid_request`, `result` and `valid_result` are the single vocabulary for
the `player_targeted` envelope, shared by the daemon and its clients.

RESPONSE VOCABULARY
-------------------
`outcome` is one of:

  attempted  The SDK call was made and returned a non-None answer. That is
             not proof the Core accepted the command, and not proof anything
             was audible: the vendored API answers None when the Core does
             not reply inside its own budget, and None after it swallows a
             send failure.
  unknown    The effect cannot be known. Never retried, never compensated.
  refused    Nothing was sent; the daemon declined before touching the SDK.

`error` is present on every reply and is null exactly when `outcome` is
"attempted". Otherwise it is one of:

  refused:  bad_request             the envelope or its arguments are inadmissible
            disconnected            no live Core connection
            zones_unknown           the zone listing is not authoritative yet
            unavailable             the captured zone is gone from the fresh list
            selection_changed       the captured zone is no longer the selected one
            capability_unavailable  the zone does not support this control
            out_of_range            the argument is outside the zone's own bounds
            output_changed          the captured output set no longer matches the zone
            unsupported             this daemon does not speak the player protocol
  unknown:  dispatch_failed         the SDK call raised, or did not prove an attempt
            internal_error          the daemon failed while handling the request
"""

from __future__ import annotations

import math

# The transport verbs Roon's playback_control takes, and the three other
# controls the bar exposes.
TRANSPORT = ("play", "pause", "playpause", "next", "previous")
VERBS = TRANSPORT + ("seek", "volume", "mute", "unmute")

# Bounds on the envelope: the request id is the client's correlation handle,
# zone and output ids are Roon's own (UUID-shaped, around 36 characters), and
# a group mute names one output per member of a zone.
MAX_REQUEST_ID = 64
MAX_ID = 128
MAX_OUTPUTS = 32

# The envelope's fields, exactly. Anything else -- an extra key, or a missing
# one -- is refused before the request reaches the session.
REQUEST_FIELDS = frozenset(
    {"cmd", "v", "request_id", "zone_id", "verb", "arg", "output_id", "output_ids"})

# The reply's fields, exactly. A reply that omits one is not a result: a
# client cannot tell "no error" from "the field was never sent", and the
# widget's own reader requires an explicit null.
RESULT_FIELDS = frozenset(
    {"v", "type", "request_id", "zone_id", "verb", "outcome", "error"})

CMD = "player_targeted"
VERSION = 1

OUTCOMES = ("attempted", "unknown", "refused")

REFUSAL_ERRORS = (
    "bad_request",
    "disconnected",
    "zones_unknown",
    "unavailable",
    "selection_changed",
    "capability_unavailable",
    "out_of_range",
    "output_changed",
    "unsupported",
)

UNKNOWN_ERRORS = ("dispatch_failed", "internal_error")


def number(value) -> bool:
    """True when `value` is a real, finite number.

    bool is refused before int because `isinstance(True, int)` is true, and a
    volume of True is not a volume. Non-finite floats survive json.loads.
    """
    if type(value) is int:
        return True
    if type(value) is float:
        return math.isfinite(value)
    return False


def identity(value) -> bool:
    """True for a non-empty identifier string inside the bound."""
    return isinstance(value, str) and 0 < len(value) <= MAX_ID


def bounded_id(value, limit) -> bool:
    """True for a non-empty string inside `limit`."""
    return isinstance(value, str) and 0 < len(value) <= limit


def seek_length(raw):
    """The track length a seek may address, or None when there is none.

    A missing, non-numeric or non-finite length is no length, and a seek
    against it has no bound to check against.
    """
    raw = raw if isinstance(raw, dict) else {}
    now_playing = raw.get("now_playing")
    if not isinstance(now_playing, dict):
        return None
    length = now_playing.get("length")
    if not number(length):
        return None
    return length


def volume_range(raw):
    """(min, max) of the first output's absolute volume scale, or None.

    None means there is no absolute scale to write to: no outputs, a
    malformed output, a fixed or incremental volume, or bounds that are not a
    real range. The FIRST output only: the bar's slider is that output's
    scale, and one output's absolute value means nothing on another's.
    """
    raw = raw if isinstance(raw, dict) else {}
    outputs = raw.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        return None
    first = outputs[0]
    if not isinstance(first, dict):
        return None
    volume = first.get("volume")
    if not isinstance(volume, dict):
        return None
    if volume.get("type") in ("fixed", "incremental"):
        return None
    low, high = volume.get("min"), volume.get("max")
    if not number(low) or not number(high) or low >= high:
        return None
    return low, high


def controls(raw) -> dict:
    """What this zone's RAW payload actually supports.

    A missing fact is False / None / [], never a fabricated value, so a
    widget rendering from this cannot offer a control the Core lacks.

    `transport` is deliberately weak: it says the zone has a body to
    command, not that a command may be sent. The SELECTION and the
    connection decide that, at dispatch.

    `mute_output_ids` is the complete, unique list of output ids the zone
    advertises, and only when every one carries usable mute metadata: a
    partial list would mute some speakers and leave the rest audible.
    """
    raw = raw if isinstance(raw, dict) else {}
    result = {
        "transport": bool(raw),
        "seek": False,
        "volume": False,
        "volume_output_id": None,
        "mute_output_ids": [],
        "muted": False,
    }
    length = seek_length(raw)
    result["seek"] = (raw.get("is_seek_allowed") is True
                      and length is not None and length > 0)

    outputs = raw.get("outputs")
    if not isinstance(outputs, list) or not 0 < len(outputs) <= MAX_OUTPUTS:
        return result
    first = outputs[0]
    if not isinstance(first, dict):
        return result

    volume = first.get("volume")
    if (identity(first.get("output_id"))
            and isinstance(volume, dict)
            and volume.get("type") not in ("fixed", "incremental")
            and all(number(volume.get(key)) for key in ("value", "min", "max"))
            and volume["min"] < volume["max"]
            and volume["min"] <= volume["value"] <= volume["max"]):
        result["volume"] = True
        result["volume_output_id"] = first["output_id"]

    ids = []
    for output in outputs:
        if not isinstance(output, dict) or not identity(output.get("output_id")):
            return result
        volume = output.get("volume")
        if (not isinstance(volume, dict)
                or volume.get("type") == "fixed"
                or type(volume.get("is_muted")) is not bool):
            return result
        ids.append(output["output_id"])
    if len(set(ids)) == len(ids):
        result["mute_output_ids"] = ids
        result["muted"] = all(output["volume"]["is_muted"] for output in outputs)
    return result


def valid_arguments(verb, arg, output_id, output_ids) -> bool:
    """Whether these arguments belong to this verb, exactly.

      transport  no argument, no output
      seek       an integer position, no output
      volume     an integer value AND the output it applies to, no group
      mute       no argument AND the complete output list, no single output

    `type(arg) is int` rather than isinstance: bool is an int in Python, and
    a volume of True is not a volume. A float is refused the same way.
    """
    if verb not in VERBS:
        return False
    if verb in ("seek", "volume"):
        if type(arg) is not int:
            return False
    elif arg is not None:
        return False
    if verb == "volume":
        return identity(output_id) and output_ids is None
    if output_id is not None:
        return False
    if verb in ("mute", "unmute"):
        return (isinstance(output_ids, list)
                and 0 < len(output_ids) <= MAX_OUTPUTS
                and all(identity(x) for x in output_ids)
                and len(set(output_ids)) == len(output_ids))
    return output_ids is None


def valid_request(request) -> bool:
    """Whether this is an admissible `player_targeted` request.

    The key set is EXACT, the version is an integer 1, the identities are
    bounded strings, and the arguments belong to the verb.
    """
    if not isinstance(request, dict) or set(request) != REQUEST_FIELDS:
        return False
    if request["cmd"] != CMD:
        return False
    if type(request["v"]) is not int or request["v"] != VERSION:
        return False
    if not bounded_id(request["request_id"], MAX_REQUEST_ID):
        return False
    if not bounded_id(request["zone_id"], MAX_ID):
        return False
    if not isinstance(request["verb"], str):
        return False
    return valid_arguments(request["verb"], request["arg"],
                            request["output_id"], request["output_ids"])


def result(request, outcome, error=None) -> dict:
    """Build the reply for `request`.

    Identities are echoed only when bounded, so a malformed request is
    answered without reflecting an unbounded value back onto the wire. The
    verb is echoed even when the daemon does not implement it, so a refusal
    still correlates with the request that caused it.
    """
    request = request if isinstance(request, dict) else {}
    request_id = request.get("request_id")
    zone_id = request.get("zone_id")
    verb = request.get("verb")
    return {
        "v": VERSION,
        "type": "command_result",
        "request_id": request_id if bounded_id(request_id, MAX_REQUEST_ID) else None,
        "zone_id": zone_id if bounded_id(zone_id, MAX_ID) else None,
        "verb": verb if bounded_id(verb, MAX_ID) else None,
        "outcome": outcome,
        "error": error,
    }


def valid_result(reply, request) -> bool:
    """Whether `reply` is a correlated, well-formed answer to `request`.

    Every field must be PRESENT and correct. The key set is exact, so an
    absent `error` is rejected rather than read as "no error" -- and the
    outcome/error pairing is checked, so a "refused" with no reason is not a
    result either. Correlation is by request id, zone id AND verb.
    """
    if not isinstance(reply, dict) or not isinstance(request, dict):
        return False
    if set(reply) != RESULT_FIELDS:
        return False
    if (type(reply["v"]) is not int or reply["v"] != VERSION
            or reply["type"] != "command_result"
            or reply["request_id"] != request.get("request_id")
            or reply["zone_id"] != request.get("zone_id")
            or reply["verb"] != request.get("verb")):
        return False
    outcome, error = reply["outcome"], reply["error"]
    if outcome not in OUTCOMES:
        return False
    if outcome == "attempted":
        return error is None
    if outcome == "unknown":
        return error in UNKNOWN_ERRORS
    return error in REFUSAL_ERRORS
