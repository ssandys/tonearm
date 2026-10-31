"""Tiny opt-in state/pause client; explicit socket required, no daemon startup.

Run with PYTHONPATH=scripts python -m tonearm_lib.contract_client --socket PATH.
Add --pause-zone ID only for an explicitly authorized test. Never retries.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import uuid

from . import targeted

MAX_REPLY_BYTES = 256 * 1024
TIMEOUT = 5.0


class ContractError(ValueError):
    """No reliable correlated result. A command's remote effect may be unknown."""


def exchange(path, request):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(TIMEOUT)
            conn.connect(path)
            conn.sendall((json.dumps(request) + "\n").encode())
            with conn.makefile("rb") as reader:
                line = reader.readline(MAX_REPLY_BYTES + 1)
        if len(line) > MAX_REPLY_BYTES or not line.endswith(b"\n"):
            raise ContractError("missing/oversized reply; unsupported daemon or unknown effect; no retry")
        reply = json.loads(line)
        if not isinstance(reply, dict):
            raise ContractError("invalid reply; no retry")
        return reply
    except (OSError, ValueError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("invalid/missing reply; remote effect unknown; no retry") from None


def status(path):
    reply = exchange(path, {"cmd": "status"})
    if type(reply.get("v")) is not int or reply["v"] != 1 or "status" not in reply:
        raise ContractError("unsupported state reply")
    return reply


def pause(path, zone_id, request_id=None):
    request = {"cmd": "pause_targeted", "v": 1,
               "request_id": request_id if request_id is not None else uuid.uuid4().hex,
               "zone_id": zone_id}
    if not targeted.valid_request(request):
        raise ContractError("invalid request identity")
    reply = exchange(path, request)
    if not targeted.valid_result(reply, request):
        raise ContractError("uncorrelated/unsupported result; remote effect unknown; no retry")
    return reply


def player(path, request):
    if not targeted.valid_player_request(request):
        raise ContractError("invalid player request")
    reply = exchange(path, request)
    if not targeted.valid_player_result(reply, request):
        raise ContractError("uncorrelated/unsupported player result; effect unknown; no retry")
    return reply


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True, help="explicit Unix socket path")
    parser.add_argument("--pause-zone", help="explicit selected zone to pause once")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(status(args.socket)))
        if args.pause_zone is not None:
            reply = pause(args.socket, args.pause_zone)
            print(json.dumps(reply))
            return 0 if reply["outcome"] == "attempted" else 2
        return 0
    except ContractError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
