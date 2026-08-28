import QtQuick
import Quickshell
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Root is Ui/Panel.qml's `Panel`, NOT `PanelBase` (does not exist here) and
// NOT `BarWidget` (no open/close/toggle lifecycle). See the plan's "Verified
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
  readonly property string barIcon: Model.GLYPH_IDLE
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
  // The bar button stays a single static Nerd Font glyph (play/pause/idle/
  // alert), which is legible at this size and is what actually carries the
  // state. Model.artUrl still exists and is tested; the popup (Task 16) is
  // where the art has room to mean something.
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
      onCloseRequested: root.close()
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
      spacing: Style.space(13)

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

        Column {
          width: parent.width - root.artPx - Style.space(14)
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

          Item { width: 1; height: Style.space(6) }

          // -- seek ------------------------------------------------------
          Item {
            width: parent.width
            height: Style.space(22)
            visible: root.hasZone

            Rectangle {
              id: seekTrack
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.top: parent.top
              height: Style.space(3)
              radius: height / 2
              color: root.trackEmpty

              Rectangle {
                width: seekTrack.width * root.progress
                height: parent.height
                radius: parent.radius
                color: root.artAccent          // the one art-derived element
              }
            }

            MouseArea {
              anchors.fill: seekTrack
              anchors.margins: -Style.space(8)
              enabled: root.length > 0
              onClicked: function (mouse) {
                // mouse.x is relative to THIS MouseArea's origin, not
                // seekTrack's: the negative margin above expands the
                // MouseArea Style.space(8) past the track on every side, so
                // its origin sits that far before the track's visual left
                // edge. Subtract the margin back out, or a click at the
                // visual start of the bar reports mouse.x as the margin
                // width instead of 0 -- a several-second offset that only
                // shows up as "clicking near the beginning doesn't go to
                // the beginning."
                var frac = Math.max(0, Math.min(1, (mouse.x - Style.space(8)) / seekTrack.width))
                service.send("seek", Math.floor(frac * root.length))
              }
            }

            Text {
              anchors.left: parent.left
              anchors.top: seekTrack.bottom
              anchors.topMargin: Style.space(5)
              text: Model.formatTime(root.pos)
              color: root.fgFaint
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
            Text {
              anchors.right: parent.right
              anchors.top: seekTrack.bottom
              anchors.topMargin: Style.space(5)
              text: Model.formatRemaining(root.pos, root.length)
              color: root.fgFaint
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }
        }
      }

      // -- transport -----------------------------------------------------
      Row {
        anchors.horizontalCenter: parent.horizontalCenter
        spacing: Style.space(20)
        visible: root.hasZone

        Text {
          // Model.GLYPH_PREV, not a typed "⏮" -- plain Unicode media symbols
          // (U+23EE here) carry emoji presentation in the deployed font and
          // render as a colour block, not a monochrome glyph. Built with
          // String.fromCodePoint in Model.js, same as the bar's own glyphs.
          text: Model.GLYPH_PREV
          color: root.fgMid
          font.family: root.fontFamily
          font.pixelSize: Style.font.heading
          // Row only manages x and leaves children top-aligned by default,
          // so without this the ~20px glyph sits at the row's top while the
          // 36px play circle spans the full row height -- putting the
          // circle's centre visibly below the glyph.
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
          // Theme accent, not root.artAccent -- Direction C reserves the
          // art-derived color for the seek fill alone.
          color: Color.accent
          Text {
            anchors.centerIn: parent
            // Model.GLYPH_PAUSED/GLYPH_PLAYING, the exact glyphs the bar
            // button already uses to pick the same state -- not a re-typed
            // "⏸"/"▶", for the same emoji-presentation reason as above.
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

        Text {
          text: "ZONES"
          color: root.fgFaint
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          font.letterSpacing: 0.9
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
          }
        }
      }
    }
  }
}
