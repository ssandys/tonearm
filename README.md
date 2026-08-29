# tonearm

Roon now-playing and transport, in the Omarchy shell bar.

Tonearm shows what's playing on your Roon system — album art, artist, and
album — with play/pause, previous/next, seek, and volume, plus the ability to
switch which Roon zone the widget follows. It also publishes an MPRIS player
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
popup's keyboard cursor, which currently only covers search results.

## Library search

The popup can search your whole Roon library — not just the current zone or
the album that's playing. The search field stays hidden until you use it, on
purpose, so the popup keeps its normal size when you're just glancing at what's
playing; the `/  search` hint on the `ZONES` line is what tells you it's
there, and clicking it opens the field.

To search, open the popup and press `/` — or just start typing; most
printable keys open the field and start your query with that character.
Press `Enter` to submit. Results come back grouped the way Roon's own search
groups them: a top match, then `Artists`, `Albums`, `Composers`, `Tracks`,
and `Works`, each row showing a count.

The exceptions are `h`, `j`, `k`, `l`, `x`, `X` and Space: the Omarchy
shell's own panel key handler claims those before any widget sees them
(`h`/`j`/`k`/`l` are vim-style movement, and `j`/`k` also double as Down/Up
here; `x` is delete; Space activates). They cannot start a search — press `/`
first and type them into the field.

| Key | Behavior |
|---|---|
| `/` | Focus the search field |
| Any printable key except `h j k l x X` and Space | Focus the field and start the query with that character |
| `Enter` (while typing) | Submit the search |
| `↑` / `↓` | Move the selection |
| `Enter` (on a result) | Play if the row is playable, otherwise descend into it (e.g. `Albums` → its list of albums) |
| `→` | Descend into the selected row |
| `←` | Back one level |
| `Esc` | Back one level; closes the popup at the top level |

Playing a row clears the search and leaves the popup open, so you can pick
something else without reopening it. The now-playing card above updates to
whatever just started, which is your confirmation it worked.

**Play Now is the only action.** There's no Queue button and no queue key,
because the popup has no way to show you a queue — so queuing would be a
control whose effect you could never see. It isn't in the daemon either: Play
Now is the only action tonearm invokes.

## Settings

Configure these from the widget's settings panel in the Omarchy shell:

| Setting | Default | What it does |
|---|---|---|
| Tint the seek bar with the album's color | on | Derives an accent color from the current track's album art and uses it for the seek bar fill. When off (or when no art is available), the theme's accent color is used instead. |
| Album art size in the popup | 118px | Size of the album art shown in the popup, from 96 to 256 pixels. |
| Show the volume slider | on | Shows or hides the volume control in the popup. |

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

## Removal

Removing just the plugin folder is not enough on its own: the systemd user
service keeps pointing at a path that no longer exists and retries on
failure forever. Stop and remove the service first, then the plugin:

```bash
# 1. Stop and remove the systemd user service
systemctl --user disable --now tonearmd.service
rm ~/.config/systemd/user/tonearmd.service
systemctl --user daemon-reload

# 2. Remove the plugin
omarchy plugin remove ssandys.tonearm      # or ./bin/dev down for a dev deploy

# 3. Optional: clear tonearm's own config, including the Roon pairing token
rm -rf ~/.config/tonearm/
```

That last step is optional but worth knowing about: `~/.config/tonearm/`
holds the Core's host/port, your **pairing token**, and the pinned zone. If
you delete it, reinstalling tonearm later means re-approving the extension
in Roon Remote → Settings → Extensions from scratch, the same as a first
install. Leaving it in place lets a reinstall pick up where you left off.

You can also disable tonearm from Roon's side at any time, independently of
anything above, from **Roon Remote → Settings → Extensions**.
