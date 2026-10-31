# In-memory search/action preview

Run `python tools/search-preview/prepare.py OUTPUT_DIRECTORY`. Reuses the existing
visual/navigation builder; copies real changed Panel/Model/BrowsePane and uses
only synthetic Service callbacks. No backend, credentials, Core host or scripts
are copied. All ordinary sends are blocked; play_selected is simulated in memory
and requires play_now, play_next, or add_to_queue. A missing or other action is
refused without moving the preview cursor.

Back up shell.json exact bytes/mode privately before enabling temporary
io.github.cbayschm74.tonearm-preview in the existing shell. Fixture IPC adds
search/edit/play to the navigation operations. The play value accepts
play_now (default), play_next or add_to_queue; all are memory-only. hold/flush exercise late replies;
behavior supports normal/empty/error/malformed/prompt/unknown/mismatch. Never
attach this Service to the real daemon. No second shell or live audio.

Close/disable only the preview, check unrelated edits before restoring exact
shell bytes/mode, remove only the validated preview path and rescan. Verify the
live daemon PID and original source are unchanged. Use a fresh component directory
if Qt retains an earlier fixture component; retire the disabled old copy outside
plugins before enabling its replacement. See the [usage guide](../../docs/usage.md) for product behavior and limitations.
