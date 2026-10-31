# tonearm

Roon now-playing and transport, in the Omarchy shell bar.

Tonearm shows what's playing on your Roon system — album art, artist, and
album — with play/pause, previous/next, seek, and volume, plus the ability to
switch which Roon zone the widget follows. Browse the library, search for a
track or album, use Play Now / Play Next / Add to Queue, and inspect the selected
zone’s queue and remaining time. See the [usage guide and current limits](docs/usage.md).

It also publishes an MPRIS player
on the session bus, so your keyboard's media keys (play/pause, next,
previous) control Roon like they would any other player, with no Hyprland
configuration required.

## Install

1. Add the plugin and put it in the bar:

   ```bash
   omarchy plugin add https://github.com/ssandys/tonearm.git --enable
   ```

   This clones tonearm into `~/.config/omarchy/plugins/ssandys.tonearm/` and
   enables the bar widget. It appears on the right of the bar straight away,
   showing an error glyph — there's no daemon behind it yet, which the next
   two steps fix. Open the popup at any point and its header tells you what
   it's currently waiting on.

   (`omarchy plugin install` is an alias for the same thing. To place it
   somewhere other than the default: `omarchy plugin enable ssandys.tonearm
   --section left`.)

2. Install the one runtime dependency Omarchy doesn't already ship:

   ```bash
   omarchy pkg add python-dbus-next
   ```

   (The other Python dependency, `websocket-client`, is already present on a
   stock Omarchy install.)

3. Run setup:

   ```bash
   ~/.config/omarchy/plugins/ssandys.tonearm/setup.sh
   ```

   This installs and starts `tonearmd`, the background daemon that talks to
   your Roon Core, as a systemd user service (`tonearmd.service`). It starts
   automatically on every login from then on.

   If automatic discovery cannot see a Core that you know is reachable, supply
   it explicitly on the first setup:

   ```bash
   ~/.config/omarchy/plugins/ssandys.tonearm/setup.sh --core 192.168.50.44
   ```

   Tonearm uses Roon's advertised HTTP/MOO port `9330` and TCP port `9150` by
   default. `HOST` can be an IPv4 address or DNS hostname. A Core advertising
   different ports can be bootstrapped with `--http-port PORT` and
   `--tcp-port PORT`. Re-running this for the same Core is safe and preserves
   pairing and the pinned zone. It deliberately refuses to replace a different
   configured Core; switching Cores also has pairing implications and is not
   part of first-run bootstrap. This also means `--core` is not a recovery path
   for a Core that moved to a new address: it refuses the replacement host and
   preserves any stored Core identity. Use the re-pair/reset path for a moved
   Core or incorrect identity rather than editing Tonearm's private config.

4. Open **Roon Remote → Settings → Extensions** and enable **tonearm**.
   `tonearmd` registers itself with your Roon Core as soon as it starts, but
   Roon will not talk to it until you approve it here — this is a one-time
   pairing step per Roon Core. Until you do, the popup header reads
   *Enable tonearm in Roon → Settings → Extensions*.

5. Check that everything came up correctly:

   ```bash
   ~/.config/omarchy/plugins/ssandys.tonearm/setup.sh --check
   ```

   This confirms the service is running, the daemon is answering on its
   socket, and — until you complete step 4 — will tell you it's running but
   not yet paired.

To update later: `omarchy plugin update ssandys.tonearm`, then re-run
`setup.sh` if the daemon changed.

Once paired, the bar widget starts showing whatever is playing on your
followed Roon zone.

The popup's header names the Roon Core you're attached to. If something is
wrong — the daemon isn't running, the extension isn't enabled yet, the Core is
unreachable — the header says so in place of the Core's name, so you never
have to guess why nothing is playing.

## What it does on your network

Worth knowing before you install something that runs unsandboxed in your shell.

**Finding your Core, once.** Roon Cores announce themselves over SOOD, but the
standard multicast query draws no reply on many networks (an access point
filtering multicast is enough). So on **first run only**, if no Core is already
configured, tonearm scans your local subnet: it opens a TCP connection to one
port — 9330, Roon's HTTP/MOO port — on each address in the `/24`, and sends a
SOOD query to whatever answers.

Supplying `setup.sh --core HOST` writes that explicit address through the same
private config path and skips this initial discovery. It does not change later
connection or relocation behavior.

Constraints on that scan:

- **First run only.** The Core's address is written to
  `~/.config/tonearm/config.json` and reused from then on. Delete that file and
  it scans again; otherwise it never repeats.
- **Private address space only.** Each interface's own IPv4 address is checked
  with `ipaddress.is_private` before its `/24` is scanned, so a machine with a
  public address on an interface does not get its neighbours probed.
- **The subnet comes from the interfaces themselves** (`SIOCGIFADDR`), not from
  a routing lookup — on a host running a VPN, the usual "UDP-connect to a
  scratch address" trick returns the tunnel's address and would point the scan
  at an unrelated subnet.
- **At most 512 addresses, total.** That is a ceiling on the whole run, not per
  interface, and the list is deduplicated first, so two interfaces sharing a
  subnet probe it once rather than twice. A machine with more qualifying
  interfaces than that stops at the budget and logs it.

**After that**, the only traffic is to your Core: a WebSocket to port 9330 for
the Roon MOO protocol, and HTTP GETs to the same host for album art.

**Nothing leaves your network.** There is no telemetry, no analytics, and no
outbound connection to anything but your Roon Core.

## Zones

The `ZONES` list at the bottom of the popup does two different things, and
they are deliberately separate targets:

| Action | What it does |
|---|---|
| Click a zone's name | Follow that zone — changes what the widget *shows*. Clicking the zone you're already pinned to unpins it, so the widget goes back to following whichever zone is playing. |
| Click the cast icon at the right of a row | **Move the music there.** Roon's queue, the current track and its position all move to that zone. |

The cast icon only appears on rows that can actually receive the stream: not
on the zone you're already listening to, and not when there's nothing playing.
Because it moves the queue rather than the playback state, it works while
paused too — pause in the kitchen, move it to the study, press play there.

If you had pinned a zone, the widget re-pins to the destination so it doesn't
sit watching the room you just emptied. If you weren't pinned, it follows the
music on its own as usual.

From a script: `tonearmctl transfer <zone_id>`.

There's no keyboard shortcut for this yet — the zone rows aren't part of the
popup's keyboard cursor, which covers Library/search results or the expanded queue.

## Strict pinning (optional)

By default, if a pinned endpoint disappears, Tonearm falls back to another
active zone. To keep controls from affecting another room, set the JSON boolean
`"strict_pins": true` in Tonearm's private `~/.config/tonearm/config.json`,
preserving the other fields. Restart the daemon for the setting to take effect.
Do not share this directory: it contains pairing credentials.

With strict pinning enabled, a missing pinned zone leaves no selected zone and
its controls are refused. Other zones remain listed, and the saved pin is kept
for the endpoint's return. Returning does not start playback or restore volume.
Unpinning restores auto-follow. Omit the setting or set it to false to keep the
existing fallback. There is no UI or CLI switch for this option.

Remote commands are also refused while the Core is disconnected, regardless of
this setting; cached zones are not usable playback targets. The selected-output
browse actions also recheck the intended zone and connection during resolution.
These checks do not make a remote Roon operation transactional.

## Library, search and queue

Open the popup and choose the output you want in **ZONES**. The three browse
actions use that selected output, even though their short labels omit its name.
Selecting a missing output does not authorize a browse action in another room.

Click **Library** to browse categories, or click the search hint / press `/`
to search. Most printable keys can also start a query; press `/` first for the
shell-reserved keys `h j k l x X` and Space. Type the query and press Enter.
Search can take a few seconds, particularly for streaming-service results.

| Control | Behavior |
|---|---|
| Click a result, Enter, or Right | Navigate into the current row; does not play it |
| Up / Down | Move the current row highlight |
| Previous / Next below the results | Change page (up to 100 rows per page) |
| Left | Return to the parent list |
| Esc | Leave search editing, go back through results, then close the popup |
| **Play Now** | Ask Roon to play the highlighted track or whole album now |
| **Play Next** | Ask Roon to put the highlighted track or whole album next |
| **Add to Queue** | Ask Roon to append the highlighted track or whole album |

Choose the **album row**, rather than one of its tracks, to act on the whole
album. The three actions appear side by side. After an action, the browse view
clears; browse or search again for your next selection. A “requested” message
means Tonearm sent an attempt; check Now Playing or Queue for the remote result.
Missing/unsupported actions are refused rather than replaced with Play Now.
An unknown result is not retried automatically; inspect Roon before repeating it.

Click **Queue** to expand the selected output's queue. Up/Down or a click moves
the highlight; Enter does not play that row. Esc collapses it. The pane shows up
to 100 items, and the header shows Roon's whole-queue time, for example
**Queue (59:32)** or **Queue (1:02:03)**. Roon's updates refresh the time as
playback advances or the queue changes; the total is not a sum of those 100 rows.
If no valid total is available, the header simply says **Queue**.

**Current limits:** one track or one whole album per action; no Ctrl/Shift
multi-selection, queue-item removal, queue reorder, clear-queue control or
play-from-here action. Use official Roon to remove or rearrange items. Roon
controls whether an enqueue operation can start audio on an empty/stopped queue;
use a safe volume. Streaming tracks such as TIDAL can take longer to start.

The [usage guide](docs/usage.md) includes a walkthrough, limitations and what to
include in a bug report. This feature set is available for testing, not a claim
that every endpoint, theme or reconnect case has been qualified.

## Settings

Configure these from the widget's settings panel in the Omarchy shell:

| Setting | Default | What it does |
|---|---|---|
| Tint the seek bar with the album's color | on | Derives an accent color from the current track's album art and uses it for the seek bar fill. When off (or when no art is available), the theme's accent color is used instead. |

There is deliberately no setting for the album art size or for hiding the
volume slider. The art size is derived from your theme's spacing scale so the
card stays balanced at any font size, and the volume row hides itself
automatically on a zone whose output reports no volume — a switch for that
would have been a second way to reach an outcome the widget already gets
right.

## Media keys

Tonearm publishes an MPRIS2 player (`org.mpris.MediaPlayer2.tonearm`) on
your session D-Bus as soon as it connects to Roon. Omarchy's existing
media-key handling picks it up automatically — no changes to your Hyprland
config are needed. If multiple players are on the bus at once (a browser tab,
a local music player, etc.), your media keys follow whichever your desktop's
existing MPRIS arbitration considers active, the same as any other MPRIS
player.

## If your Roon Core goes away

Tonearm notices within a few seconds. The bar icon switches to the alert
glyph in the error colour, and the popup's header says **Roon Core
unreachable** in place of the Core's name. The popup stops showing a track
rather than leaving the last one on screen, because a stale track under an
error header is the widget being confidently wrong.

Recovery is automatic — the underlying Roon library retries about every 20
seconds, and tonearm goes back to normal on its own when the Core answers
again. There is nothing to restart.

If the problem is *your* network rather than the Core — a VPN that captures
your local subnet, wifi associated but not routing — the header says **No
route to your network** instead. Tonearm works this out by probing your
default gateway when a connection fails: if the gateway itself does not
answer, the Core is not the thing to go and look at.

## Removal

Removing just the plugin folder leaves the user service behind. Remove the
service first, then the plugin. The uninstall command checks that the unit
belongs to tonearm and preserves your Core address, pairing token and pinned
zone for a future reinstall:

```bash
# 1. Stop and remove the systemd user service (safe to repeat)
~/.config/omarchy/plugins/ssandys.tonearm/setup.sh --uninstall

# 2. Remove the plugin
omarchy plugin remove ssandys.tonearm      # or ./bin/dev down for a dev deploy

# 3. Optional: clear tonearm's own config, including the Roon pairing token
rm -rf ~/.config/tonearm/
```

If the plugin folder was already removed, `setup.sh` is no longer available.
Units installed with this version skip starting when the daemon path is
missing; older units may still retry with `203/EXEC`. In either case, remove
the leftover unit manually:

```bash
systemctl --user disable --now tonearmd.service
rm ~/.config/systemd/user/tonearmd.service
systemctl --user daemon-reload
```

That last step is optional but worth knowing about: `~/.config/tonearm/`
holds the Core's host/port, your **pairing token**, and the pinned zone. If
you delete it, reinstalling tonearm later means re-approving the extension
in Roon Remote → Settings → Extensions from scratch, the same as a first
install. Leaving it in place lets a reinstall pick up where you left off.

You can also disable tonearm from Roon's side at any time, independently of
anything above, from **Roon Remote → Settings → Extensions**.
