"""Bounded opt-in selected browse request/result, independent of legacy ops.

The action field is a fixed token. It is never a Core menu title and never
free text. Legacy browse play/activate stay Play Now and do not use this
envelope.
"""
from .targeted import bounded_id

# Tokens map to measured Core titles in browse.py. Start Radio is absent on
# purpose: this contract does not expose it.
SELECTED_ACTIONS = {
    'play_now': 'Play Now',
    'play_next': 'Add Next',
    'add_to_queue': 'Queue',
}


def valid_action(value):
    return type(value) is str and value in SELECTED_ACTIONS


def valid_request(req):
    return (isinstance(req, dict) and set(req) == {
        'cmd', 'op', 'session', 'index', 'level_id', 'zone_id', 'request_id',
        'action'}
        and req['cmd'] == 'browse' and req['op'] == 'play_selected'
        and bounded_id(req['session'], 64) and bounded_id(req['zone_id'], 128)
        and bounded_id(req['request_id'], 64) and valid_action(req['action'])
        and type(req['index']) is int and 0 <= req['index'] < 100
        and type(req['level_id']) is int and 0 <= req['level_id'] <= 9007199254740991)


def result(req, outcome, error):
    action = req.get('action')
    return dict(v=1, ok=outcome == 'attempted',
                request_id=req.get('request_id') if bounded_id(req.get('request_id'),64) else None,
                zone_id=req.get('zone_id') if bounded_id(req.get('zone_id'),128) else None,
                action=action if valid_action(action) else None,
                outcome=outcome, error=error)


def valid_result(reply, req):
    return (isinstance(reply, dict) and type(reply.get('v')) is int and reply['v'] == 1
            and reply.get('request_id') == req['request_id']
            and reply.get('zone_id') == req['zone_id']
            and reply.get('action') == req['action']
            and ((reply.get('outcome') == 'attempted' and reply.get('error') is None)
                 or (reply.get('outcome') in ('refused','unknown')
                     and bounded_id(reply.get('error'),64))))
