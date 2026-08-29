import QtQuick
import Quickshell
import Quickshell.Io
// REQUIRED: this file calls Model.nextRetryDelay. Without this import that is
// a ReferenceError raised inside a signal handler -- invisible to qmllint,
// and it fails as a relay that never restarts.
import "Model.js" as Model

// I/O only. Everything decidable (formatting, severity, glyph choice) lives
// in Model.js so it can be tested under node; this file exists to be as
// small as the unverifiable surface allows.
//
// Panel.qml (Task 15) binds to this and renders; it holds no display state
// of its own -- same split as headway/colophon/galley's own Service.qml.
Item {
  id: root

  property string ctlPath: ""
  // Last payload from the daemon, or null when the relay has never spoken
  // (startup, or tonearmd down).
  property var state: null
  // Wall-clock ms at which `state` arrived. Model.position() interpolates
  // from this, so the seek bar never depends on a cross-process clock.
  property real receivedAt: 0
  readonly property bool connected: state !== null

  // Backoff step, reset to 0 on every successfully parsed line so a long
  // healthy connection does not leave the NEXT reconnect waiting 30s.
  property int _attempt: 0

  Process {
    id: relay
    running: false
    command: [root.ctlPath, "subscribe"]
    stdout: SplitParser {
      onRead: function (line) {
        if (!line || line.length === 0) return
        var parsed = null
        try {
          parsed = JSON.parse(line)
        } catch (e) {
          // A partial line is not worth tearing the connection down for.
          console.warn("tonearm: unparseable state line")
          return
        }
        root._attempt = 0
        root.receivedAt = Date.now()
        root.state = parsed
      }
    }

    // A failed spawn never emits exited() -- confirmed live: the process
    // goes straight to running=false without ever passing through true --
    // so onRunningChanged is the only drain signal that covers both a failed
    // spawn and a normal exit (tonearmctl subscribe exits the instant
    // tonearmd is down).
    onRunningChanged: {
      if (!relay.running) {
        root.state = null
        // Cleared alongside state, not left stale: the two are documented as
        // a pair (receivedAt is the arrival stamp for `state`), and a null
        // state with a stale receivedAt is a latent inconsistency even
        // though every current reader of receivedAt is gated behind a
        // non-null zone.
        root.receivedAt = 0
        backoff.interval = Model.nextRetryDelay(root._attempt)
        root._attempt = root._attempt + 1
        backoff.restart()
      }
    }
  }

  // With tonearmd down, `tonearmctl subscribe` exits immediately; respawning
  // on exit without this delay is a fork loop.
  Timer {
    id: backoff
    interval: 1000
    repeat: false
    onTriggered: relay.running = true
  }

  // One process per command. Process.command assigned mid-run is silently
  // ignored (galley trap #11), so nothing here is ever reassigned on a
  // running Process -- each send() is its own detached, fire-and-forget
  // process instead.
  //
  // Variadic ahead of need: Task 16's `zone pin <id>` must reach tonearmctl
  // as two argv entries, not one joined string, so arg2 is accepted now
  // rather than widening this signature later.
  function send(verb, arg, arg2) {
    var argv = [root.ctlPath, verb]
    if (arg !== undefined && arg !== null) argv.push(String(arg))
    if (arg2 !== undefined && arg2 !== null) argv.push(String(arg2))
    Quickshell.execDetached(argv)
  }

  // One Process per call, created here and destroyed in onRunningChanged.
  // Reassigning command on a running Process is silently ignored (galley
  // trap #11), and a single shared Process would drop every overlapping
  // browse request -- which is exactly what happens when a keypress lands
  // while a search is still in flight.
  Component {
    id: rpcComponent

    Process {
      id: rpc
      property var callback: null
      property string buffer: ""

      stdout: SplitParser {
        onRead: function (line) {
          if (line && line.length > 0 && rpc.buffer === "") rpc.buffer = line
        }
      }

      // onRunningChanged, NOT onExited. A failed spawn never emits exited()
      // -- measured, and documented at Service.qml:52-56: the process goes
      // straight to running=false without ever passing through true. Firing
      // the callback from onExited would mean a failed spawn never calls back
      // at all, so BrowsePane's `busy` flag would stay true forever and the
      // pane would freeze with no error anywhere. onRunningChanged is the one
      // drain signal that covers both a failed spawn and a normal exit.
      //
      // `done` guards against a double fire: onRunningChanged also runs on the
      // false->true transition when the process starts, and a callback invoked
      // twice would clear `busy` before the real reply arrives.
      property bool done: false

      onRunningChanged: {
        if (rpc.running || rpc.done) return
        rpc.done = true
        var parsed = null
        if (rpc.buffer.length > 0) {
          try {
            parsed = JSON.parse(rpc.buffer)
          } catch (e) {
            console.warn("tonearm: unparseable browse reply")
          }
        }
        if (rpc.callback) rpc.callback(parsed)
        rpc.destroy()
      }
    }
  }

  // Fire a browse op and hand the parsed reply to `callback`. The callback is
  // guaranteed to run exactly once -- on a reply, on a crash, or on a failed
  // spawn -- because the Process above drains on onRunningChanged rather than
  // onExited. Callers rely on that guarantee to clear their `busy` flag; a
  // path that can skip the callback freezes the pane silently.
  function browse(args, callback) {
    var argv = [root.ctlPath, "browse"]
    for (var i = 0; i < args.length; i++) argv.push(String(args[i]))
    var proc = rpcComponent.createObject(root, {
      command: argv,
      callback: callback
    })
    if (proc === null) {
      console.warn("tonearm: could not create browse process")
      if (callback) callback(null)
      return
    }
    proc.running = true
  }

  // Qt.resolvedUrl() percent-encodes reserved characters (a space becomes
  // %20), so a bare `.replace("file://", "")` would leave that escape in the
  // path and the spawn would fail on any install path containing one. A
  // failed spawn never emits exited() -- it goes straight to running=false
  // -- so the relay would back off and retry forever with nothing in the
  // journal at all: no ReferenceError, no "unparseable state line", because
  // there would be no output to parse. Copied verbatim from
  // galley/Panel.qml:46-50 and colophon/Panel.qml:25-30, which independently
  // converged on the same body.
  function pathFromUrl(url) {
    var value = String(url || "")
    if (value.indexOf("file://") === 0) return decodeURIComponent(value.substring(7))
    return value
  }

  Component.onCompleted: {
    // Exactly one onCompleted handler: QML rejects a duplicate and the whole
    // component fails to instantiate with nothing in the journal.
    root.ctlPath = root.pathFromUrl(Qt.resolvedUrl("scripts/tonearmctl"))
    relay.running = true
  }
}
