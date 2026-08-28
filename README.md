# tonearm

Roon now-playing and transport, in the Omarchy shell bar.

Tonearm shows what's playing on your Roon system — album art, artist, and
album — with play/pause, previous/next, seek, and volume, plus the ability to
switch which Roon zone the widget follows. It also publishes an MPRIS player
on the session bus, so your keyboard's media keys (play/pause, next,
previous) control Roon like they would any other player, with no Hyprland
configuration required.

## Install

1. Install the one runtime dependency this plugin needs that Omarchy doesn't
   already ship:

   ```bash
   omarchy pkg add python-dbus-next
   ```

   (The other Python dependency, `websocket-client`, is already present on a
   stock Omarchy install.)

2. From the installed plugin's directory, run setup:

   ```bash
   ~/.config/omarchy/plugins/ssandys.tonearm/setup.sh
   ```

   This installs and starts `tonearmd`, the background daemon that talks to
   your Roon Core, as a systemd user service (`tonearmd.service`). It starts
   automatically on every login from then on.

3. Open **Roon Remote → Settings → Extensions** and enable **tonearm**.
   `tonearmd` registers itself with your Roon Core as soon as it starts, but
   Roon will not talk to it until you approve it here — this is a one-time
   pairing step per Roon Core.

4. Check that everything came up correctly:

   ```bash
   ~/.config/omarchy/plugins/ssandys.tonearm/setup.sh --check
   ```

   This confirms the service is running, the daemon is answering on its
   socket, and — until you complete step 3 — will tell you it's running but
   not yet paired.

Once paired, the bar widget starts showing whatever is playing on your
followed Roon zone.

## Settings

Configure these from the widget's settings panel in the Omarchy shell:

| Setting | Default | What it does |
|---|---|---|
| Tint the seek bar with the album's color | on | Derives an accent color from the current track's album art and uses it for the seek bar fill. When off (or when no art is available), the theme's accent color is used instead. |
| Album art size in the popup | 118px | Size of the album art shown in the popup, from 96 to 256 pixels. |
| Show the volume slider | on | Shows or hides the volume control in the popup. |
| Notify when the Roon Core becomes unreachable | on | Sends a desktop notification if tonearm loses its connection to Roon. |
| Notify when the followed zone changes | off | Sends a desktop notification whenever the zone tonearm is displaying changes (for example, because you started playback somewhere else in Roon). |

## Media keys

Tonearm publishes an MPRIS2 player (`org.mpris.MediaPlayer2.tonearm`) on
your session D-Bus as soon as it connects to Roon. Omarchy's existing
media-key handling picks it up automatically — no changes to your Hyprland
config are needed. If multiple players are on the bus at once (a browser tab,
a local music player, etc.), your media keys follow whichever your desktop's
existing MPRIS arbitration considers active, the same as any other MPRIS
player.

## Known limitation

If your Roon Core goes offline *after* tonearm has already connected to it,
tonearm does not currently notice. The daemon's status stays `ok` and the
displayed zone/track data simply goes stale — it doesn't switch to an error
state until the connection is retried from a cold start (for example, a
restart of `tonearmd.service`). If the widget looks "stuck," restarting the
service is the workaround:

```bash
systemctl --user restart tonearmd.service
```

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
