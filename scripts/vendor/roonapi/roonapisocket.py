from __future__ import unicode_literals

import threading

import websocket

from .constants import LOGGER, REGISTERED, SERVICE_PING, CONTROL_VOLUME, SERVICE_TRANSPORT

try:
    import simplejson as json
except ImportError:
    import json

try:
    import thread
except ImportError:
    import _thread as thread


# LOCAL PATCH (tonearm queue): short allocation/registration lock, never SDK I/O.
# Shared across instances so __new__ test doubles use the same safe primitive.
_REQUEST_LOCK = threading.RLock()


class QueueSubscription:
    """Cancellation belongs to one socket and exact entry, not an endpoint."""
    def __init__(self, socket, request_id):
        self._socket = socket
        self._request_id = request_id
        self._closed = False

    @property
    def active(self):
        with _REQUEST_LOCK:
            entry = self._socket._subscriptions.get(self._request_id)
            return not self._closed and entry is not None and entry.get("handle") is self

    def cancel(self):
        socket = self._socket
        with _REQUEST_LOCK:
            if not self.active:
                self._closed = True
                return
            self._closed = True
            entry = socket._subscriptions.pop(self._request_id)
        socket._cancel_entry(entry)


class RoonApiWebSocket(
    threading.Thread
):  # pylint: disable=too-many-instance-attributes
    """Class to handle the roon websocket connection."""

    @property
    def results(self):
        """Return the result of the previous request."""
        return self._results

    def register_connected_callback(self, callback):
        """To be called on connection."""
        self._connected_callback = callback

    def register_registered_calback(self, callback):
        """To be called on registration."""
        self._registered_calback = callback

    def register_source_controls_callback(self, callback):
        """To be called on source changes."""
        self._source_controls_callback = callback

    def register_volume_controls_callback(self, callback):
        """To be called on volume changes."""
        self._volume_controls_callback = callback

    def run(self):
        """Start the socket thread."""
        self._socket.run_forever(ping_interval=10)
        if not self._exit:
            LOGGER.warning("Session unexpectedly disconnected!")
            self._exit = True
            self.failed_state = True
        else:
            LOGGER.debug("socket connection closed")

    def stop(self):
        """Stop the socket thread."""
        self._exit = True
        with _REQUEST_LOCK:
            entries = list(self._subscriptions.values())
            self._subscriptions.clear()
            for entry in entries:
                if entry.get("handle") is not None:
                    entry["handle"]._closed = True
        try:
            for entry in entries:
                try:
                    self._cancel_entry(entry)
                except Exception:
                    LOGGER.warning("subscription cancellation failed during stop")
        finally:
            self._socket.close()

    def __init__(self, host):
        """Create the websocket connection to the roon server."""

        self._socket = None
        self._results = {}
        self._requestid = 10  # initial request_id of 10 to prevent confusion with the requests that are sent by the server at initialization
        self._subkey = 0
        self._exit = False
        self._subscriptions = {}
        self.connected = False
        self.failed_state = False

        self._connected_callback = lambda: None
        self._registered_calback = lambda _: None
        self._source_controls_callback = lambda _a, _b, _c: None
        self._volume_controls_callback = lambda _a, _b, _c: None

        self._socket = websocket.WebSocketApp(
            host,
            on_message=self.on_message,
            on_error=self.on_error,
            on_open=self.on_open,
            on_close=self.on_close,
        )
        threading.Thread.__init__(self)
        self.daemon = True

    def subscribe(self, service, endpoint, callback, opt_data=None):
        """Legacy callback/body-only/None-return contract is preserved."""
        self._subscribe_record(service, endpoint, callback, opt_data, False)

    def subscribe_owned_queue(self, callback, zone_id, max_item_count):
        handle = self._subscribe_record(
            SERVICE_TRANSPORT, "queue", callback,
            {"zone_or_output_id": zone_id, "max_item_count": max_item_count}, True)
        if handle is None:
            raise RuntimeError("queue socket unavailable")
        return handle

    def _subscribe_record(self, service, endpoint, callback, opt_data, owned):
        with _REQUEST_LOCK:
            if not self.connected:
                return None
            subkey = self._subkey
            self._subkey += 1
            request_id = self._requestid
            self._requestid += 1
            data = {"subscription_key": subkey}
            if opt_data and isinstance(opt_data, dict):
                data.update(opt_data)
            handle = QueueSubscription(self, request_id) if owned else None
            entry = {"service": service, "endpoint": endpoint, "request_id": request_id,
                     "subkey": subkey, "callback": callback, "handle": handle}
            # Routing must exist before a synchronous/very fast response.
            self._subscriptions[request_id] = entry
        try:
            self._send_allocated(service + "/subscribe_" + endpoint, request_id, data)
        except Exception:
            with _REQUEST_LOCK:
                if self._subscriptions.get(request_id) is entry:
                    del self._subscriptions[request_id]
                if handle is not None:
                    handle._closed = True
            raise
        return handle

    def _cancel_entry(self, entry):
        if not self.connected:
            return
        rid = self.send_request(entry["service"] + "/unsubscribe_" + entry["endpoint"],
                                {"subscription_key": entry["subkey"]})
        # No waiter owns this cancellation receipt; late/unknown replies drop.
        with _REQUEST_LOCK:
            if type(rid) is int:
                self._results.pop(rid, None)

    def unsubscribe(self, service, endpoint):
        with _REQUEST_LOCK:
            matches = [(k,v) for k,v in self._subscriptions.items()
                       if v["service"] == service and v["endpoint"] == endpoint]
            for key, entry in matches:
                del self._subscriptions[key]
                if entry.get("handle") is not None:
                    entry["handle"]._closed = True
        for _, entry in matches:
            self._cancel_entry(entry)

    # pylint: disable=too-many-branches
    def on_message(self, w_socket, message=None):
        """Handle message callback."""
        if not message:
            message = w_socket  # compatability fix because of change in websocket-client v0.49
        request_id = None
        owned = False
        try:
            message = message.decode("utf-8")
            lines = message.split("\n")
            header = lines[0]
            body = ""

            request_id = None
            line_with_request_id = [
                line for line in lines if line.startswith("Request-Id")
            ]
            if line_with_request_id:
                request_id = int(line_with_request_id[0].split("Request-Id: ")[1])

            if "Content-Type:" in message:
                # Roon uses a blank line after the header to indicate body.
                # See https://github.com/RoonLabs/node-roon-api/blob/master/moomsg.js#L45
                body = "".join(message.split("\n\n")[1:])
            elif "Logging:" not in message:
                body = header
            if body and "{" in body:
                body = json.loads(body)
            # handle message
            if SERVICE_PING in header:
                # reply to incoming ping from server
                self.send_complete(request_id, "Success")
            elif REGISTERED in header:
                self._registered_calback(body)
            elif CONTROL_VOLUME in header:
                # incoming message for volume_control endpoint
                event = header.split("/")[-1]
                LOGGER.debug("CONTROL_VOLUME endpoint %s", event)
                if self._volume_controls_callback:
                    self._volume_controls_callback(event, request_id, body)
            elif request_id in self._subscriptions:
                with _REQUEST_LOCK:
                    entry = self._subscriptions.get(request_id)
                    handle = entry.get("handle") if entry else None
                    owned = handle is not None
                    if handle is not None and not handle.active:
                        entry = None
                    if entry and handle is not None and header.startswith("MOO/1 COMPLETE "):
                        self._subscriptions.pop(request_id, None)
                        handle._closed = True
                if entry:
                    if handle is not None:
                        entry["callback"](header.split(" ", 2)[-1], body)
                    else:
                        entry["callback"](body)
            else:
                # LOCAL PATCH: only pending ordinary requests own a result.
                with _REQUEST_LOCK:
                    if request_id in self._results:
                        self._results[request_id] = body
        except websocket.WebSocketConnectionClosedException:
            # This can happen while closing a connection - so ignore
            pass

        except Exception:  # pylint: disable=broad-except
            with _REQUEST_LOCK:
                owned = owned or self._subscriptions.get(request_id, {}).get("handle") is not None
            if owned:
                LOGGER.exception("Error while parsing queue message")
            else:
                LOGGER.exception("Error while parsing message '%s'", message)

    def on_error(self, w_socket, error=None):
        """Handle error callback."""
        if not error:
            error = w_socket  # compatability fix because of change in websocket-client v0.49
        LOGGER.info("on_error %s", error)

    # pylint: disable=unused-argument
    def on_close(self, w_socket, close_status_code, close_msg):
        """Handle closing the session."""
        LOGGER.debug("session closed (%s) %s", close_msg, close_status_code)
        with _REQUEST_LOCK:
            self.connected = False
            self._requestid = 10
            self._subkey = 0
            self._subscriptions = {}

    # pylint: disable=unused-argument
    def on_open(self, w_socket=None):
        """Handle opening the session."""
        LOGGER.debug("Opened Websocket connection to the server...")
        self.connected = True
        thread.start_new_thread(self._connected_callback, ())

    def send_continue(self, request_id, body):
        """Send continue message if socket open."""
        if not self.connected:
            LOGGER.error("Connection is not (yet) ready!")
            return
        body = json.dumps(body)
        msg = (
            "MOO/1 CONTINUE Changed\nRequest-Id: %s\nContent-Length: %s\nContent-Type: application/json\n\n%s"
            % (request_id, len(body), body)
        )
        msg = bytes(msg, "utf-8")
        self._socket.send(msg, 0x2)

    def send_complete(self, request_id, name, body=""):
        """Send complete message if socket open."""
        if not self.connected:
            LOGGER.error("Connection is not (yet) ready!")
            return
        msg = "MOO/1 COMPLETE %s\nRequest-Id: %s" % (name, request_id)
        if body:
            body = json.dumps(body)
            msg += "\nContent-Length: %s\nContent-Type: application/json\n\n%s" % (
                len(body),
                body,
            )
        else:
            msg += "\n\n"
        msg = bytes(msg, "utf-8")
        self._socket.send(msg, 0x2)

    def send_request(
        self, command, body=None, content_type="application/json", header_type="REQUEST"
    ):
        """Send request to the roon sever."""
        with _REQUEST_LOCK:
            if not self.connected:
                LOGGER.error("Connection is not (yet) ready!")
                return False
            request_id = self._requestid
            self._requestid += 1
            self._results[request_id] = None
        try:
            self._send_allocated(command, request_id, body, content_type)
        except Exception:
            with _REQUEST_LOCK:
                self._results.pop(request_id, None)
            raise
        return request_id

    def _send_allocated(self, command, request_id, body, content_type="application/json"):
        if body is None:
            msg = "MOO/1 REQUEST %s\nRequest-Id: %s\n\n" % (command, request_id)
        else:
            body = json.dumps(body)
            msg = (
                "MOO/1 REQUEST %s\nRequest-Id: %s\nContent-Length: %s\nContent-Type: %s\n\n%s"
                % (command, request_id, len(body), content_type, body)
            )
        msg = bytes(msg, "utf-8")
        self._socket.send(msg, 0x2)
