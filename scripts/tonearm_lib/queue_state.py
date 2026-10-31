"""One bounded read-only queue cache; candidate body schema awaits live validation.

observe/event/read are cache-only. drain performs SDK I/O on the existing daemon
watcher, never on a UI request or inside the global publish/server lock.
"""
from dataclasses import dataclass, replace
import json
import threading

MAX_ITEMS = 100
MAX_CACHE_BYTES = 60 * 1024  # leaves room for bounded request/zone envelope
MAX_TEXT = 256


@dataclass(frozen=True)
class Context:
    status: str
    api: object
    socket: object
    zone_id: str | None
    zone_generation: int = 0

    def same(self, other):
        return (other is not None and self.status == other.status and self.api is other.api
                and self.socket is other.socket and self.zone_id == other.zone_id)


def _item(raw):
    if not isinstance(raw, dict):
        raise ValueError('invalid_item')
    key, lines = raw.get('queue_item_id'), raw.get('three_line')
    if not ((type(key) is int and 0 <= key <= 9007199254740991)
            or (isinstance(key, str) and 0 < len(key) <= 128)):
        raise ValueError('invalid_item')
    if not isinstance(lines, dict) or not isinstance(lines.get('line1'), str):
        raise ValueError('invalid_item')
    subtitle = lines.get('line2') or ''
    if not isinstance(subtitle, str):
        raise ValueError('invalid_item')
    return {'id': key, 'title': lines['line1'][:MAX_TEXT], 'subtitle': subtitle[:MAX_TEXT]}


def _items(raw):
    if not isinstance(raw, list) or len(raw) > MAX_ITEMS:
        raise ValueError('window_limit')
    return [_item(row) for row in raw]


class QueueCache:
    def __init__(self, context_provider):
        self._context = context_provider
        self._lock = threading.RLock()
        self._wanted = None
        self._generation = 0
        self._settled = -1
        self._handle = None
        self._busy = False
        self._stopped = False
        self._status = 'unknown'
        self._rows = []
        self._error = None
        self._revision = 0
        self._last_api = None
        self._last_socket = None
        self._last_zone_generation = 0
        self._zone_floor = 1
        self._schema = {}

    def _effective(self, ctx):
        if ctx.status != 'available':
            return ctx
        floor = self._zone_floor if ctx.api is self._last_api else 1
        if ctx.api is self._last_api and ctx.socket is not self._last_socket:
            floor = max(floor, self._last_zone_generation + 1)
        return replace(ctx, status='unknown') if ctx.zone_generation < floor else ctx

    def observe(self, ctx):
        with self._lock:
            if self._stopped:
                return
            if ctx.api is not self._last_api:
                self._zone_floor = 1
            elif (ctx.socket is not self._last_socket or
                  (ctx.status == 'disconnected' and self._wanted and self._wanted.status == 'available')):
                self._zone_floor = max(self._zone_floor, self._last_zone_generation + 1)
            self._last_api, self._last_socket = ctx.api, ctx.socket
            self._last_zone_generation = ctx.zone_generation
            ctx = self._effective(ctx)
            if ctx.same(self._wanted):
                return
            self._wanted = ctx
            self._generation += 1  # invalidate before any cancellation I/O
            self._rows = []
            self._error = None
            self._schema = {}
            self._status = 'loading' if ctx.status == 'available' else ctx.status
            self._revision += 1

    def drain(self):
        with self._lock:
            if self._stopped or self._busy or self._settled == self._generation:
                return
            self._busy = True
            token, ctx = self._generation, self._wanted
            handle, self._handle = self._handle, None
            self._settled = token  # failure is not an automatic retry loop
        new_handle = None
        try:
            if handle is not None:
                handle.cancel()
            if ctx is None or ctx.status != 'available':
                return
            new_handle = ctx.api.subscribe_queue(
                lambda name, body: self._event(token, ctx, name, body), ctx.zone_id, MAX_ITEMS)
            live = self._context()
            with self._lock:
                accept = (not self._stopped and token == self._generation
                          and ctx.same(self._wanted) and ctx.same(self._effective(live)))
                if accept:
                    self._handle = new_handle
                    new_handle = None
        except Exception:
            with self._lock:
                if token == self._generation:
                    self._fail('subscription_failed')
        finally:
            try:
                if new_handle is not None:
                    new_handle.cancel()
            finally:
                with self._lock:
                    self._busy = False

    def _fail(self, code):
        self._status, self._error, self._rows = 'error', code, []
        self._revision += 1

    def _event(self, token, ctx, name, body):
        try:
            live = self._context()  # no queue lock while reading Core context
        except Exception:
            live = None
        with self._lock:
            if self._stopped or token != self._generation or not ctx.same(self._wanted):
                return
            if live is None or not ctx.same(self._effective(live)):
                return
            # Field-name/type summary for an approved schema probe; never values.
            keys = sorted(k for k in body if isinstance(k, str) and k.isascii()
                          and k.isidentifier() and len(k) <= 32)[:16] if isinstance(body, dict) else []
            self._schema = {'event': name[:64] if isinstance(name, str) else 'invalid',
                            'fields': {k: type(body[k]).__name__ for k in keys}}
            try:
                if name == 'Subscribed':
                    rows = _items(body.get('items')) if isinstance(body, dict) else _items(None)
                elif name == 'Changed':
                    if self._status != 'ready':
                        raise ValueError('snapshot_required')
                    changes = body.get('changes') if isinstance(body, dict) else None
                    if not isinstance(changes, list) or len(changes) > 2 * MAX_ITEMS:
                        raise ValueError('invalid_changes')
                    rows = list(self._rows)
                    for change in changes:
                        if not isinstance(change, dict):
                            raise ValueError('invalid_change')
                        index = change.get('index')
                        if type(index) is not int or not 0 <= index <= len(rows):
                            raise ValueError('invalid_index')
                        op = change.get('operation')
                        if op == 'insert':
                            rows[index:index] = _items(change.get('items'))
                        elif op == 'remove':
                            count = change.get('count')
                            if type(count) is not int or count <= 0 or index + count > len(rows):
                                raise ValueError('invalid_count')
                            del rows[index:index+count]
                        else:
                            raise ValueError('unsupported_operation')
                        if len(rows) > MAX_ITEMS:
                            raise ValueError('window_limit')
                else:
                    raise ValueError('subscription_ended' if name == 'Unsubscribed' else 'unsupported_response')
                if len({(type(row['id']), row['id']) for row in rows}) != len(rows):
                    raise ValueError('duplicate_item')
                payload = {'status': 'ready', 'items': rows, 'schema': self._schema}
                if len(json.dumps(payload).encode()) > MAX_CACHE_BYTES:
                    raise ValueError('payload_limit')
                self._rows, self._status, self._error = rows, 'ready', None
                self._revision += 1
            except (ValueError, TypeError, KeyError):
                # Stable codes only, never log/reflect Core titles or raw bodies.
                self._fail('invalid_queue_event')

    def snapshot(self, live, zone_id):
        with self._lock:
            ctx = self._effective(live)
            status, error, rows = self._status, self._error, self._rows
            if self._stopped:
                status, error, rows = 'disconnected', None, []
            elif ctx.zone_id != zone_id:
                status, error, rows = 'unavailable', 'selection_changed', []
            elif ctx.status != 'available':
                status, error, rows = ctx.status, None, []
            elif not ctx.same(self._wanted):
                status, error, rows = 'loading', None, []
            return {'status': status, 'error': error, 'items': [dict(row) for row in rows],
                    'revision': self._revision, 'limit': MAX_ITEMS,
                    'schema': json.loads(json.dumps(self._schema)) if ctx.same(self._wanted) and not self._stopped and ctx.zone_id == zone_id else {}}

    def stop(self):
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._generation += 1
            self._status, self._rows = 'disconnected', []
            handle, self._handle = self._handle, None
        if handle is not None:
            handle.cancel()
