# Queue view — DRAFT, live acceptance pending

The popup's Queue disclosure displays a read-only bounded window for the selected
zone. Click it to expand. Up/down move one cursor, Enter does nothing, and Escape
collapses the view. The window has at most 100 rows and may omit queue items.
An older daemon without the additive `queue` snapshot field leaves the disclosure
unavailable. No second backend starts. There are no queue edits or row artwork.

Rows arrive through the existing daemon snapshot/subscribe stream. One session
owns the subscription independently of pane reloads and monitor count. Only the
existing connection watcher binds/cancels; snapshot reads never call the SDK.
Selection/status/API/socket/full-list authority changes invalidate callbacks
before cancellation. After an invalid event the cache clears, reports an error,
and allows one fresh subscription; another failure stays in error until context
changes. A subscription send failure does not start a retry loop.

Queue time is optional selected-zone `queue_time_remaining` metadata from the
existing zone/seek path, never a sum of visible rows. Unknown, malformed, negative,
non-finite or oversized numbers are null; zero is real. Unknown/unavailable
selection hides it. Radio/repeat totals are not inferred.

## Evidence boundary

Retained batch 7 proves only one paused iFi initial `Subscribed` with `items`,
integer `queue_item_id` and string `three_line.line1`/`line2`. It does not record
the initial frame's MOO verb. No live Core, playback, queue, D-Bus or installed
action was performed while preparing this draft.

Official [transport lib.js](https://github.com/RoonLabs/node-roon-api-transport/blob/master/lib.js)
confirms `zone_or_output_id`, `max_item_count`, forwarded response/body and the
optional zone queue-time scalar. Official
[moo.js](https://github.com/RoonLabs/node-roon-api/blob/master/moo.js) confirms
allocated subscription keys, keyed unsubscribe and terminal COMPLETE requests.
These sources do not establish consumer delta bodies. The owned callback keeps
the event name and explicitly supplies the MOO verb as its third argument;
legacy callbacks remain body-only and return None.

All new offline frames are explicitly synthetic. Empty `items`, string IDs,
`Changed.changes` ordered insert `{operation,index,items}` and remove
`{operation,index,count}` are defensive contract assumptions, not live schemas.
Other operations, malformed indices, duplicate IDs, deltas before a snapshot,
oversized windows and unsupported responses fail closed. Labels are clipped to
256 characters; IDs are bounded. Owned wire frames cap at 64 KiB, encoded bodies
at 60 KiB, and normalized rows below 60 KiB including snapshot field overhead.
The existing 256 KiB server envelope cap and degradation rules remain in force.

This remains **DRAFT** pending separately authorized sanitized live checks of:

- Initial MOO CONTINUE versus COMPLETE and optional nested item fields.
- Empty queue acknowledgement distinguished from an error.
- Ordered Changed insert/remove and rolling window behavior beyond 100 rows;
  whether `max_item_count` is honored.
- Error, end and Unsubscribed frames, plus wire-level remote cancellation
  acknowledgement (local cancellation is not that proof).
- Any output beyond the single paused iFi sample.
- A finite numeric queue-time sample and actual countdown behavior, including
  unknown totals for radio/repeat where applicable.

Synthetic tests and mocked Qt renders do not satisfy the maintainer's live-schema
gate. No schema probe or new public RPC is included in this consumer.

## Independent composition with Library PR 55

This branch starts directly at `62faec956df5ed4ed6699d81e582f4ea8973daa3`
(0.14.6), independent of PRs 54 and 55. QueuePane/QueueModel import no Library
helpers or actions. Panel hosts the queue, routes cursor/Enter/Escape only while
it is expanded, and keeps upstream search routing while collapsed.

Expanding Queue calls upstream `BrowsePane.releaseSearch()` and temporarily hides
the browse pane and its separator. It does not reset or send a browse request.
Panel listens to existing `hasContentChanged`, `editingChanged` and `busyChanged`
signals. Opening Library via PR 55's shortcut sets busy/content, which collapses
Queue and reveals Library; search editing does the same. No shortcut/action from
55 is copied. Both merge orders have the same behavior when both integrations
are retained. BrowsePane restarts its existing bounded row-art retry when its
visibility returns, so a timer that fired while the queue overlay was open
does not strand placeholders. This hook issues no request itself. Textual
conflicts may occur in BrowsePane's visibility/art scheduling, Panel's pane
column/key handlers or core's import/init/publish/watcher/stop integration;
preserve both the Library changes and the visibility retry hook without adding
semantic helpers.

Focused synthetic regression commands and mocked visual/click results are
recorded in `/tmp/10p-queue-worker-report.md`. Coordinator review found and
fixed the hidden-pane art-retry interaction; the focused offscreen Qt regression
passes. Full offline suite passes, but live-schema acceptance remains pending.
The coordinator owns draft publication; no live probe is part of this change.
