# Queue view offscreen checks

Run `/usr/bin/python tools/queue-preview/run.py`. It stages exact QueuePane.qml
and QueueModel.js plus QtTest fixtures in a temporary directory and runs system
Qt's qmltestrunner offscreen/software, with no production qmldir/Service singleton.
No shell, backend, network, credentials, installed plugin or config is accessed.
It removes its staged source after completion. No second shell/daemon is launched.

Tests cover actual QML visibility/timer/single-flight behavior, pending read across
close/reopen, stale selection replies, empty/error/disconnected states, bounded
list/keyboard methods and Enter as a no-op. Synthetic ready/unavailable screenshots
are written to /tmp/tonearm-queue-view-{ready,unavailable}.png for visual inspection.
These are isolated component checks, not full popup/theme/hot-reload/live acceptance.

Pure requests/replies: `node --test --test-isolation=none tests/queue-model.test.js`.
Node26's default isolation reported only file-level results here; the explicit flag
reports the six individual cases. Full normal suite can use the same NODE_OPTIONS
without changing repository test runner settings. Qt semantic lint for this pure
component: `/usr/lib/qt6/bin/qmllint QueuePane.qml`.

Integration review adds the installed pure QtQuick PanelKeyCatcher plus the exact
queue key handlers extracted from Panel.qml to the offscreen harness. This tests
real key dispatch/focus for arrows, Return, Space and Escape without Quickshell or
a shell window. Its source is read-only under /usr/share/omarchy/shell/Ui; the
manual harness requires that installed file. Ten Qt entries now pass.

`python tools/queue-preview/prepare.py NEW_DIRECTORY` prepares the complete actual
Panel with in-memory search/queue Service callbacks, synthetic four zones and up
to100 queue rows. It does NOT install/enable it. All action sends remain blocked.
Queue fixture IPC supports open/close, mode (ready/large/empty/loading/unknown/
unavailable/disconnected/error/mismatch), hold and flush. inspect reports queue
state/reads/pending/cursor and whether the full content column fits the card.
Registration, real shell focus/geometry and cleanup require separate approval.

## Bounded diagnostic fixture

Prepare with an explicit fresh ID, for example:
`python tools/queue-preview/prepare.py OUTPUT --id io.github.cbayschm74.tonearm-queue-diagnostics`.
Only generated fixtures change: the production Panel/Service/queue/backend do not.
The generated Service has a128-event scalar-only ring, sequence numbers, build tag
queue-diag-v1 and unique instance ID. It records key signals, popup/focus/queue edges,
blocked verb names and synthetic queue request/reply/flush identities. No typed text,
Core bodies, titles, credentials or personal library data are logged. The journal
survives popup close while the bar widget/service instance remains alive; removal
or engine/component replacement can destroy it and must be detected by instance ID.

New IPC `diagnostics` returns history and current context; `arm` opens queue/focus
and records readiness. Collect diagnostics before cleanup even on a failed check.
`driver.py` reads only inspect/diagnostics on the diagnostic target; it preserves
bounded IPC exit/stdout/stderr details and rejects wrong builds. require_open()
refuses transitions while closed instead of asserting a misleading product failure.
No silent retry/restart or install action is provided by the driver.

`python tools/queue-preview/diagnose.py` builds/tests the generated fixture in a
private temporary directory, with the actual pure QtQuick key dispatcher and
instrumented Panel handlers. Five Qt entries pass, including key history across
close, blocked verb retention, and held reply identity. Existing run.py still
passes its ten entries. JS journal tests cover ring bounds/detachment/filtering;
Python driver tests cover closed admission and error/version handling.

Before another approved desktop run, obtain manual readiness before arming the
popup; do not open a new question dialog during the key sequence. Returning to chat
may close the popup normally. Inspect the journal rather than treating that final
closed/cursor=-1 snapshot as evidence the keys failed. Physical focus/compositor
and IPC failure causes remain unverified until that run. Do not claim a product
fix from these diagnostic fixtures.
