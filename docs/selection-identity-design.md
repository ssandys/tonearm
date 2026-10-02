# Selection identity checkpoint

The v1 snapshot gains `selection: {mode, zone_id, availability}`. Existing
`v`, `status`, `core`, `zone`, and `zones` retain their names and shapes.
`mode` is `pinned` or `auto`; availability is `available`, `missing`, `none`,
or `unknown`. `zone_id` is a bounded identifier or null. A malformed or
oversize persisted pin is ignored as a selection, rather than clipped into a
different target or passed unhashably to the arbiter.

The new field describes intent independently from the playable `zone`.
Strict missing pins therefore expose their identity while `zone` stays null;
legacy fallback still exposes its playable fallback in `zone`. Model.js shows
“Pinned output unavailable” only when the Core is healthy, the pin is
confirmed missing, and no playable zone exists; the bar uses warning severity
and the fault glyph for the same case. Old v1 consumers ignore the
additive field and continue to read `zone` and `zones`.

The compatibility claim is about field shape, not identical values at every
instant. During `status: ok` after a reconnect but before a fresh full list,
the new daemon publishes `zone: null` and `zones: []` in those existing v1
fields, even if roonapi still holds the previous connection's rooms. Once the
full list arrives, `zone`/`zones` resume from it. This prevents an older
consumer from presenting stale now-playing data, but is a transient behavior
change for consumers that previously displayed cached rooms. A pinned ID can
remain in `selection` with `availability: unknown` during this interval.
For auto-follow, `none` means no room is selected; a paused last-followed
room remains selected and reports `available`.

An authoritative list is a successful initial `get_zones` response or a full
`zones` subscription, including an empty list. The vendored API increments a
per-instance generation for only those events. A connection drop requires a
new generation before the daemon publishes zone data again. Incremental
updates, an unanswered initial query, and socket reconnection alone cannot
prove a pin missing. This avoids the observed healthy connection arriving
before its zones. A Core that never supplies a full list stays `unknown`;
that is preferable to a false missing claim. This is snapshot readiness only:
direct CLI/MPRIS commands still use the existing command path, which can read
cached zones while status has returned to `ok`. Command readiness and
target/result protocol are separate work. This checkpoint adds no command
targeting, result protocol, QML control, or visual acceptance.
