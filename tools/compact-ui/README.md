# Isolated compact UI check

Prepare a fresh directory outside the installed plugin:

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python tools/compact-ui/prepare.py /tmp/tonearm-compact-ui-check-new
QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software /usr/lib/qt6/bin/qmltestrunner -input /tmp/tonearm-compact-ui-check-new -import /tmp/tonearm-compact-ui-check-new
```

The builder copies the real BrowsePane, QueuePane and their models unchanged.
It supplies synthetic theme/TextField stand-ins and a memory-only browse service.
No production singleton, scripts, credentials, sockets, daemon, shell instance or
settings are loaded. Mouse clicks verify action tokens/selected identity without
sending commands. Header changes use synthetic whole-queue values.

Four cases plus Qt setup/cleanup verify normal/narrow/large-font layout, explicit
action routing, disabled missing output and live header replacement. Screenshots
are written to `/tmp/tonearm-compact-ui-normal.png` and
`/tmp/tonearm-compact-ui-large.png`, plus the expanded queue in
`/tmp/tonearm-compact-ui-expanded.png`. Inspect all three after running. Theme stand-ins
are not full installed-shell acceptance; real popup fit remains a deployment gate.
