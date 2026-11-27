"""Bounded, read-only DRAFT queue consumer.

Only a paused iFi initial items shape has retained live evidence. Empty,
string IDs, Changed insert/remove and terminal cases below are synthetic
contract assumptions, not protocol acceptance. No raw bodies leave this cache.
"""
from dataclasses import dataclass
import json
import threading

MAX_ITEMS = 100
MAX_TEXT = 256
MAX_CACHE_BYTES = 60 * 1024


@dataclass(frozen=True)
class Context:
    status: str
    api: object
    socket: object
    zone_id: str | None
    generation: int = 0
    required: int = 1

    def same(self, other):
        return (other is not None and self.api is other.api
                and self.socket is other.socket and self.status == other.status
                and self.zone_id == other.zone_id and self.generation == other.generation
                and self.required == other.required)


def _items(raw):
    if not isinstance(raw, list) or len(raw) > MAX_ITEMS:
        raise ValueError('items')
    rows = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError('item')
        key, lines = item.get('queue_item_id'), item.get('three_line')
        if not ((type(key) is int and 0 <= key <= 2**53 - 1)
                or (isinstance(key, str) and 0 < len(key) <= 128
                    and all(33 <= ord(c) <= 126 for c in key))):
            raise ValueError('id')
        if not isinstance(lines, dict):
            raise ValueError('lines')
        title, subtitle = lines.get('line1'), lines.get('line2', '')
        if not isinstance(title, str) or not isinstance(subtitle, str):
            raise ValueError('text')
        rows.append({'id': key, 'title': title[:MAX_TEXT], 'subtitle': subtitle[:MAX_TEXT]})
    return rows


def _apply(name, body, current, ready):
    # Bound the encoded input as well as the normalized cache. Unknown optional
    # metadata cannot bypass the local budget even if the server ignores its cap.
    if not isinstance(body, dict):
        raise ValueError('body')
    pending, nodes = [(body, 0)], 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if nodes > 4096 or depth > 16:
            raise ValueError('structure')
        if isinstance(value, str) and len(value) > MAX_CACHE_BYTES:
            raise ValueError('string')
        if isinstance(value, (dict, list)):
            if len(value) > 4096:
                raise ValueError('structure')
            if isinstance(value, dict):
                pending.extend((key, depth + 1) for key in value)
                value = value.values()
            pending.extend((child, depth + 1) for child in value)
    if len(json.dumps(body, allow_nan=False).encode()) > MAX_CACHE_BYTES:
        raise ValueError('body')
    if name == 'Subscribed':
        rows = _items(body.get('items'))
    elif name == 'Changed' and ready:
        changes = body.get('changes')
        if not isinstance(changes, list) or len(changes) > 2 * MAX_ITEMS:
            raise ValueError('changes')
        rows = list(current)
        for change in changes:
            if not isinstance(change, dict):
                raise ValueError('change')
            index = change.get('index')
            if type(index) is not int or not 0 <= index <= len(rows):
                raise ValueError('index')
            if change.get('operation') == 'insert':
                rows[index:index] = _items(change.get('items'))
            elif change.get('operation') == 'remove':
                count = change.get('count')
                if type(count) is not int or not 1 <= count <= len(rows) - index:
                    raise ValueError('count')
                del rows[index:index + count]
            else:
                raise ValueError('operation')
            if len(rows) > MAX_ITEMS:
                raise ValueError('window')
    else:
        raise ValueError('event')
    if len({(type(row['id']), row['id']) for row in rows}) != len(rows):
        raise ValueError('duplicate')
    if len(json.dumps(rows).encode()) > MAX_CACHE_BYTES - 1024:
        raise ValueError('cache')
    return rows


class QueueCache:
    """observe is cache-only; only watcher drain and stop perform SDK I/O.

    The provider reads current selection without SDK calls. Reading it inside
    our instance lock prevents stale observe/event writers from reverting a
    newer context. changed is always called after releasing that lock.
    """
    def __init__(self, provider, changed):
        self._provider, self._changed = provider, changed
        self._lock = threading.RLock()
        self._wanted = None
        self._token = 0
        self._settled = -1
        self._handle = None
        self._handle_token = -1
        self._busy = False
        self._stopped = False
        self._recovery = False
        self._status, self._error, self._rows = 'unknown', None, []
        self._revision = 0
        self._notified_revision = -1
        self._last_api = self._last_socket = None
        self._last_generation = 0
        self._floor = 1

    def _observe(self):
        if self._stopped:
            return
        ctx = self._provider()
        if ctx.api is not self._last_api:
            self._floor = max(1, ctx.required)
        elif (ctx.socket is not self._last_socket or
              (ctx.status == 'disconnected' and self._wanted
               and self._wanted.status != 'disconnected')):
            self._floor = max(self._floor, self._last_generation + 1, ctx.required)
        self._last_api, self._last_socket = ctx.api, ctx.socket
        self._last_generation = ctx.generation
        if ctx.status == 'available' and ctx.generation < max(self._floor, ctx.required):
            ctx = Context('unknown', ctx.api, ctx.socket, None, ctx.generation, ctx.required)
        if ctx.same(self._wanted):
            return
        self._wanted = ctx
        self._token += 1
        self._recovery = False
        self._status = 'loading' if ctx.status == 'available' else ctx.status
        self._error, self._rows = None, []
        self._revision += 1

    def snapshot(self):
        with self._lock:
            self._observe()
            return {'status': self._status, 'error': self._error,
                    'zone_id': self._wanted.zone_id if self._wanted and not self._stopped else None,
                    'items': [dict(row) for row in self._rows],
                    'revision': self._revision, 'limit': MAX_ITEMS}

    def _fail(self, code, recover):
        self._status, self._error, self._rows = 'error', code, []
        self._revision += 1
        self._token += 1  # reject every subsequent callback from this bind
        if recover and not self._recovery:
            self._recovery = True  # one watcher-driven fresh bind
        else:
            self._settled = self._token  # terminal until context changes

    def _event(self, token, name, body, verb):
        with self._lock:
            self._observe()
            if self._stopped or token != self._token:
                return
            try:
                # MOO COMPLETE ends request ownership regardless of event name.
                # Initial live Subscribed verb remains unmeasured.
                if verb != 'CONTINUE':
                    raise ValueError('terminal')
                rows = _apply(name, body, self._rows, self._status == 'ready')
                self._rows, self._status, self._error = rows, 'ready', None
                self._revision += 1
            except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
                self._fail('invalid_queue_event', True)
        self._notify()

    def _notify(self):
        with self._lock:
            if self._notified_revision == self._revision:
                return
            self._notified_revision = self._revision
        # Re-read the current snapshot at publication; never publish captured
        # rows from an earlier event or hold the queue lock through broadcast.
        self._changed()

    def drain(self):
        with self._lock:
            self._observe()
            # Always detach invalid ownership, even after exhausted recovery.
            if self._busy:
                return
            old, self._handle = self._handle, None
            if self._settled == self._token or self._stopped:
                bind = False
            else:
                bind = self._wanted is not None and self._wanted.status == 'available'
            # Preserve a healthy settled handle.
            if old is not None and self._handle_token == self._token:
                self._handle = old
                return
            self._busy = True
            token, ctx = self._token, self._wanted
            self._settled = token
        new = None
        try:
            if old is not None:
                old.cancel()
            # Cancellation can synchronously close/reconnect, stop, or change
            # selection. Revalidate before sending the captured replacement.
            with self._lock:
                self._observe()
                bind = bind and not self._stopped and token == self._token
            if bind:
                new = ctx.api.subscribe_queue(
                    lambda name, body, verb: self._event(token, name, body, verb),
                    ctx.zone_id, MAX_ITEMS)
                if new is None:
                    raise ValueError('missing_handle')
                with self._lock:
                    self._observe()
                    if not self._stopped and token == self._token:
                        self._handle, new = new, None
                        self._handle_token = token
        except Exception:
            with self._lock:
                if token == self._token:
                    self._fail('subscription_failed', False)
        finally:
            try:
                if new is not None:
                    try:
                        new.cancel()
                    except Exception:
                        # Owned cancellation already invalidates its local
                        # route. Do not leak exceptions or restore stale rows.
                        with self._lock:
                            if not self._stopped and token == self._token:
                                self._fail('subscription_failed', False)
            finally:
                with self._lock:
                    self._busy = False
        self._notify()

    def stop(self):
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._token += 1
            self._status, self._error, self._rows = 'disconnected', None, []
            self._revision += 1
            handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.cancel()
            except Exception:
                pass  # state is invalidated; the owner's API stop must proceed
