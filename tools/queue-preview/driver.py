"""Read-only diagnostic IPC client; never installs, opens or controls anything."""
import json
import subprocess
import sys

TARGET='io.github.cbayschm74.tonearm-queue-diagnostics-fixture'


class PreviewError(ValueError):
    pass


def decode(result, method):
    if result.returncode:
        # Target is synthetic only. Keep the actual cause instead of a bare
        # assertion, while bounding any wrapper error or unexpectedly large text.
        raise PreviewError(json.dumps({'method':method,'exit_code':result.returncode,
                                       'stdout':result.stdout[:2048],'stderr':result.stderr[:2048]}))
    if len(result.stdout)>65536:
        raise PreviewError('oversized synthetic IPC reply')
    try:
        value=json.loads(result.stdout)
    except ValueError:
        raise PreviewError('non-JSON synthetic IPC reply: '+result.stdout[:2048]) from None
    if not isinstance(value,dict):
        raise PreviewError('invalid synthetic IPC object')
    if method=='diagnostics' and (value.get('build')!='queue-diag-v1' or not isinstance(value.get('instance'),str)
                                  or not isinstance(value.get('history'),dict)):
        raise PreviewError('wrong/mixed diagnostic fixture; stop without shell restart')
    return value


def require_open(value, queue=False):
    if value.get('opened') is not True or (queue and value.get('queueOpen') is not True):
        raise PreviewError('popup/queue closed; transition test not admitted')
    return value


def read(method):
    if method not in ('inspect','diagnostics'):
        raise PreviewError('read-only method required')
    try:
        result=subprocess.run(['omarchy-shell',TARGET,method],capture_output=True,text=True,timeout=5)
    except subprocess.TimeoutExpired:
        raise PreviewError('synthetic IPC timeout; no retry/restart') from None
    return decode(result,method)


if __name__=='__main__':
    try:
        print(json.dumps(read(sys.argv[1] if len(sys.argv)>1 else 'diagnostics')))
    except PreviewError as error:
        print(str(error),file=sys.stderr)
        raise SystemExit(1)
