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

  // Where active focus goes when the search field gives it up: Panel.qml
  // passes its PanelKeyCatcher. Without this, focus is a one-way trip.
  // focusSearch() grabs it into the TextField and nothing ever handed it
  // back, so after submitting a search the pane's whole key map was dead:
  // Ui/TextField.qml inherits QQC2 TextField, i.e. it IS the QQuickTextInput
  // and it ACCEPTS the keys it understands. Enter re-ran the same search via
  // onAccepted, "q" typed a q, and Left/Right moved the text caret instead of
  // navigating. Clearing `editing` alone (which is all this used to do) only
  // unblocks the catcher -- it does not give the catcher back the focus it
  // needs to see a key at all. Task 11's nesting fix made IGNORED keys
  // propagate; it does nothing for keys the field accepts.
  property Item keyTarget: null

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
  //
  // `busy` and `path.length > 0` were added after `editing || hasResults ||
  // errorText.length > 0` alone left two states with nothing visible: (1) a
  // search in flight -- `search()` sets `editing = false` synchronously
  // before the reply repopulates rows/errorText, so without `busy` the pane
  // (and the field the user just typed into) would vanish for the
  // round-trip; (2) a genuine zero-result search -- an `ok` reply with no
  // rows leaves `errorText` cleared too, so without `path.length > 0` the
  // "No results" Text below (itself gated on `path.length > 0`, not on row
  // count, for exactly this reason) could never render, because its
  // ancestor would already be `visible: false`.
  readonly property bool hasContent: root.editing || root.hasResults || root.errorText.length > 0
                                      || root.busy || root.path.length > 0

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
  //
  // `seed` is the character that triggered this, and seeding it is what makes
  // README's "any letter opens the field and starts your query with it" true.
  // It was not: PanelKeyCatcher's onTextKey branch never sets
  // event.accepted, so the triggering key propagates onward -- to a field
  // that is not focused yet (the grab is deferred, above) and therefore never
  // receives it. Press `o`, type `ingo boingo`, and the field held
  // `ingo boingo`. Setting the text here rather than trying to replay the key
  // also keeps the deferred grab intact.
  //
  // Assigning only for a non-empty seed is deliberate: `/` passes "" and must
  // leave an in-progress query alone rather than wiping it.
  function focusSearch(seed) {
    root.editing = true
    if (seed && seed.length > 0) {
      field.text = seed
      field.cursorPosition = field.text.length
    }
    Qt.callLater(function () { field.forceActiveFocus() })
  }

  // The symmetric counterpart to focusSearch(). Must be called EVERYWHERE
  // `editing` is cleared -- clearing the flag without moving focus leaves the
  // TextField holding it and swallowing the keys the pane needs.
  function releaseSearch() {
    root.editing = false
    if (root.keyTarget) root.keyTarget.forceActiveFocus()
  }

  // Called when the popup closes (Panel.qml's onOpenedChanged). Spec 5.1:
  // `reset` exists "so a stale cursor is never carried into the next
  // session". Nothing called it except handleBack() at path.length === 1, so
  // after any search `path.length > 0` stayed true for the daemon's whole
  // lifetime -- `hasContent` with it, which silently undid R13/R14: the idle
  // popup kept its original height only until the user's first search. It
  // also left the previous session's rows and cursor on screen next time.
  //
  // releaseSearch() rather than a bare `editing = false`, to keep that
  // invariant in exactly one place; its forceActiveFocus() is a no-op on a
  // panel that is already closing (an invisible item cannot take focus) and
  // the KeyboardPanel re-focuses its focusTarget on the next open anyway.
  //
  // The reset goes through _send, so `busy` gates it like every other op --
  // rows and path therefore clear when the daemon's reply lands, not
  // optimistically here (see handleBack for why optimistic clearing loses to
  // a reply still in flight).
  function resetPane() {
    field.text = ""
    root.releaseSearch()
    _send(["reset"])
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
  //
  // Returns whether the request was actually SENT. handleBack() needs that:
  // returning true when `busy` had suppressed the send told Panel.qml the key
  // was consumed, so Esc was swallowed and the popup could not be closed from
  // the keyboard while a browse was in flight.
  function _send(args, after) {
    if (!root.service || root.busy) return false
    root.busy = true
    root.service.browse(args, function (reply) {
      root._apply(reply)
      if (after) after(reply)
    })
    return true
  }

  function search(term) {
    if (!term || term.length === 0) return
    root.cursor = -1
    // releaseSearch, not `editing = false`: the results are about to be
    // keyboard-navigable and the catcher cannot see a key it does not have
    // focus for.
    root.releaseSearch()
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
  // On a PLAY, the search clears and the popup stays open. This reverses spec
  // 7.3, which closed it. That made sense while Queue existed: playing was the
  // terminal action and queuing was the one you repeated, so closing on play
  // was "done, get out of the way". With Queue gone, Play Now is the ONLY
  // action -- so closing on it meant every selection ended the session and a
  // second one cost a reopen and a retype.
  //
  // Clearing rather than merely leaving the results up: the reset shrinks the
  // pane back to idle height, which is the visible confirmation that something
  // was picked, and it lands the popup in the same state a fresh open would --
  // no stale cursor pointing into a level the user has moved on from.
  //
  // Only when the daemon reports it actually PLAYED. Resetting unconditionally
  // would wipe the search the moment Enter descended into a category, throwing
  // away the very list the user was navigating.
  function handleActivate() {
    if (root.editing || root.cursor < 0) return
    _send(["activate", String(root.cursor), String(root.levelId)],
          function (reply) {
            // Model.activatePlayed, not the condition inline: this is the one
            // decision in the callback, and it is node-tested there.
            if (Model.activatePlayed(reply)) {
              // Safe from inside an `after` callback: _apply has already
              // cleared `busy`, so resetPane's own _send is not suppressed.
              root.resetPane()
            }
          })
  }

  function handleDescend() {
    if (root.editing || root.cursor < 0) return
    if (!root.rows[root.cursor].can_descend) return
    _send(["enter", String(root.cursor), String(root.levelId)])
  }

  // No handleQueue(). Play Now is the only action the UI offers, because the
  // popup has no way to SHOW a queue -- so queuing was a write-only control
  // whose effect the user could never see. Removing it also deleted the
  // `hasSelection` property and Panel.qml's context-sensitive `q` branch, the
  // trickiest piece of key handling in that file, which existed solely because
  // `q` is both the queue shortcut and the first letter of Queen.
  //
  // The daemon does not carry it either. Keeping a queue action there would
  // have left protocol surface with no consumer and no path to one, so the
  // whole action-selection argument went with it: `play` now takes an index
  // and a level, and Play Now is the only action browse.py invokes.

  // Returns true when it consumed the key. Panel.qml uses that to decide
  // whether Esc should close the popup instead (spec 7.2).
  function handleBack() {
    if (root.editing) { root.releaseSearch(); return true }
    if (root.path.length > 1) return _send(["back"])
    // At the top level, `reset` is the same op class as every other browse
    // call: gated by `busy`, and cleared only once the daemon's reply says
    // so, via _apply -- not optimistically here. An earlier draft cleared
    // rows/path/cursor locally and called service.browse() directly,
    // bypassing `busy`. Fast key-repeat during an in-flight activate/enter
    // then let that op's reply land afterward and silently overwrite the
    // reset (_apply has no idea a reset happened underneath it).
    //
    // Both branches now report whether the send actually happened rather than
    // an unconditional true: `busy` suppressing it means the key did nothing,
    // and claiming otherwise left Esc with no effect at all instead of
    // falling through to closing the popup.
    if (root.path.length === 1) return _send(["reset"])
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
      // releaseSearch, not `editing = false`: unblocking the catcher is
      // useless while this field still holds active focus.
      Keys.onEscapePressed: root.releaseSearch()
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
