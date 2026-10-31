# Synthetic navigation preview

Run `python tools/navigation-preview/prepare.py OUTPUT_DIRECTORY` from an
isolated worktree. The builder extends tools/visual-preview/prepare.py, copies
the real changed QML/Model and replaces Service with an in-memory fixture.
The temporary ID is `io.github.cbayschm74.tonearm-preview`. No backend, Core host,
credentials or private configuration is copied. All player sends are blocked.
Only synthetic navigation operations receive callbacks; pending replies are
bounded and can be held/flushed to exercise stale callbacks.

Use only the existing shell. Before registration, save exact shell.json bytes
and mode privately; inspect unrelated edits before restoring. Enable only the
temporary ID. The fixture IPC target adds `nav`, `fixture` and `inspect`; nav
supports library/next/previous/cursor/enter/back/down/up/reset/hold/behavior/
flush/flush_one. This instrumentation is generated into the preview copy only.

Close and disable the preview, verify the original shell state, restore its
exact saved bytes/mode, remove only the validated preview directory, then rescan
plugins. Verify installed Tonearm source and daemon PID are unchanged. Do not
connect this Service to Roon or start a second shell/daemon. See
../../docs/usage.md for product behavior and current limitations.
