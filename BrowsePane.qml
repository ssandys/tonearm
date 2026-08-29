import QtQuick
import Quickshell
import qs.Commons
import qs.Ui
// NOT QtQuick.Controls: it also exports a TextField, and which one wins is
// decided by import order rather than by anything visible at the use site.
// The shell's own qs.Ui TextField is the one that carries the theme. Nothing
// else here needs Controls -- ListView and Text are QtQuick.
// REQUIRED: this file calls Model.moveCursor and Model.rowArtUrl.
// Without this import each is a ReferenceError raised inside
// a signal handler -- invisible to qmllint, and it fails as a pane whose
// arrow keys silently do nothing.
import "Model.js" as Model

// Search + results. Owns all browse state; Panel.qml owns only the split
// layout. Extracted rather than folded into Panel.qml because that file is
// already 552 lines and this would put it past 800 (spec 6).
Item {
  id: root

  property var service: null
  property var state: null
  property int artPx: Style.space(30)
  property string fontFamily: ""

  // Browse state. `levelId` is the generation counter from spec 5.1.1 -- it
  // MUST accompany every index-addressed op, or a session reset between
  // render and keypress plays the wrong album silently.
  property var rows: []
  property int levelId: -1
  property var path: []
  property int cursor: -1
  property bool busy: false
  property string errorText: ""
  property bool editing: false

  readonly property int rowCount: rows.length
  readonly property bool hasResults: rows.length > 0

  // Single source of truth for "does this pane have anything to show right
  // now" -- bound by both this Item's own `visible` below and Panel.qml's
  // PanelSeparator, so the two can never disagree about whether the popup
  // should be showing the separator/pane pair (spec 7.1). A Column skips an
  // invisible child entirely, including the spacing that would otherwise be
  // reserved before it; a visible-but-zero-height pane would still cost that
  // spacing, which is the defect this property closes.
  readonly property bool hasContent: root.editing || root.hasResults || root.errorText.length > 0

  signal playStarted()
  signal closeRequested()

  implicitHeight: column.implicitHeight
  // Idle (no rows, no error, not editing) must contribute NOTHING to the
  // popup -- not just zero height. Panel.qml:201 binds contentHeight back to
  // contentColumn.implicitHeight, so a visible-but-empty pane would still
  // reserve contentColumn's inter-item spacing before it, growing the popup
  // permanently for every user even if they never search.
  visible: root.hasContent

  // Sets `editing` synchronously so `hasContent`/`visible` above flip true in
  // this same pass, then defers the actual focus grab. This mirrors
  // Panel.qml's own KeyboardPanel, which schedules its focusTarget's
  // forceActiveFocus() through Qt.callLater for the identical reason: an
  // item cannot take active focus while it (or an ancestor) is still
  // invisible, and Qt raises no warning when that silently fails. Calling
  // field.forceActiveFocus() in the same synchronous tick that flips `root`
  // from invisible to visible would race the layout pass that actually maps
  // the field, so the grab must run after that pass completes instead.
  function focusSearch() {
    root.editing = true
    Qt.callLater(function () { field.forceActiveFocus() })
  }

  function _apply(reply) {
    root.busy = false
    if (!reply) { root.errorText = "tonearmd is not answering"; return }
    if (reply.ok === false) {
      // A stale reply is not a user-visible failure: the screen was simply out
      // of date. Re-render and discard the keystroke rather than replaying it
      // against a level the user never saw (spec 5.2).
      root.errorText = reply.error === "stale" ? "" : (reply.message || "error")
      if (reply.rows !== undefined) root._applyLevel(reply)
      return
    }
    root.errorText = ""
    root._applyLevel(reply)
  }

  // `level_id` increments on every level change (search/enter/back/reset,
  // and an activate that resolves to a descend rather than a play) and stays
  // put across a page reload or a play, which return the SAME level
  // unchanged (spec 5.1.1, 5.3 "level_id is strictly increasing... on every
  // level change"). A level change puts the cursor on the first row --
  // descending into an album should not carry over whatever index the
  // previous level happened to have the cursor on. page/play leave the
  // cursor where the user had it, merely clamped to the (possibly shorter)
  // new row count.
  function _applyLevel(reply) {
    var levelChanged = reply.level_id !== root.levelId
    root.rows = reply.rows || []
    root.levelId = reply.level_id
    root.path = reply.path || []
    root.cursor = Model.moveCursor(levelChanged ? -1 : root.cursor, 0, root.rows.length)
  }

  // `after` runs once the reply has been applied, so a caller can react to
  // what the daemon actually did rather than to what it hoped would happen.
  function _send(args, after) {
    if (!root.service || root.busy) return
    root.busy = true
    root.service.browse(args, function (reply) {
      root._apply(reply)
      if (after) after(reply)
    })
  }

  function search(term) {
    if (!term || term.length === 0) return
    root.cursor = -1
    root.editing = false
    _send(["search", term])
  }

  // Horizontal movement is navigation, not cursor movement: right descends,
  // left goes back (spec 7.2). PanelKeyCatcher delivers both axes through one
  // signal, so they are separated here rather than in Panel.qml.
  function handleMove(dx, dy) {
    if (root.editing) return
    if (dx > 0) { root.handleDescend(); return }
    if (dx < 0) { root.handleBack(); return }
    if (dy === 0) return
    root.cursor = Model.moveCursor(root.cursor, dy, root.rows.length)
    list.positionViewAtIndex(root.cursor, ListView.Contain)
  }

  // Enter: activate -- plays if playable, descends if not. The widget must NOT
  // decide which: spec 2.4 measured that a category row and an album row are
  // both hint "list" and indistinguishable before descending, so any rule here
  // would be a guess that fails on art-less albums.
  //
  // The popup closes only when the daemon reports it actually PLAYED
  // (spec 7.3). Emitting playStarted() unconditionally would close the popup
  // on Enter over a category -- which descends -- so the user would see the
  // right thing happen and the window vanish on top of it.
  function handleActivate() {
    if (root.editing || root.cursor < 0) return
    _send(["activate", String(root.cursor), String(root.levelId)],
          function (reply) {
            if (reply && reply.ok !== false && reply.played === true) {
              root.playStarted()
            }
          })
  }

  function handleDescend() {
    if (root.editing || root.cursor < 0) return
    if (!root.rows[root.cursor].can_descend) return
    _send(["enter", String(root.cursor), String(root.levelId)])
  }

  function handleQueue() {
    if (root.editing || root.cursor < 0) return
    _send(["play", String(root.cursor), "queue", String(root.levelId)])
  }

  // Returns true when it consumed the key. Panel.qml uses that to decide
  // whether Esc should close the popup instead (spec 7.2).
  function handleBack() {
    if (root.editing) { root.editing = false; return true }
    if (root.path.length > 1) { _send(["back"]); return true }
    // At the top level, `reset` is the same op class as every other browse
    // call: gated by `busy`, and cleared only once the daemon's reply says
    // so, via _apply -- not optimistically here. An earlier draft cleared
    // rows/path/cursor locally and called service.browse() directly,
    // bypassing `busy`. Fast key-repeat during an in-flight activate/enter
    // then let that op's reply land afterward and silently overwrite the
    // reset (_apply has no idea a reset happened underneath it).
    if (root.path.length === 1) { _send(["reset"]); return true }
    return false
  }

  Column {
    id: column
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.top: parent.top
    spacing: Style.space(8)

    TextField {
      id: field
      width: parent.width
      placeholderText: "Search library"
      onAccepted: root.search(text)
      // Esc leaves the field without closing the popup; Panel.qml's key
      // catcher is blocked while this has focus, so it never sees this key.
      Keys.onEscapePressed: root.editing = false
      onActiveFocusChanged: if (activeFocus) root.editing = true
    }

    Text {
      width: parent.width
      visible: root.path.length > 0
      text: "‹  " + root.path.join("  ›  ")
      color: Color.muted
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      elide: Text.ElideRight
    }

    Text {
      width: parent.width
      visible: root.errorText.length > 0
      text: root.errorText
      color: Model.COLOR_ERROR
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      wrapMode: Text.WordWrap
    }

    Text {
      width: parent.width
      visible: !root.busy && root.path.length > 0 && root.rows.length === 0
               && root.errorText.length === 0
      text: "No results"
      color: Color.muted
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }

    ListView {
      id: list
      width: parent.width
      // Fixed maximum so the popup grows once and then stops, rather than
      // tracking result count into an unusable column (spec 7.1).
      height: Math.min(contentHeight, Style.space(8 * 38))
      visible: root.rows.length > 0
      clip: true
      model: root.rows
      boundsBehavior: Flickable.StopAtBounds

      delegate: Rectangle {
        width: list.width
        height: Style.space(38)
        // Util.alpha(Color.foreground, 0.08) is the shell's own selected-row
        // colour (Ui/ConfirmDialog.qml:15). Color.surfaceVariant does NOT exist.
        color: index === root.cursor ? Util.alpha(Color.foreground, 0.08) : "transparent"
        radius: Style.space(4)

        Row {
          anchors.fill: parent
          anchors.leftMargin: Style.space(6)
          anchors.rightMargin: Style.space(6)
          spacing: Style.space(9)

          Rectangle {
            anchors.verticalCenter: parent.verticalCenter
            width: root.artPx
            height: root.artPx
            radius: Style.space(3)
            color: Color.muted
            clip: true
            visible: modelData.image_key !== null
                     && modelData.image_key !== undefined

            Image {
              anchors.fill: parent
              source: Model.rowArtUrl(root.state, modelData, root.artPx * 2)
              sourceSize.width: root.artPx * 2
              sourceSize.height: root.artPx * 2
              fillMode: Image.PreserveAspectCrop
              asynchronous: true
            }
          }

          Column {
            anchors.verticalCenter: parent.verticalCenter
            width: parent.width - root.artPx - Style.space(15)
            spacing: Style.space(1)

            Text {
              width: parent.width
              // Defended at the use site even though the daemon guarantees a
              // non-null title/subtitle (spec 5.3) -- same idiom as
              // Panel.qml:252's `root.np.title || "Nothing playing"`. A null
              // reaching `.length` below would throw inside a property
              // binding, which Qt swallows silently, leaving the binding
              // stale with no visible error anywhere.
              text: modelData.title || ""
              color: Color.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              elide: Text.ElideRight
            }

            Text {
              width: parent.width
              visible: (modelData.subtitle || "").length > 0
              text: modelData.subtitle || ""
              color: Color.muted
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
            }
          }
        }

        MouseArea {
          anchors.fill: parent
          onClicked: { root.cursor = index; root.handleActivate() }
        }
      }
    }
  }
}
