# Real-service/fake-CLI development preview

prepare.py OUTPUT invokes the existing visual-preview preparer, then copies the
real updated Service.qml and changes only ctlPath to fixture-ctl.py. It adds
fixture selection/probe properties and a preview-only sendPlayer IPC function.
No real daemon/scripts/credentials are copied. Output must not exist. The mock
helper is made executable explicitly; keep that bit when copying it manually.

The helper emits one synthetic state then waits as a subscribe fixture. Player
requests are logged under ~/.local/state/omarchy-roon/player-preview-2026-10-29;
next returns a delayed selection_changed refusal, other accepted fake calls return
attempted. Browse returns an empty successful level. It has no Roon/network imports
or socket operations and rejects other command classes. This is a test fixture,
not a security sandbox or a product backend. No real command should point to it.

Use only a temporary distinct widget in the existing shell, with a private
shell.json backup and exact cleanup as in visual-preview/README.md. Do not load
both preview namespaces simultaneously or start another shell/daemon. The
recorded final run used a fresh component URI to avoid stale QML/JS cache after
a helper permission correction. It checked pause attempt without optimistic
state change, external fixture update, blocked seek/volume, incremental mute,
volume output identity, correlated refusal and delayed response filtering.

Close/disable/remove only the created preview, restore original shell config
if unrelated edits did not intervene, rescan, and verify original daemon PID,
installed source and no remaining fixture helper. Logs/crops are synthetic.
Never point this fixture at a real socket or use it for playback acceptance.
