import QtQuick
import Quickshell
import qs.Commons
import qs.Commons as Commons
import qs.Ui
// NOT QtQuick.Controls: it also exports a TextField, and which one wins is
// decided by import order rather than by anything visible at the use site.
// The shell's own qs.Ui TextField is the one that carries the theme. Nothing
// else here needs Controls -- ListView and Text are QtQuick.
// REQUIRED: this file calls Model.moveCursor and Model.rowArtSource.
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

  // Which of the daemon's browse cursors this pane drives. Panel.qml derives
  // it per bar surface, because `service` is now a singleton shared by every
  // surface while the rows, cursor and path below are emphatically not: two
  // panes on one key means navigating on one monitor re-renders the other.
  property string session: ""
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

  // Row art the daemon delivered AFTER a level's first reply, keyed by
  // image_key. tonearmd fetches row thumbnails in the background (art.py,
  // RowArtCache) and waits only for the first screenful before replying, so
  // the rest arrive through a few `current` re-reads. Kept apart from `rows`
  // so filling in art never resets the ListView's model -- and with it the
  // scroll position and the delegates already drawn.
  property var artMap: ({})
  property int artRetries: 0

  property int totalCount: 0
  property int pageOffset: 0
  property int viewGeneration: 0
  property int requestNumber: 0
  property var activeRequest: null
  property var navParents: []
  property int playSequence: 0
  // play_now here is only the shared row/output gate. All three actions use
  // it; the clicked control sends its own token. One row, never a selection.
  readonly property bool canPlaySelection: !root.editing && !root.busy && !!Model.browsePlayRequest(
    root.state, root.rows[root.cursor], root.cursor, root.levelId, "availability", "play_now")
  readonly property string contextKey: Model.browseContext(root.state, root.session)
  readonly property var paging: Model.browsePaging(root.pageOffset, root.rows.length, root.totalCount)
  onContextKeyChanged: root.invalidateView()

  function invalidateView() {
    root.viewGeneration++
    root.activeRequest = null
    root.busy = false
    root.rows = []
    root.path = []
    root.levelId = -1
    root.cursor = -1
    root.totalCount = 0
    root.pageOffset = 0
    root.errorText = ""
    root.navParents = []
    root.artMap = ({})
    root.artRetries = 0
    artTimer.stop()
  }

  function openLibrary() {
    if (!Model.browseActionsSupported(root.state) || root.state.status !== "ok") return
    root.invalidateView()
    root.releaseSearch()
    root._send(["library"])
  }

  function changePage(direction, desiredCursor) {
    if (root.busy) return false
    var offset = direction > 0 ? root.paging.next : root.paging.previous
    if (offset === null || root.levelId < 0) return false
    var op = Model.browseNavRoute(root.state, "page")
    return root._send(op === "page_nav" ? [op, String(offset), String(root.levelId)] : [op, String(offset)], null, desiredCursor)
  }

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
  // Closing invalidates the view before sending reset. Older callbacks carry
  // the discarded generation and cannot restore rows into the next popup.
  function resetPane() {
    field.text = ""
    root.releaseSearch()
    // Invalidate before reset, even if an older read is still in flight.
    root.invalidateView()
    root._send(["reset"])
  }

  function _apply(reply, args, desiredCursor) {
    root.busy = false
    if (!reply) { root.invalidateView(); root.errorText = "tonearmd is not answering"; return false }
    if (reply.ok === false) {
      root.errorText = reply.error === "stale" ? "" : (reply.message || "error")
      if (reply.rows === undefined) {
        var message = root.errorText
        root.invalidateView()
        root.errorText = message
        return false
      }
    } else root.errorText = ""
    var next = Model.browsePageForRequest(reply, args, root, desiredCursor)
    if (!next) {
      root.invalidateView()
      root.errorText = "Library view changed or returned an invalid page; reload Library."
      return false
    }
    if (reply.ok === false) root.navParents = []
    root._applyLevel(reply)
    root.totalCount = next.count
    root.pageOffset = next.offset
    root.cursor = next.cursor
    Qt.callLater(function() { if (root.cursor >= 0) list.positionViewAtIndex(root.cursor, ListView.Contain) })
    return true
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
    root.artMap = Model.mergeRowArt(root.artMap, root.rows)
    if (levelChanged) root.artRetries = 0
    root._scheduleArt()
  }

  function _scheduleArt() {
    if (root.artRetries < Model.ROW_ART_RETRIES && Model.rowArtPending(root.rows, root.artMap))
      artTimer.restart()
    else
      artTimer.stop()
  }

  // Deliberately NOT through _send: it must not set `busy` (which would
  // swallow a keypress landing meanwhile) and its reply must not replace
  // `rows`. Only the art map is read from it, and only for the level still
  // on screen. A daemon older than 0.14.2 refuses `current` with ok:false,
  // which ends the retries quietly -- its rows simply keep their placeholder.
  function _refreshArt() {
    if (!root.service || !root.visible) return
    root.artRetries += 1
    var level = root.levelId
    root.service.browse(root.session, ["current"], function (reply) {
      if (!reply || reply.ok === false || reply.level_id !== level
          || level !== root.levelId) return
      root.artMap = Model.mergeRowArt(root.artMap, reply.rows)
      root._scheduleArt()
    })
  }

  Timer {
    id: artTimer
    interval: Model.ROW_ART_RETRY_MS
    repeat: false
    onTriggered: root._refreshArt()
  }

  // `after` runs once the reply has been applied, so a caller can react to
  // what the daemon actually did rather than to what it hoped would happen.
  //
  // Returns whether the request was actually SENT. handleBack() needs that:
  // returning true when `busy` had suppressed the send told Panel.qml the key
  // was consumed, so Esc was swallowed and the popup could not be closed from
  // the keyboard while a browse was in flight.
  function _send(args, after, desiredCursor) {
    if (!root.service || root.busy) return false
    var token = {view:root.viewGeneration, request:++root.requestNumber, context:root.contextKey}
    root.activeRequest = token
    var activatedParent = args[0] === "activate" ? {path:root.path.slice(), offset:root.pageOffset, cursor:root.cursor} : null
    root.busy = true
    root.errorText = ""
    root.service.browse(root.session, args, function (reply) {
      if (!Model.browseReplyCurrent(token, root.activeRequest, root.viewGeneration, root.contextKey)) return
      root.activeRequest = null
      if (args[0] === "play_selected") {
        var expected = {zone_id:args[3], request_id:args[4], action:args[5]}
        var message = Model.browsePlayReplyText(expected, reply)
        if (!Model.validBrowsePlayReply(expected,reply) || reply.rows === undefined) root.invalidateView()
        else { root._apply(reply,args); root.navParents = [] }
        root.busy = false
        root.errorText = message
        return
      }
      var accepted = root._apply(reply, args, desiredCursor)
      if (accepted && activatedParent && reply.ok !== false && !Model.activatePlayed(reply) &&
          JSON.stringify(root.path.slice(0,-1)) === JSON.stringify(activatedParent.path))
        root.navParents = root.navParents.concat([activatedParent]).slice(-64)
      if (accepted && after) after(reply)
    })
    return true
  }

  function search(term) {
    if (!term || term.length === 0) return
    root.invalidateView()
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
    if (root.editing || root.busy) return
    if (dx > 0) { root.handleDescend(); return }
    if (dx < 0) { root.handleBack(); return }
    if (dy === 0) return
    if (dy > 0 && root.cursor === root.rows.length-1 && root.paging.next !== null) { root.changePage(1,0); return }
    if (dy < 0 && root.cursor === 0 && root.paging.previous !== null) { root.changePage(-1,-2); return }
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

  function handlePlay(action) {
    if (root.busy || root.editing) return false
    if (action !== "play_now" && action !== "play_next" && action !== "add_to_queue") {
      root.errorText = "This browse action is not supported."
      return false
    }
    var rid = "browse-" + Date.now().toString(36) + "-" + (++root.playSequence)
    var request = Model.browsePlayRequest(root.state,root.rows[root.cursor],root.cursor,root.levelId,rid,action)
    if (!request) { root.errorText = "Choose an available output and a playable row first."; return false }
    return root._send(["play_selected",String(request.index),String(request.level_id),request.zone_id,request.request_id,request.action])
  }

  function handleDescend() {
    if (root.editing || root.cursor < 0) return
    var row = root.rows[root.cursor]
    if (Model.browseActionsSupported(root.state) && !Model.canNavigateRow(row)) {
      root.errorText = "This row requires search or playback; navigation only here."
      return
    }
    if (!row.can_descend) return
    var parent = {path:root.path.slice(), offset:root.pageOffset, cursor:root.cursor}
    _send([Model.browseNavRoute(root.state, "enter"), String(root.cursor), String(root.levelId)],
      function(reply) {
        if (reply.ok !== false) root.navParents = root.navParents.concat([parent]).slice(-64)
      })
  }

  // Returns true when it consumed the key. Panel.qml uses that to decide
  // whether Esc should close the popup instead (spec 7.2).
  function handleBack() {
    if (root.editing) { root.releaseSearch(); return true }
    if (root.path.length > 1) {
      var parent = root.navParents.length ? root.navParents[root.navParents.length-1] : null
      if (parent && JSON.stringify(parent.path) !== JSON.stringify(root.path.slice(0,-1))) parent = null
      var op = Model.browseNavRoute(root.state, "back")
      return _send(op === "back_nav" ? [op, String(parent ? parent.offset : 0), String(root.levelId)] : [op],
        function(reply) { if (reply.ok !== false) root.navParents = root.navParents.slice(0,-1) }, op === "back_nav" && parent ? parent.cursor : -1)
    }
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
      textFormat: Text.PlainText
      width: parent.width
      visible: root.busy
      text: "Loading library\u2026"
      color: Commons.Color.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }

    Text {
      textFormat: Text.PlainText
      width: parent.width
      visible: root.path.length > 0
      text: "‹  " + root.path.join("  ›  ")
      color: Commons.Color.muted
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      elide: Text.ElideRight
    }

    Text {
      textFormat: Text.PlainText
      width: parent.width
      visible: root.errorText.length > 0
      text: root.errorText
      color: Model.COLOR_ERROR
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      wrapMode: Text.WordWrap
    }

    Text {
      textFormat: Text.PlainText
      width: parent.width
      visible: !root.busy && root.path.length > 0 && root.rows.length === 0
               && root.errorText.length === 0
      text: "No results"
      color: Commons.Color.muted
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
      enabled: !root.busy
      opacity: enabled ? 1 : 0.5
      model: root.rows
      boundsBehavior: Flickable.StopAtBounds

      delegate: Rectangle {
        width: list.width
        height: Style.space(38)
        // Util.alpha(Commons.Color.foreground, 0.08) is the shell's own selected-row
        // colour (Ui/ConfirmDialog.qml:15). Commons.Color.surfaceVariant does NOT exist.
        color: index === root.cursor ? Util.alpha(Commons.Color.foreground, 0.08) : "transparent"
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
            color: Commons.Color.muted
            clip: true
            visible: modelData.image_key !== null
                     && modelData.image_key !== undefined

            // The daemon's cached thumbnail, never a Core URL (marketplace
            // security review, 2026-10-31). "" until it lands, and the muted
            // square behind is what shows meanwhile -- or for good, if the
            // fetch failed or the file will not decode.
            Image {
              anchors.fill: parent
              source: Model.rowArtSource(modelData, root.artMap)
              sourceSize.width: root.artPx * 2
              sourceSize.height: root.artPx * 2
              fillMode: Image.PreserveAspectCrop
              asynchronous: true
              visible: status === Image.Ready
            }
          }

          Column {
            anchors.verticalCenter: parent.verticalCenter
            width: parent.width - root.artPx - Style.space(15)
            spacing: Style.space(1)

            Text {
              textFormat: Text.PlainText
              width: parent.width
              // Defended at the use site even though the daemon guarantees a
              // non-null title/subtitle (spec 5.3) -- same idiom as
              // Panel.qml:252's `root.np.title || "Nothing playing"`. A null
              // reaching `.length` below would throw inside a property
              // binding, which Qt swallows silently, leaving the binding
              // stale with no visible error anywhere.
              text: modelData.title || ""
              color: Commons.Color.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              elide: Text.ElideRight
            }

            Text {
              textFormat: Text.PlainText
              width: parent.width
              visible: (modelData.subtitle || "").length > 0
              text: modelData.subtitle || ""
              color: Commons.Color.muted
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
    Text {
      textFormat: Text.PlainText
      width: parent.width
      visible: root.rows.length > 0 && !Model.browseActionsSupported(root.state)
      text: "Selected actions and Library shortcut require an updated daemon. Search, arrows and Enter remain available."
      wrapMode: Text.WordWrap
      color: Commons.Color.muted
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }

    Grid {
      id: actionRow
      objectName: "browseActions"
      width: parent.width
      visible: root.rows.length > 0
      spacing: Style.space(8)
      // Reflow rather than elide distinct actions into identical "Play …".
      columns: width >= 3 * (Math.max(playNowMetrics.width, playNextMetrics.width, actionLabelMetrics.width) + Style.space(12)) + 2 * spacing ? 3 : 1

      TextMetrics {
        id: playNowMetrics
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        text: "Play Now"
      }

      TextMetrics {
        id: playNextMetrics
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        text: "Play Next"
      }

      TextMetrics {
        id: actionLabelMetrics
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        text: "Add to Queue"
      }

      Repeater {
        model: [ {label: "Play Now", action: "play_now"},
                 {label: "Play Next", action: "play_next"},
                 {label: "Add to Queue", action: "add_to_queue"} ]
        delegate: Text {
          required property var modelData
          objectName: modelData.action
          width: Math.max(0, (actionRow.width - (actionRow.columns - 1) * actionRow.spacing) / actionRow.columns)
          textFormat: Text.PlainText
          text: modelData.label
          horizontalAlignment: Text.AlignHCenter
          wrapMode: Text.Wrap
          leftPadding: Style.space(6)
          rightPadding: Style.space(6)
          topPadding: Style.space(6)
          bottomPadding: Style.space(6)
          color: enabled ? Commons.Color.foreground : Commons.Color.muted
          enabled: root.canPlaySelection
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          MouseArea { anchors.fill: parent; onClicked: root.handlePlay(parent.modelData.action) }
        }
      }
    }

    Item {
      id: pager
      width: parent.width
      height: Math.max(previousLabel.implicitHeight, pageLabel.implicitHeight, nextLabel.implicitHeight) + Style.space(8)
      visible: root.path.length > 0
      Text {
        id: previousLabel
        textFormat: Text.PlainText
        text: "\u2039 Previous"
        anchors.left: parent.left
        width: Math.min(implicitWidth, Math.max(0, (parent.width - Style.space(8)) / 2))
        elide: Text.ElideRight
        color: enabled ? Commons.Color.foreground : Commons.Color.muted
        enabled: !root.busy && root.paging.previous !== null
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        MouseArea { anchors.fill: parent; onClicked: root.changePage(-1) }
      }
      Text {
        id: pageLabel
        textFormat: Text.PlainText
        text: root.paging.label
        anchors.horizontalCenter: parent.horizontalCenter
        // A centered label must reserve the larger side width on BOTH sides.
        width: Math.min(implicitWidth, Math.max(0, parent.width - 2 * Math.max(previousLabel.width, nextLabel.width) - Style.space(16)))
        visible: width >= Style.space(32)
        horizontalAlignment: Text.AlignHCenter
        elide: Text.ElideRight
        color: Commons.Color.muted
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }
      Text {
        id: nextLabel
        textFormat: Text.PlainText
        text: "Next \u203a"
        anchors.right: parent.right
        width: Math.min(implicitWidth, Math.max(0, (parent.width - Style.space(8)) / 2))
        horizontalAlignment: Text.AlignRight
        elide: Text.ElideRight
        color: enabled ? Commons.Color.foreground : Commons.Color.muted
        enabled: !root.busy && root.paging.next !== null
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        MouseArea { anchors.fill: parent; onClicked: root.changePage(1) }
      }
    }
  }
}
