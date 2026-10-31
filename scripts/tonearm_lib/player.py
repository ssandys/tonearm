"""Pure normal-player capability facts; presentation defaults grant nothing."""
import math

TRANSPORT = ('play', 'pause', 'playpause', 'next', 'previous')
VERBS = TRANSPORT + ('seek', 'volume', 'mute', 'unmute')
MAX_OUTPUTS = 32


def number(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


def identity(value):
    return isinstance(value, str) and 0 < len(value) <= 128


def valid_arguments(verb, arg, output_id, output_ids):
    if verb not in VERBS:
        return False
    if verb in ('seek', 'volume'):
        if type(arg) is not int:
            return False
    elif arg is not None:
        return False
    if verb == 'volume':
        return identity(output_id) and output_ids is None
    if output_id is not None:
        return False
    if verb in ('mute', 'unmute'):
        return (isinstance(output_ids, list) and 0 < len(output_ids) <= MAX_OUTPUTS
                and all(identity(x) for x in output_ids)
                and len(set(output_ids)) == len(output_ids))
    return output_ids is None


def controls(raw):
    raw = raw if isinstance(raw, dict) else {}
    np = raw.get('now_playing')
    length = np.get('length') if isinstance(np, dict) else None
    result = dict(transport=bool(raw), seek=(raw.get('is_seek_allowed') is True
                  and number(length) and length > 0), volume=False,
                  volume_output_id=None, mute_output_ids=[], muted=False)
    outputs = raw.get('outputs')
    if not isinstance(outputs, list) or not 0 < len(outputs) <= MAX_OUTPUTS:
        return result
    first = outputs[0]
    if not isinstance(first, dict):
        return result
    vol = first.get('volume')
    if (identity(first.get('output_id')) and isinstance(vol, dict)
            and vol.get('type') not in ('fixed', 'incremental')
            and all(number(vol.get(k)) for k in ('value', 'min', 'max'))
            and vol['min'] < vol['max'] and vol['min'] <= vol['value'] <= vol['max']):
        result['volume'] = True
        result['volume_output_id'] = first['output_id']
    ids = []
    for output in outputs:
        if not isinstance(output, dict) or not identity(output.get('output_id')):
            return result
        v = output.get('volume')
        if (not isinstance(v, dict) or v.get('type') == 'fixed'
                or type(v.get('is_muted')) is not bool):
            return result
        ids.append(output['output_id'])
    if len(set(ids)) == len(ids):
        result['mute_output_ids'] = ids
        result['muted'] = all(o['volume']['is_muted'] for o in outputs)
    return result
