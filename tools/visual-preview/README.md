# Synthetic visual preview (development only)

`prepare.py OUTPUT` creates a fresh disposable plugin copy from this worktree:
real Panel/Model/BrowsePane + qmldir, distinct module/IPC identity, and the
no-I/O Service.qml here. It does not enable a plugin, start a daemon, install
packages or read private state. Output must not exist. No backend/scripts copied.
Fixtures have synthetic names, a 42-second position and no hosts/image paths.

Only after recording a reversible preview plan and privately backing up shell
configuration, register/enable the preview in the existing Omarchy shell. Never
launch a second shell or replace the live Tonearm plugin. The preview ID is
`io.github.cbayschm74.tonearm-preview`; fixture IPC target is that ID + `-fixture`.
`fixture NAME` selects states.json entries; `inspect` returns only synthetic UI
state/geometry/focus/counters. The normal preview ID exposes open/close via Panel.
All send/browse actions are blocked; an attempted browse returns preview_only.

Qt can retain JS on a live file reload. Our first hot-reload screenshots still
showed the old loading header; they were not accepted as final. To load a new
candidate without restarting the shell, close/disable/remove the previous
preview, prepare a fresh source directory URI with the SAME temporary ID, rescan
and reenable. Never register two directories claiming that ID at once. Verify
`fixture unknown` reports `Loading outputs…` before capturing final images.

`Panel.instrumentation.qmlpart` adds preview-only fixture/geometry introspection;
it is not product code. Exact native Panel layout is otherwise reused except
module identity and this instrumentation. Current screenshots used the real
shell components, theme and monitor scaling, not an HTML approximation.

Cleanup closes/disables the temporary widget, verifies no unrelated shell config
edits, restores the original bytes/mode, removes only the created preview folder
and rescans. Check original Tonearm source/PID unchanged. Never overwrite user
changes to achieve byte equality. Private backups are not committed.
