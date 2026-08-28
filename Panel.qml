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
}
