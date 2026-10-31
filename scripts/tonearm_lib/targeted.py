"""Opt-in pause-only envelope. Legacy commands retain their existing contract."""

MAX_REQUEST_ID = 64
MAX_ZONE_ID = 128


def bounded_id(value, limit):
    return isinstance(value, str) and 0 < len(value) <= limit


def valid_request(request):
    return (isinstance(request, dict)
            and set(request) == {"cmd", "v", "request_id", "zone_id"}
            and request["cmd"] == "pause_targeted"
            and type(request["v"]) is int and request["v"] == 1
            and bounded_id(request["request_id"], MAX_REQUEST_ID)
            and bounded_id(request["zone_id"], MAX_ZONE_ID))


def result(request, outcome, error=None):
    # Malformed/unbounded identity is not reflected back into replies/logs.
    rid, zid = request.get("request_id"), request.get("zone_id")
    return {"v": 1, "type": "command_result",
            "request_id": rid if bounded_id(rid, MAX_REQUEST_ID) else None,
            "zone_id": zid if bounded_id(zid, MAX_ZONE_ID) else None,
            "outcome": outcome, "error": error}


def valid_result(reply, request):
    if not isinstance(reply, dict):
        return False
    if (type(reply.get("v")) is not int or reply["v"] != 1
            or reply.get("type") != "command_result"
            or reply.get("request_id") != request["request_id"]
            or reply.get("zone_id") != request["zone_id"]):
        return False
    outcome, error = reply.get("outcome"), reply.get("error")
    return ((outcome == "attempted" and error is None)
            or (outcome == "unknown" and error in ("dispatch_failed", "internal_error"))
            or (outcome == "refused" and error in (
                "bad_request", "disconnected", "zones_unknown", "unavailable",
                "selection_changed", "unsupported")))


def valid_player_request(request):
    from . import player
    return (isinstance(request, dict)
            and set(request) == {'cmd','v','request_id','zone_id','verb','arg','output_id','output_ids'}
            and request['cmd'] == 'player_targeted'
            and type(request['v']) is int and request['v'] == 1
            and bounded_id(request['request_id'], MAX_REQUEST_ID)
            and bounded_id(request['zone_id'], MAX_ZONE_ID)
            and isinstance(request['verb'], str)
            and player.valid_arguments(request['verb'],request['arg'],request['output_id'],request['output_ids']))


def player_result(request, outcome, error=None):
    from . import player
    reply = result(request, outcome, error)
    reply['verb'] = request.get('verb') if request.get('verb') in player.VERBS else None
    return reply


def valid_player_result(reply, request):
    if not isinstance(reply, dict) or reply.get('verb') != request['verb']:
        return False
    if reply.get('outcome') == 'refused' and reply.get('error') in (
            'capability_unavailable','out_of_range','output_changed'):
        return (type(reply.get('v')) is int and reply['v'] == 1
                and reply.get('type') == 'command_result'
                and reply.get('request_id') == request['request_id']
                and reply.get('zone_id') == request['zone_id'])
    return valid_result(reply, request)
