pragma Singleton
import QtQuick
import "Fixtures.js" as Fixtures
Item {
  id: root
  property var state: Fixtures.states.connecting
  property double receivedAt: Date.now()
  property string playerError: ""
  property int consumers: 0
  property int blockedCommands: 0
  property int blockedBrowse: 0
  property string fixtureName: "connecting"
  function attach() { consumers++ }
  function detach() { consumers = Math.max(0, consumers - 1) }
  function selectFixture(name) {
    if (!Object.prototype.hasOwnProperty.call(Fixtures.states, name)) return "unknown-fixture"
    state = Fixtures.states[name]
    receivedAt = Date.now()
    fixtureName = name
    return "ok"
  }
  function send() { blockedCommands++; return false }
  function browse(session, payload, callback) {
    blockedBrowse++
    if (callback) callback({v:1, ok:false, error:"preview_only", message:"Preview blocks all I/O"})
  }
}
