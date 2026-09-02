# Marketplace submission draft

For `HANCORE-linux/omarchy-plugin-marketplace`. Fields below map 1:1 to the
**"Submit a plugin"** issue form (`.github/ISSUE_TEMPLATE/submit-plugin.yml`)
and to the CLI path documented in the marketplace's own `SUBMISSION.md`, which
is the authority for exact values. Not yet filed.

The marketplace's `SUBMISSION.md` carries an "Instructions for AI agents"
section requiring that the completed title and body be shown to the owner, and
that the issue be created only after the owner explicitly approves. That is
why this document exists rather than a filed issue.

Target commit: set to `git rev-parse master` at filing time.

---

## Repository URL

https://github.com/ssandys/tonearm

## Category

`Widgets`

## Tags

`bar`, `media`, `quickshell`

Exactly three — more are rejected. **Lowercase**: the web dropdown displays
them title-cased, but `SUBMISSION.md`'s CLI path lists the allowed values as
lowercase kebab (`bar`, `media`, `quickshell`, `power-management`). Category is
the opposite — case-sensitive and title-cased (`Widgets`).

## Suggest a missing tag

*(leave empty — `Media` covers it, and suggesting a tag that duplicates an
existing one is noise for the reviewer)*

## Maintainer notes

Roon now-playing, transport, library search and zone transfer in the bar.
QML widget plus a Python daemon; the widget itself makes no network calls and
does no filesystem access of its own.

**What installing commits the user to.** `setup.sh` installs and starts
`tonearmd.service`, a systemd **user** service that starts at login. That is
the one thing a user should know before enabling this, so it is in the
manifest description, not just the README. `README.md` documents removal
including the service teardown, because removing only the plugin folder would
leave a unit retrying against a missing path.

**No privilege.** There is no `sudo` and no `pkexec` anywhere in the
repository — code, scripts or documentation. `setup.sh` uses `systemctl --user`
and writes only under `~/.config/systemd/user/`. The `package-manager`
capability the baseline will report comes from prose only: `README.md`,
`LICENSE` and one `setup.sh` error message all mention `omarchy pkg add` as an
instruction to the user. tonearm installs nothing.

**Network.** On first run only, if no Core is configured, it discovers the
Roon Core: SOOD multicast first, and — because many networks filter multicast —
falling back to opening a TCP connection to one port (9330) on each address in
the local `/24`. That scan is bounded to interfaces whose own IPv4 address is
`ipaddress.is_private`, deduplicated across interfaces, and capped at 512
addresses for the whole run (`sood.MAX_SCAN_HOSTS`) rather than 254 per
qualifying interface; the subnet is taken from the interfaces themselves
rather than a routing lookup, which a VPN would otherwise poison. The result is
cached in `~/.config/tonearm/config.json` and never scanned again. After that
the only traffic is to the Core: one WebSocket for Roon's MOO protocol and HTTP
GETs for album art. No telemetry, no third-party endpoints. This is documented
in the README under "What it does on your network" rather than left for a
reviewer to discover in `sood.py`.

**What the shared shell process consumes.** Informed by your review of
Headway (#2659), which applies to this plugin too:

- The widget reads exactly one file, the cached album-art thumbnail, via
  `ColorQuantizer`. That class has no size cap, no stat and no symlink control,
  so the bound is enforced producer-side: the daemon refuses to publish
  `art_path` for anything that is not a regular file within 1 MiB
  (`art.is_publishable`, using `os.lstat`, not `os.path.exists`, which follows
  symlinks). A residual same-user race is documented in the source rather than
  papered over — closing it would need `O_NOFOLLOW` on the reader, and the
  reader is Qt.
- The art fetch caps the read itself at 1 MiB rather than checking the size
  after buffering the body, and writes through `tempfile.mkstemp` plus
  `os.replace` — never a predictable `<name>.tmp`.
- The daemon's unix socket (0600, in `$XDG_RUNTIME_DIR`) bounds every
  resource a client can reach: the request line at 64 KiB, the time to send
  it at 10s, concurrent handler threads at 32, registered subscribers at 16,
  one write to a subscriber at 5s, and in-memory browse sessions at 8, keyed
  on a `session` string of at most 64 characters. No I/O runs while the
  subscriber-list lock is held, so one stalled peer cannot stall the rest.
- Browse results are capped at 100 rows per level by the daemon.

**Files written.** `~/.config/tonearm/` (0700) holds the Core address, the Roon
pairing token and the pinned zone — token and config are 0600, written to a
temp file and `os.replace`d. `$XDG_RUNTIME_DIR/tonearm/` (0700) holds the
socket and the art cache, capped at 10 files. Nothing else on disk is touched,
and no user configuration outside tonearm's own directories is modified.

**Dependencies.** Two Arch packages the user installs themselves,
`python-dbus-next` and `python-websocket-client`; neither is bundled.
`scripts/vendor/roonapi/` is a vendored, unmodified copy of `roonapi` 0.1.6
(Apache-2.0) with its licence retained in place and the reason for vendoring in
`scripts/vendor/README.md`. tonearm's own code is MIT. `LICENSE` carries the
dependency breakdown below the grant.

**Tests.** `./bin/test` runs 256 Python and 82 JS tests; CI runs it on every
push.

## Submission checklist

- [x] The repository is public and contains installation and removal
      instructions. — `README.md` "Install" and "Removal".
- [x] I have documented the plugin license and any external dependencies. —
      `LICENSE` carries a Dependencies section covering the vendored
      Apache-2.0 `roonapi` and the two runtime packages.
- [x] I confirm that I own or have permission to submit this plugin and its
      preview assets. — `preview.png` is a screenshot of the running widget on
      the author's own machine; the album art in frame is blurred past
      recognition. Track and album titles remain, which are factual metadata
      rather than a reproduction of artwork.
- [x] The plugin does not overwrite user configuration without explicit
      consent. — the only file written outside tonearm's own directories is
      `~/.config/systemd/user/tonearmd.service`, created by a setup script the
      user runs deliberately. **See "Open before filing".**
- [x] I understand that approval is for listing and is not a security review.

---

## Repository description

For the GitHub repo's description field, which the marketplace links to:

> Omarchy shell bar widget for Roon: now-playing, transport, library search,
> zone switching and transfer. Backed by a Python daemon that speaks Roon's
> MOO protocol and publishes MPRIS.

## Ready to file

Nothing outstanding. All five checklist boxes are verified true, the
repository description is set, and the preview carries no third-party artwork.

## Closed while drafting

- **The repository description was empty.** Now set to the text above.

- **`preview.png` contained third-party album artwork.** The art region is now
  blurred past recognition, which clears checklist item 3 while keeping
  everything the image exists to show: the layout, the metadata, the transport,
  the search hint, the transfer icons — and the seek fill, which takes its
  colour from that art, so the blurred colours still explain where the gold
  came from. Reproducible:

  ```
  magick preview.png -crop 236x236+32+104 +repage \
      -virtual-pixel edge -blur 0x18 art.png
  magick -size 236x236 xc:black -fill white \
      -draw "roundrectangle 0,0 235,235 12,12" mask.png
  magick preview.png art.png mask.png -geometry +32+104 -composite preview.png
  ```

  Blurring the region in isolation with `-virtual-pixel edge` rather than in
  place matters: an in-place blur pulls panel background into the box and
  softens its edge. The mask restores the widget's own corner radius
  (`Style.space(6)`), so the preview still shows what the widget renders.

- **`setup.sh` copied onto a predictable path unguarded.** `cp` follows a
  symlink at its destination, so a link planted at
  `~/.config/systemd/user/tonearmd.service` redirected the write — the same
  class as both findings in #2659. The script now refuses a symlink, refuses a
  non-regular file, refuses a regular file that is not tonearm's own unit, and
  writes through `mktemp` plus an atomic rename. Seven tests run `setup.sh` for
  real against a `systemctl` shim; reverting to the bare `cp` fails three of
  them. Closes `docs/FOLLOWUPS.md` item 4.
