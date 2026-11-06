# Selected-output player controls

The popup captures the displayed zone when a control is clicked. A current
daemon checks that zone against its current selection and fresh output list
before calling Roon. A selection change, missing zone, stale list or changed
output group refuses the request rather than sending it elsewhere.

Controls are derived from raw Roon metadata:

- Transport requires an available selected zone.
- Seek requires is_seek_allowed: true and a positive finite track length.
- Absolute volume uses only the first output's raw scale and captured output ID.
  Fixed and incremental outputs do not receive an absolute slider.
- Mute requires usable mute metadata on every group member. It captures the
  complete unique list of output IDs (at most 32); a changed list is refused.

Presentation defaults do not grant capability. The daemon rechecks capabilities
and argument ranges at dispatch. Commands do not pin a zone, transfer playback,
restore volume or retry after uncertainty.

## Compatibility

A plugin update can load new QML while the older daemon is still running.
The widget chooses its route before sending:

| Snapshot | Route |
|---|---|
| Selected zone with valid selection and controls | Targeted request |
| Displayed zone with neither field (older v1 daemon) | Existing plain command |
| Available matching selection, controls absent (0.14.4 daemon) | Existing plain command |
| Missing zone, disconnected, unknown/missing selection, malformed or partial contract | No command |

The legacy route preserves the older daemon's fire-and-forget behavior. It has
no correlated result and resolves the active zone in that daemon; it cannot
provide the modern captured-target guarantee across a concurrent selection
change. Rerunning setup applies the current daemon as described in the README.

Once a targeted request is sent, an empty, malformed, foreign or timed-out reply
never triggers a legacy command. Late replies cannot overwrite feedback for a
newer click or a different selection. Playback and volume displays continue to
come from subscriptions, rather than optimistic command results.

## IPC

tonearmctl player '<json>' accepts one envelope:

~~~json
{"cmd":"player_targeted","v":1,"request_id":"player-1","zone_id":"zone-a","verb":"playpause","arg":null,"output_id":null,"output_ids":null}
~~~

The exact fields are required. Request IDs are 1–64 characters; zone/output IDs
are 1–128. Verbs are play, pause, playpause, next, previous, seek, volume, mute,
unmute. Seek and volume take integer arg; volume requires output_id,
mute/unmute require unique output_ids; other target fields are null.

A correlated reply includes v, type: command_result, request_id,
zone_id, verb, outcome and error:

- attempted, error null: the SDK invocation returned a non-null answer.
  This does not prove audible playback or remote acceptance.
- refused: nothing was sent. Errors are bad_request, disconnected,
  zones_unknown, unavailable, selection_changed, capability_unavailable,
  out_of_range, output_changed or unsupported.
- unknown: the SDK returned no answer, raised, or the handler failed after
  dispatch may have begun (dispatch_failed or internal_error). No retry.

A group mute is sequential and can partially complete. It stops on the first
uncertain response; there is no compensating unmute or automatic second attempt.
The client budget is 90 seconds for up to 32 ordinary 2.5-second SDK requests
plus overhead. Status and browse keep their existing 5/25-second budgets.

CLI exit codes: 0 attempted; 1 correlated refusal/unknown; 2 usage; 3 daemon
unavailable; 4 timeout; 5 missing or invalid correlated result. All failure
paths send at most one player envelope.

## Readiness and scope

Both targeted and existing command paths wait for an authoritative full zone
list after reconnect. A missing generation marker remains unknown. The popup
shows **Loading outputs…** during this interval, distinct from a confirmed
unavailable pinned zone. Pin configuration may still be changed while loading.

The change adds no Library play actions, queue UI/subscriptions or vendor API.
It preserves daemon-local bounded artwork and the filtered MPRIS bus proxy.
MPRIS uses its existing command route with the new readiness gate; captured
MPRIS targets are a separate follow-up.

Tests use synthetic snapshots, fake SDKs and temporary local IPC. Offscreen
checks use mocked host/process boundaries; real-shell popup appearance and
USB DAC, built-in speakers and network-zone coverage remain separate live checks.
