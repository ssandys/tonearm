from __future__ import unicode_literals

import threading

import websocket

from .constants import LOGGER, REGISTERED, SERVICE_PING, CONTROL_VOLUME

try:
    import simplejson as json
except ImportError:
    import json

try:
    import thread
except ImportError:
    import _thread as thread


class RoonApiWebSocket(
    threading.Thread
):  # pylint: disable=too-many-instance-attributes
    """Class to handle the roon websocket connection."""

    @property
    def results(self):
        """Return the result of the previous request."""
        return self._results

    # LOCAL PATCH (tonearm #47): synchronize waiter cleanup with reply admission.
    def remove_result(self, request_id):
        """Forget a completed or timed-out request without admitting a late reply."""
        with self._request_lock:
            self._results.pop(request_id, None)

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

    # LOCAL PATCH (tonearm #47): invalidate before unsubscribe, and close even if cancellation fails.
    def stop(self):
        """Stop the socket thread."""
        self._exit = True
        with self._request_lock:
            entries = list(self._subscriptions.values())
            self._subscriptions.clear()
        try:
            for entry in entries:
                try:
                    self._cancel_entry(entry)
                except Exception:
                    LOGGER.warning("subscription cancellation failed during stop")
        finally:
            self._socket.close()

    # LOCAL PATCH (tonearm #47): initialize the per-instance bookkeeping lock.
    def __init__(self, host):
        """Create the websocket connection to the roon server."""

        self._socket = None
        self._request_lock = threading.Lock()
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

    # LOCAL PATCH (tonearm #47): register before send, preserving body-only/None.
    def subscribe(self, service, endpoint, callback, opt_data=None):
        """Subscribe without retaining an ordinary result or exposing a new API."""
        with self._request_lock:
            if not self.connected:
                return None
            subkey = self._subkey
            self._subkey += 1
            request_id = self._requestid
            self._requestid += 1
            data = {"subscription_key": subkey}
            if opt_data and isinstance(opt_data, dict):
                data.update(opt_data)
            entry = {"service": service, "endpoint": endpoint, "request_id": request_id,
                     "subkey": subkey, "callback": callback}
            self._subscriptions[request_id] = entry
        try:
            self._send_allocated(service + "/subscribe_" + endpoint, request_id, data)
        except Exception:
            with self._request_lock:
                if self._subscriptions.get(request_id) is entry:
                    del self._subscriptions[request_id]
            raise
        return None

    # LOCAL PATCH (tonearm #47): cancellation replies have no ordinary result owner.
    def _cancel_entry(self, entry):
        with self._request_lock:
            if not self.connected:
                return
            request_id = self._requestid
            self._requestid += 1
        # Never create/pop a result entry: a reentrant close may reuse this
        # ID for a new ordinary request before cancellation send returns.
        self._send_allocated(entry["service"] + "/unsubscribe_" + entry["endpoint"],
                             request_id, {"subscription_key": entry["subkey"]})

    # LOCAL PATCH (tonearm #47): invalidate all matching routing before sending cancellation.
    def unsubscribe(self, service, endpoint):
        with self._request_lock:
            matches = [(k,v) for k,v in self._subscriptions.items()
                       if v["service"] == service and v["endpoint"] == endpoint]
            for key, entry in matches:
                del self._subscriptions[key]
        for _, entry in matches:
            self._cancel_entry(entry)

    # pylint: disable=too-many-branches
    # LOCAL PATCH (tonearm #47): drop unsolicited results and call subscribers outside the lock.
    def on_message(self, w_socket, message=None):
        """Handle message callback."""
        if not message:
            message = w_socket  # compatability fix because of change in websocket-client v0.49
        request_id = None
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
            else:
                # No callback/I/O while holding the bookkeeping lock. Only
                # ordinary requests already in flight may receive a result.
                with self._request_lock:
                    entry = self._subscriptions.get(request_id)
                    if entry is None and request_id in self._results:
                        self._results[request_id] = body
                if entry is not None:
                    entry["callback"](body)
        except websocket.WebSocketConnectionClosedException:
            # This can happen while closing a connection - so ignore
            pass

        except Exception:  # pylint: disable=broad-except
            # Message bodies can contain personal library data.
            LOGGER.exception("Error while parsing message")

    def on_error(self, w_socket, error=None):
        """Handle error callback."""
        if not error:
            error = w_socket  # compatability fix because of change in websocket-client v0.49
        LOGGER.info("on_error %s", error)

    # pylint: disable=unused-argument
    # LOCAL PATCH (tonearm #47): reset subscription identifiers under the instance lock.
    def on_close(self, w_socket, close_status_code, close_msg):
        """Handle closing the session."""
        LOGGER.debug("session closed (%s) %s", close_msg, close_status_code)
        with self._request_lock:
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

    # LOCAL PATCH (tonearm #47): allocate ordinary requests once under the instance lock.
    def send_request(
        self, command, body=None, content_type="application/json", header_type="REQUEST"
    ):
        """Send request to the roon sever."""
        with self._request_lock:
            request_id = None
            if self.connected:
                request_id = self._requestid
                self._requestid += 1
                self._results[request_id] = None
        if request_id is None:
            LOGGER.error("Connection is not (yet) ready!")
            return False
        try:
            self._send_allocated(command, request_id, body, content_type)
        except Exception:
            with self._request_lock:
                self._results.pop(request_id, None)
            raise
        return request_id

    # LOCAL PATCH (tonearm #47): send outside the bookkeeping lock after registration.
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
