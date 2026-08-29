import QtQuick
import Quickshell
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Root is Ui/Panel.qml's `Panel`, NOT `PanelBase` (does not exist here) and
// NOT `BarWidget` (no open/close/toggle lifecycle). See AGENTS.md's "Verified
// QML idiom" table and ~/Src/headway/Panel.qml, the structural reference.
Panel {
  id: root

  // Matches manifest.json's "id" and the IPC target documented in its own
  // description ("Summon with: omarchy-shell shell toggle ssandys.tonearm").
  // galley and colophon (same author, this shell) both set moduleName and
  // ipcTarget to the full plugin id, not the display name -- the brief's
  // draft had moduleName: "Tonearm", which is not what either sibling does.
  moduleName: "ssandys.tonearm"
  ipcTarget: "ssandys.tonearm"

  // REQUIRED: Ui/Panel.qml sets no implicit size of its own. Without these
  // two lines the root is 0x0, `anchors.fill: parent` on the button below
  // faithfully passes that zero along, and the widget renders NOTHING with
  // nothing logged -- confirmed by every sibling plugin's own comment to
  // this effect (galley:29-33, colophon, headway).
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // Ui/Panel.qml provides bar, settings, opened and barForeground -- and NOT
  // these three. A missing one is a ReferenceError raised inside a property
  // binding, invisible to qmllint, that fails as a silently unstyled/absent
  // element rather than a compile error.
  readonly property string barIcon: Model.GLYPH_VINYL
  readonly property color dim: Qt.darker(root.barForeground, 1.45)
  readonly property string fontFamily: root.bar ? root.bar.fontFamily : Style.font.family

  // NOT named `bar` -- Ui/Panel.qml already declares `property QtObject bar`
  // (the injected bar reference WidgetButton reads for hover/tooltip
  // routing). The brief's draft named this computed value `bar`, which
  // shadows that inherited property; QML rejects a duplicate property name
  // on a derived type at compile time, so the whole file would fail to
  // load. `display` is Model.barState's result instead.
  readonly property var st: service.state
  readonly property var display: Model.barState(root.st, service.receivedAt, clock.now)

  // -- popup-only derived state --------------------------------------------
  // root.display.playing, not a fresh `root.zone.state === "playing"` check:
  // Model.barState already derives the identical condition to pick the bar
  // glyph, and re-deriving it here would put the same comparison in two
  // places, only one of them tested.
  readonly property var zone: root.st ? root.st.zone : null
  readonly property bool hasZone: root.zone !== null && root.zone !== undefined
  readonly property var np: root.zone ? root.zone.now_playing : null
  readonly property bool playing: root.display.playing
  readonly property real length: root.hasZone ? (root.zone.length || 0) : 0
  readonly property real pos: Model.position(root.zone, service.receivedAt, clock.now)
  readonly property real progress: root.length > 0 ? root.pos / root.length : 0
  // A fixed- or incremental-volume output reports no volume object at all;
  // null here (not a zeroed-out slider) is what tells the volume row to hide.
  readonly property var volume: root.hasZone ? root.zone.volume : null
  // Real arithmetic with a genuine edge case (v.max === v.min), moved into
  // Model.js and node-tested rather than living only in a QML binding.
  readonly property real volumeFraction: Model.volumeFraction(root.volume)

  readonly property int artPx: root.setting("artSizePx", 118)
  readonly property bool useArtAccent: root.setting("accentFromArt", true)
  // The daemon's now_playing carries art_path: a LOCAL cached copy of the
  // cover, nullable. ColorQuantizer cannot load the Core's own http:// art
  // URL -- measured live, it returns zero colors against that URL and eight
  // against the identical bytes read as file://. The popup's own Image below
  // still displays the remote URL (Model.artUrl), which loads fine; only the
  // quantizer needs the cached file.
  readonly property string artPath: root.np && root.np.art_path ? root.np.art_path : ""

  ColorQuantizer {
    id: quantizer
    source: root.artPath ? "file://" + root.artPath : ""
    depth: 3
    rescaleSize: 64
  }

  // quantizer.colors is a list of QColor, not the string array Model.pickAccent
  // (node-tested, no QColor there) takes. String(qcolor) renders as
  // "#aarrggbb"; Model.normalizeHex already strips that alpha pair, so no
  // further conversion happens here.
  readonly property var quantizedHex: {
    var out = []
    for (var i = 0; i < quantizer.colors.length; i++) out.push(String(quantizer.colors[i]))
    return out
  }
  // Direction C: exactly ONE element in the whole popup takes its color from
  // the cover -- the seek fill below. Everything else, including the play
  // button, stays on the theme accent.
  readonly property color artAccent: root.useArtAccent
    ? Model.pickAccent(root.quantizedHex, String(Color.background))
    : Color.accent

  // Color.qml exposes background, foreground, accent, muted and the
  // per-surface roles (Color.bar.*, Color.popups.*) -- there is no
  // lightForeground/darkForeground/selection to bind to. Secondary and
  // tertiary text and inactive fills are alpha-blended off Color.foreground
  // here, the same technique PanelSeparator.qml uses for its own rule.
  readonly property color fgMid: Qt.rgba(Color.foreground.r, Color.foreground.g, Color.foreground.b, 0.72)
  readonly property color fgFaint: Qt.rgba(Color.foreground.r, Color.foreground.g, Color.foreground.b, 0.46)
  readonly property color rowHighlight: Qt.rgba(Color.foreground.r, Color.foreground.g, Color.foreground.b, 0.10)
  // The seek/volume track background, at the same 0.12 alpha PanelSeparator
  // uses for its own rule -- NOT Color.muted (#707880), a light blue-grey
  // that reads as a fully filled bar against the near-black panel and made
  // 0:00 look indistinguishable from playing at 100%. This must look empty
  // when it is empty.
  readonly property color trackEmpty: Qt.rgba(Color.foreground.r, Color.foreground.g, Color.foreground.b, 0.12)

  Service {
    id: service
  }

  // One ticking source for the whole widget. A binding that needs "now"
  // reads clock.now rather than calling Date.now() directly, which would
  // never re-evaluate since QML bindings only re-run when a property they
  // read changes.
  Timer {
    id: clock
    property real now: Date.now()
    interval: 1000
    running: root.opened || root.display.playing
    repeat: true
    onTriggered: now = Date.now()
  }

  // Spec 5.1: `reset` is "used when the popup closes, so a stale cursor is
  // never carried into the next session". Nothing was wired to the closing
  // edge, so the daemon session kept its path forever after the first search
  // -- which kept BrowsePane.hasContent true, which put the separator and the
  // whole pane into every subsequent popup open and silently undid R13/R14's
  // idle height. `opened` is Ui/Panel.qml's own readonly property; this is
  // the closing edge it exists for.
  onOpenedChanged: if (!root.opened) browsePane.resetPane()

  // BarIconButton, not a hand-rolled Item + MouseArea: it paints the glyph
  // through OpticalGlyph, which centers on the painted ink rather than the
  // monospace advance cell, and every icon-only bar widget in this shell
  // (audio, network, tray, headway, galley, colophon) uses it for that
  // reason. It extends WidgetButton, so bar/text/foreground/tooltipText/
  // onPressed all carry over unchanged.
  //
  // anchors.fill: parent is REQUIRED -- without it the button has no
  // geometry inside the Panel root and renders at zero size.
  //
  // No fixedWidth/fixedHeight override here, matching galley and colophon:
  // BarIconButton sizes itself from Style.bar.iconSlot, so the widget's
  // width in the bar is a constant that does not depend on what state.zone
  // reports.
  //
  // No album art here. An earlier version overlaid a cover-art thumbnail in
  // the glyph's corner, but at icon-slot resolution (a ~16-27px square) it
  // read as an indistinct smear, not art -- illegible rather than useful.
  // Model.artUrl still exists and is tested; the popup is where the art has
  // room to mean something.
  //
  // The glyph is now tonearm's PRODUCT icon (a record), not a transport mime.
  // Two things made that worth the swap. First, dropping the art badge left
  // the widget indistinguishable from its eighteen monochrome neighbours until
  // you memorised its position; a record restores that without the
  // unreadable-at-10px problem the art had. Second, the old glyph set meant a
  // STATUS here (paused => pause bars) and an ACTION in the popup (paused =>
  // offers play), so the same two glyphs read as opposites a hundred pixels
  // apart.
  //
  // Transport state is NOT lost -- it moves entirely into `foreground` below,
  // where it already lived: playing renders bright, everything else dim. Only
  // paused-vs-idle stops being distinguishable at a glance, and both were
  // already dim; the tooltip still names the difference.
  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.display.glyph
    tooltipText: Model.tooltipText(root.st)

    // Severity reaches the bar entirely through the glyph's color, same
    // convention as headway/galley/colophon. Not top-level Color.red /
    // Color.yellow -- those roles do not exist on Commons/Color.qml; the
    // severity colors live in Model.js (COLOR_ERROR, COLOR_WARN) for the
    // same reason headway carries its own. "ok" but not playing (paused or
    // idle) dims via root.dim, a Qt.darker() of the bar's own foreground,
    // rather than an alpha blend -- there is no Color.selection or
    // Color.lighterBackground here to blend toward.
    //
    // root.display.playing, not a hand-rolled `root.st.zone.state ===
    // "playing"` check: Model.js already derives the identical condition to
    // pick the glyph, and duplicating it here bypasses the one file this
    // codebase keeps test-free of that logic.
    foreground: {
      if (root.display.severity === "error") return Model.COLOR_ERROR
      if (root.display.severity === "warn") return Model.COLOR_WARN
      if (root.display.playing) return root.barForeground
      return root.dim
    }

    // BarIconButton's own signal is onPressed(which), not onClicked(mouse).
    // Middle-click is playpause without opening anything -- the one
    // transport action worth having without a popup (Task 16 adds the
    // popup content; this file only owns the bar button).
    onPressed: function (which) {
      if (which === Qt.MiddleButton) { service.send("playpause"); return }
      root.toggle()
    }
  }

  // KeyboardPanel, not contentWidth/contentHeight on the root -- Ui/Panel.qml
  // (the root here) has no such properties; those live on this child, and
  // KeyboardPanel has no toggle() and no visible of its own. The lifecycle
  // stays on root: `open: root.opened`, driven by button.onPressed above.
  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(400))
    contentHeight: panel.fittedContentHeight(contentColumn.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      // Gate on the pane's own editing flag, not on activeFocus: that is the
      // pattern the shell's own network panel uses for its passphrase field
      // (plugins/panels/network/Panel.qml:996, `blocked: root.passwordSsid !== ""`).
      // Unblocked while typing, every letter would be swallowed as a shortcut.
      blocked: browsePane.editing

      onMoveRequested: function (dx, dy) { browsePane.handleMove(dx, dy) }
      onActivateRequested: browsePane.handleActivate()

      // Esc backs out one level and only closes at the top (spec 7.2). One
      // stray Esc must not discard a whole navigation.
      onCloseRequested: { if (!browsePane.handleBack()) root.close() }

      // `/` is explicit; everything else printable starts a search SEEDED with
      // that character, so typing goes straight into the field without a
      // preparatory keystroke and without losing the first letter.
      //
      // There used to be a third branch here: `q` queued the selected row, and
      // because `q` is also the first letter of Queen it had to be resolved by
      // whether a row was selected. Dropping Queue from the UI deleted that
      // branch outright -- `q` is an ordinary search letter again.
      //
      // h j k l x X and Space never arrive here at all -- PanelKeyCatcher
      // consumes them for move/delete/activate before onTextKey is emitted --
      // so they cannot be recovered from this file. README and spec 7.2 say
      // so rather than promising "any letter".
      onTextKey: function (text) {
        if (text === "/") { browsePane.focusSearch(""); return }
        if (text && text.length === 1 && text >= " ") {
          browsePane.focusSearch(text)
        }
      }

      Column {
        id: contentColumn
        // Anchored left/right/top, never bottom: contentHeight above is bound
        // back to contentColumn.implicitHeight, so pinning the bottom too would
        // be a binding loop. Unanchored, the column takes its own implicitWidth
        // -- the widest child, an unwrapped title -- while the panel background
        // stays at contentWidth, and content would paint outside the panel.
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        // 10, not 13. With the transport folded into the card above there are
        // three blocks here instead of four, and the separators already do the
        // grouping this gap was over-doing.
        spacing: Style.space(10)

        // -- header --------------------------------------------------------
        // Named because the widget's identity should not depend on
        // recognising an icon in a row of eighteen. The shell's own
        // convention is a header that DOES something rather than a bare title
        // -- the audio panel pairs "Audio" with a power switch -- so the
        // right half carries state the popup could not previously show at
        // all.
        //
        // Healthy, that is the Core's name: which Core you are attached to
        // appears nowhere else in this popup. Unhealthy, it is the fault in
        // words, and this is the part that earns the height. Until now the
        // popup could not say WHY nothing was playing: the reason lived only
        // in the bar glyph's colour and its tooltip, and an open popup covers
        // the bar it would have to point at. `Model.headerStatus` is the same
        // function the tooltip uses for every unhealthy state, so the two can
        // never drift into different wordings for one fault.
        Item {
          width: parent.width
          height: headerName.implicitHeight

          Row {
            id: headerLeft
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(7)

            Text {
              // The same product icon the bar shows, so the popup and its
              // button read as one thing.
              text: Model.GLYPH_VINYL
              color: root.fgMid
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              anchors.verticalCenter: parent.verticalCenter
            }
            Text {
              id: headerName
              text: "Tonearm"
              color: root.fgMid
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              font.weight: Font.DemiBold
              font.letterSpacing: 0.6
            }
          }

          Text {
            // Anchored to BOTH edges rather than just the right one: elide
            // needs a bounded width, and "Enable tonearm in Roon -> Settings
            // -> Extensions" is far wider than the gap beside the name. Right
            // alignment then keeps it hugging the right edge whenever it is
            // short enough to fit, which is the healthy case.
            anchors.left: headerLeft.right
            anchors.leftMargin: Style.space(10)
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            horizontalAlignment: Text.AlignRight
            text: Model.headerStatus(root.st)
            // The same three-way severity split the bar button uses, so a
            // fault reads as a fault in both places at once.
            color: {
              if (root.display.severity === "error") return Model.COLOR_ERROR
              if (root.display.severity === "warn") return Model.COLOR_WARN
              return root.fgFaint
            }
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }
        }

        PanelSeparator { foreground: Color.foreground }

        Row {
          spacing: Style.space(14)
          width: parent.width

          Rectangle {
            width: root.artPx
            height: root.artPx
            radius: Style.space(6)
            color: Color.muted
            clip: true

            Image {
              anchors.fill: parent
              // The remote Core URL, NOT root.artPath -- that one is only for
              // ColorQuantizer above. Measured live: this URL loads fine
              // (status Ready, correctly sized); ColorQuantizer is the one that
              // cannot read it.
              source: root.display.showArt ? Model.artUrl(root.st, root.artPx * 2) : ""
              sourceSize.width: root.artPx * 2
              sourceSize.height: root.artPx * 2
              fillMode: Image.PreserveAspectCrop
              asynchronous: true
            }
          }

          // An Item with two ANCHORED groups, not one Column with a fixed
          // spacer between them. The text hangs off the top, the controls off
          // the bottom, and the whole thing is exactly as tall as the art -- so
          // the card is one block, flush on both edges, with no leftover
          // rectangle beside the art. That is the defect this whole redesign
          // started from, and a fixed spacer only ever approximates the fix:
          // measured live, tuning the gap by hand still left ~18 units of art
          // sticking out below the transport, and any font-size change would
          // have moved the number again.
          //
          // The Math.max is the guard on that: `artPx` is a raw pixel setting
          // (96-256) while every other measurement here scales with the theme's
          // base font size, so a large font against a small art size is the one
          // combination where the two groups could otherwise overlap. Then the
          // card grows instead, and the art is the short edge.
          Item {
            width: parent.width - root.artPx - Style.space(14)
            height: Math.max(root.artPx,
                             trackText.implicitHeight + controls.implicitHeight + Style.space(8))

            Column {
              id: trackText
              anchors.top: parent.top
              anchors.left: parent.left
              anchors.right: parent.right
              spacing: Style.space(3)

              Text {
                width: parent.width
                text: root.np ? (root.np.title || "Nothing playing") : "Nothing playing"
                color: Color.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.title
                font.weight: Font.DemiBold
                // wrapMode does not constrain a Text: a wrapping Text still
                // reports its full single-line implicitWidth. Elide instead.
                elide: Text.ElideRight
              }
              Text {
                width: parent.width
                text: root.np ? (root.np.artist || "") : ""
                color: root.fgMid
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                elide: Text.ElideRight
              }
              Text {
                width: parent.width
                text: root.np ? (root.np.album || "") : ""
                color: root.fgFaint
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                elide: Text.ElideRight
              }
            }

            Column {
              id: controls
              anchors.bottom: parent.bottom
              anchors.left: parent.left
              anchors.right: parent.right
              spacing: Style.space(6)
              visible: root.hasZone

              // -- seek ------------------------------------------------------
              // A bare track now, not a 22-unit Item carrying the time labels
              // underneath it. The labels moved down onto the transport row,
              // which is what freed the vertical room for the transport to come
              // up into this column at all.
              Rectangle {
                id: seekTrack
                width: parent.width
                height: Style.space(3)
                radius: height / 2
                color: root.trackEmpty

                Rectangle {
                  width: seekTrack.width * root.progress
                  height: parent.height
                  radius: parent.radius
                  color: root.artAccent          // the one art-derived element
                }

                // A CHILD of the track now rather than a sibling filling it.
                // Negative margins still expand a 3-unit track into a clickable
                // band; an item outside its parent's bounds is still hit-tested
                // because nothing on the path up to the panel sets `clip`.
                //
                // The bottom margin is deliberately shorter than the rest. At -8
                // this band reached into the transport row below and covered the
                // top of the play button. Paint order would in fact have resolved
                // that in the button's favour (it is a later sibling), but a
                // control whose hit box only works because of sibling ordering is
                // a trap for whoever next reorders this column.
                MouseArea {
                  anchors.fill: parent
                  anchors.leftMargin: -Style.space(8)
                  anchors.rightMargin: -Style.space(8)
                  anchors.topMargin: -Style.space(8)
                  anchors.bottomMargin: -Style.space(3)
                  enabled: root.length > 0
                  onClicked: function (mouse) {
                    // mouse.x is relative to THIS MouseArea's origin, not
                    // seekTrack's: the left margin above puts that origin
                    // Style.space(8) before the track's visual left edge.
                    // Subtract it back out, or a click at the visual start of the
                    // bar reports mouse.x as the margin width instead of 0 -- a
                    // several-second offset that only shows up as "clicking near
                    // the beginning doesn't go to the beginning."
                    var frac = Math.max(0, Math.min(1, (mouse.x - Style.space(8)) / seekTrack.width))
                    service.send("seek", Math.floor(frac * root.length))
                  }
                }
              }

              // -- transport + times -----------------------------------------
              // Inside the art's own column, not a full-width band below it. The
              // old layout left two empty regions that were really one mistake: a
              // dead rectangle beside the bottom of the art (this text column
              // filled only ~87 of the art's 118 units) and a 400-wide row
              // holding three small centred controls. Folding the transport into
              // the dead rectangle closes both at once, takes ~50 units off the
              // panel's height, and puts the buttons beside the seek bar and
              // times they actually act on.
              //
              // The times flank the controls on this line instead of sitting
              // under the track, which is what buys the vertical room for this
              // row to fit beside the art at all. Landing flush with the art is
              // the enclosing Item's anchoring, not this row's height.
              Item {
                width: parent.width
                height: Style.space(36)

                Text {
                  anchors.left: parent.left
                  anchors.verticalCenter: parent.verticalCenter
                  text: Model.formatTime(root.pos)
                  color: root.fgFaint
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }

                Row {
                  anchors.horizontalCenter: parent.horizontalCenter
                  anchors.verticalCenter: parent.verticalCenter
                  spacing: Style.space(18)

                  Text {
                    // Model.GLYPH_PREV, not a typed "⏮" -- plain Unicode media
                    // symbols (U+23EE here) carry emoji presentation in the
                    // deployed font and render as a colour block, not a
                    // monochrome glyph. Built with String.fromCodePoint in
                    // Model.js, same as the bar's own icon.
                    text: Model.GLYPH_PREV
                    color: root.fgMid
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.heading
                    // Row only manages x and leaves children top-aligned by
                    // default, so without this the ~20px glyph sits at the row's
                    // top while the 36px play circle spans the full row height --
                    // putting the circle's centre visibly below the glyph.
                    anchors.verticalCenter: parent.verticalCenter
                    MouseArea {
                      anchors.fill: parent
                      anchors.margins: -Style.space(6)
                      onClicked: service.send("previous")
                    }
                  }

                  Rectangle {
                    width: Style.space(36)
                    height: Style.space(36)
                    radius: width / 2
                    // Theme accent, not root.artAccent -- Direction C reserves
                    // the art-derived color for the seek fill alone.
                    color: Color.accent
                    Text {
                      anchors.centerIn: parent
                      // These two glyphs are an ACTION here: paused offers play.
                      // The bar no longer uses either one (it shows the product
                      // icon in every healthy state), which is what removed the
                      // old collision where the same glyph meant "is paused" in
                      // the bar and "press to play" in the popup.
                      text: root.playing ? Model.GLYPH_PAUSED : Model.GLYPH_PLAYING
                      color: Color.background
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.title
                    }
                    MouseArea { anchors.fill: parent; onClicked: service.send("playpause") }
                  }

                  Text {
                    text: Model.GLYPH_NEXT
                    color: root.fgMid
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.heading
                    anchors.verticalCenter: parent.verticalCenter
                    MouseArea {
                      anchors.fill: parent
                      anchors.margins: -Style.space(6)
                      onClicked: service.send("next")
                    }
                  }
                }

                Text {
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  text: Model.formatRemaining(root.pos, root.length)
                  color: root.fgFaint
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }
            }
          }
        }

        // -- volume ----------------------------------------------------------
        Row {
          width: parent.width
          spacing: Style.space(9)
          // A fixed-volume output reports no volume object; a slider there
          // would be a lie. Respects the showVolume setting too.
          visible: root.setting("showVolume", true) && root.volume !== null

          Text {
            // Model.GLYPH_VOLUME_MUTED/GLYPH_VOLUME_HIGH, built with
            // String.fromCodePoint in Model.js rather than a literal escape
            // here -- routed through the same constant-glyph convention as
            // every other Nerd Font icon in this file.
            text: root.volume && root.volume.muted ? Model.GLYPH_VOLUME_MUTED : Model.GLYPH_VOLUME_HIGH
            color: root.fgFaint
            font.family: root.fontFamily
            font.pixelSize: Style.font.subtitle
            // Row only manages x and leaves children top-aligned by default --
            // same fix already applied to the transport row's side glyphs
            // above, needed here too since this row's tallest child is the
            // volTrack Rectangle's own height plus the neighboring Text metrics.
            anchors.verticalCenter: parent.verticalCenter
            MouseArea {
              anchors.fill: parent
              anchors.margins: -Style.space(4)
              onClicked: service.send(root.volume && root.volume.muted ? "unmute" : "mute")
            }
          }

          Rectangle {
            id: volTrack
            width: parent.width - Style.space(52)
            height: Style.space(3)
            radius: height / 2
            anchors.verticalCenter: parent.verticalCenter
            color: root.trackEmpty

            Rectangle {
              width: volTrack.width * root.volumeFraction
              height: parent.height
              radius: parent.radius
              // root.dim, not Color.accent -- at 100 a full-width accent bar
              // was the brightest, heaviest element in the whole panel,
              // louder than the play button and far louder than the seek
              // bar (dark and empty at rest). Volume is secondary: readable,
              // but visibly quieter than the seek fill and the play button.
              color: root.dim
              Behavior on width { NumberAnimation { duration: 90 } }
            }

            MouseArea {
              anchors.fill: parent
              anchors.margins: -Style.space(8)
              onClicked: function (mouse) {
                // Same coordinate-frame correction as the seek MouseArea above:
                // mouse.x is relative to this MouseArea's own origin, which the
                // negative margin puts Style.space(8) before volTrack's left
                // edge.
                var frac = Math.max(0, Math.min(1, (mouse.x - Style.space(8)) / volTrack.width))
                service.send("volume", Model.volumeFromFraction(root.volume, frac))
              }
            }
          }

          Text {
            text: root.volume ? String(root.volume.value) : ""
            color: root.fgFaint
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            anchors.verticalCenter: parent.verticalCenter
          }
        }

        // -- zones -------------------------------------------------------
        Column {
          width: parent.width
          spacing: Style.space(2)

          PanelSeparator { foreground: Color.foreground }
          Item { width: 1; height: Style.space(6) }

          // The caption line carries the search hint on its right, which is
          // the whole fix for "nothing on screen says search exists"
          // (the closed "search was undiscoverable" follow-up) at a cost of
          // zero added height: this row already
          // existed and its right half was empty. It also sits directly above
          // where results appear, so the hint is next to its own output.
          //
          // An Item, not a Row: "ZONES" must stay hard left and the hint hard
          // right regardless of the panel's width.
          Item {
            width: parent.width
            height: zonesLabel.implicitHeight

            Text {
              id: zonesLabel
              anchors.left: parent.left
              text: "ZONES"
              color: root.fgFaint
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.letterSpacing: 0.9
            }

            Text {
              id: searchHint
              anchors.right: parent.right
              // baseline, not verticalCenter: the two labels differ in
              // letterSpacing and would otherwise sit a fraction of a pixel
              // apart on a line the eye reads as one.
              anchors.baseline: zonesLabel.baseline
              text: "/  search"
              color: root.fgFaint
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            // Clicking the hint does what it advertises. It is a hint first --
            // the keyboard is the real path -- but a label naming a key the
            // user cannot click is a smaller affordance than one they can.
            MouseArea {
              anchors.fill: searchHint
              anchors.margins: -Style.space(5)
              onClicked: browsePane.focusSearch("")
            }
          }

          Repeater {
            model: Model.zoneList(root.st)

            Rectangle {
              id: zoneRow
              required property var modelData
              width: parent.width
              height: Style.space(26)
              radius: Style.space(5)
              color: root.zone && root.zone.id === zoneRow.modelData.id
                     ? root.rowHighlight : "transparent"

              Row {
                anchors.left: parent.left
                anchors.leftMargin: Style.space(7)
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(8)

                Rectangle {
                  width: Style.space(5)
                  height: Style.space(5)
                  radius: width / 2
                  anchors.verticalCenter: parent.verticalCenter
                  color: zoneRow.modelData.state === "playing" ? Color.accent : Color.muted
                }
                Text {
                  text: zoneRow.modelData.name
                  color: root.fgMid
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                }
              }

              Text {
                anchors.right: parent.right
                anchors.rightMargin: Style.space(8)
                anchors.verticalCenter: parent.verticalCenter
                text: Model.isZonePinned(root.zone, zoneRow.modelData.id) ? "pinned" : ""
                color: root.fgFaint
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }

              MouseArea {
                anchors.fill: parent
                onClicked: {
                  var pinnedHere = Model.isZonePinned(root.zone, zoneRow.modelData.id)
                  // Clicking the already-pinned zone unpins it, so auto-follow
                  // is reachable without a second control.
                  if (pinnedHere) service.send("zone", "unpin")
                  else service.send("zone", "pin", zoneRow.modelData.id)
                }
              }

              // "Move the music here." Declared AFTER the row's MouseArea on
              // purpose: siblings are hit-tested in reverse declaration order,
              // so a glyph declared before it would be covered by the
              // full-row pin handler and never receive a click.
              //
              // Clicking the row still only changes which zone the widget
              // FOLLOWS. This is the one control that moves audio between
              // rooms, so it gets its own small target rather than sharing a
              // gesture with the view change -- and it never collides with the
              // "pinned" label above, because canTransferTo excludes the
              // followed zone, which is the only row that label appears on.
              Text {
                id: transferGlyph
                anchors.right: parent.right
                anchors.rightMargin: Style.space(8)
                anchors.verticalCenter: parent.verticalCenter
                visible: Model.canTransferTo(root.st, zoneRow.modelData.id)
                text: Model.GLYPH_TRANSFER
                // Lighting up on hover is what says "this is a separate
                // target", on a row whose whole surface is already clickable.
                color: transferArea.containsMouse ? Color.accent : root.fgFaint
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall

                MouseArea {
                  id: transferArea
                  anchors.fill: parent
                  // The glyph is ~11 units square in a 26-unit row; without
                  // this the target is small enough to miss, and missing means
                  // silently repinning instead.
                  anchors.margins: -Style.space(6)
                  hoverEnabled: true
                  // The popup deliberately stays open: the daemon repins to
                  // the destination, so the card above redraws as the new
                  // zone. That redraw is the confirmation the action worked.
                  onClicked: service.send("transfer", zoneRow.modelData.id)
                }
              }
            }
          }
        }

        PanelSeparator {
          width: parent.width
          // Bound to the same property BrowsePane uses for its own `visible`
          // (BrowsePane.qml's `hasContent`), so the separator and the pane
          // can never disagree about whether there is anything to show.
          visible: browsePane.hasContent
        }

        BrowsePane {
          id: browsePane
          width: parent.width
          service: service
          state: root.st
          fontFamily: root.fontFamily
          // Where the search field returns active focus. Without it the
          // TextField keeps focus after a search and every navigation key is
          // eaten by the text input instead of reaching the catcher.
          keyTarget: keyCatcher
        }
      }
    }
  }
}
