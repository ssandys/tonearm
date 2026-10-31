"""Bounded cache-only queue query. No action addresses or mutations."""
import json

MAX_REPLY_BYTES = 64 * 1024
STATUSES = ('unknown', 'loading', 'ready', 'unavailable', 'disconnected', 'error')


def _id(value, limit):
    return isinstance(value, str) and 0 < len(value) <= limit


def valid_request(req):
    return (isinstance(req, dict) and set(req) == {'cmd','v','request_id','zone_id'}
            and req['cmd'] == 'queue_status' and type(req['v']) is int and req['v'] == 1
            and _id(req['request_id'],64) and _id(req['zone_id'],128))


def result(req, status, error=None):
    return {'type':'queue_state','v':1,'request_id':req.get('request_id') if _id(req.get('request_id'),64) else None,
            'zone_id':req.get('zone_id') if _id(req.get('zone_id'),128) else None,
            'status':status,'error':error,'items':[],'revision':0,'limit':100,'schema':{}}


def valid_result(reply, req):
    if not isinstance(reply,dict) or set(reply) != set(result(req,'unknown')):
        return False
    if (reply['type'] != 'queue_state' or type(reply['v']) is not int or reply['v'] != 1
            or reply['request_id'] != req['request_id'] or reply['zone_id'] != req['zone_id']
            or reply['status'] not in STATUSES or type(reply['revision']) is not int or reply['revision'] < 0
            or type(reply['limit']) is not int or reply['limit'] != 100
            or reply['error'] not in (None,'selection_changed','subscription_failed','invalid_queue_event',
                                      'bad_request','unsupported','internal_error','payload_limit')):
        return False
    rows=reply['items']
    if not isinstance(rows,list) or len(rows)>100 or (reply['status'] != 'ready' and rows):
        return False
    for row in rows:
        if (not isinstance(row,dict) or set(row) != {'id','title','subtitle'}
                or not ((type(row['id']) is int and 0 <= row['id'] <= 9007199254740991) or _id(row['id'],128))
                or not all(isinstance(row[k],str) and len(row[k])<=256 for k in ('title','subtitle'))):
            return False
    if len({(type(row['id']),row['id']) for row in rows}) != len(rows):
        return False
    schema=reply['schema']
    if not isinstance(schema,dict) or (schema and (set(schema) != {'event','fields'}
            or not isinstance(schema['event'],str) or len(schema['event'])>64
            or not isinstance(schema['fields'],dict) or len(schema['fields'])>16
            or not all(isinstance(k,str) and k.isascii() and k.isidentifier() and len(k)<=32
                       and v in ('dict','list','str','int','float','bool','NoneType') for k,v in schema['fields'].items()))):
        return False
    try:
        return len(json.dumps(reply).encode()) + 1 <= MAX_REPLY_BYTES
    except (TypeError,ValueError):
        return False
