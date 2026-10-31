pragma ComponentBehavior: Bound
import QtQuick
import "QueueModel.js" as Queue

// Pure view with an injected cache-only service; no transport/audio actions.
Item {
    id: root
    property var service: null
    property var roonState: null
    property bool panelOpen: false
    property bool expanded: false
    property string session: "queue"
    property string fontFamily: ""
    property color foreground: "#ffffff"
    property color muted: "#aaaaaa"
    property color highlight: "#303030"
    property int fontSize: 13
    property real unit: 1
    property var rows: []
    property string status: "unknown"
    property int cursor: -1
    property int generation: 0
    property int sequence: 0
    property var pending: null
    property bool alive: true
    readonly property bool active: panelOpen && expanded && visible
    readonly property string contextKey: Queue.context(roonState)
    readonly property bool busy: pending !== null
    readonly property string message: Queue.message(status, rows.length)
    implicitHeight: header.height + (expanded ? body.height + 6*root.unit : 0)
    height: implicitHeight
    onActiveChanged: invalidate()
    onContextKeyChanged: invalidate()
    Component.onDestruction: alive = false

    function invalidate() {
        generation++
        rows=[];cursor=-1
        var selected=Queue.selection(roonState)
        status=selected.status === "available" ? "loading" : selected.status
        // Keep pending until its process drains, even across close/reopen.
        if (active) poll()
    }
    function poll() {
        if (!active || busy || !service || typeof service.queue !== "function") return
        var request=Queue.request(roonState,session.slice(0,32)+"-"+(++sequence))
        if (!request) return
        var token={view:generation,context:contextKey,request:request}
        pending=token
        try {
            service.queue(request,function(value) {
                if (!root || !root.alive) return
                var accept=Queue.current(token,root.pending,root.generation,root.contextKey,root.active)
                if (root.pending === token) root.pending=null
                if (!accept) return
                var parsed=Queue.reply(value,request)
                if (!parsed) { root.status="error";root.rows=[];root.cursor=-1;return }
                root.status=parsed.status;root.rows=parsed.items
                root.cursor=root.rows.length ? Math.max(0,Math.min(root.cursor,root.rows.length-1)) : -1
            })
        } catch (e) { pending=null;status="error";rows=[];cursor=-1 }
    }
    function move(delta) {
        if (rows.length) {
            cursor=Math.max(0,Math.min(rows.length-1,Math.max(0,cursor)+delta))
            list.positionViewAtIndex(cursor,ListView.Contain)
        }
    }
    function activate() { return expanded } // Consume Enter: rows have no action.
    function back() { if (!expanded) return false;expanded=false;return true }
    Timer { interval:1000;repeat:true;running:root.active;onTriggered:root.poll() }
    Rectangle {
        id: header
        width:parent.width;height:30*root.unit
        color:headerMouse.containsMouse ? root.highlight : "transparent"
        radius:4*root.unit
        Text { objectName:"queueHeader"; anchors.left:parent.left;anchors.leftMargin:6*root.unit;anchors.verticalCenter:parent.verticalCenter
            textFormat:Text.PlainText;text:(root.expanded ? "▾ " : "▸ ") + Queue.header(root.roonState)
            elide:Text.ElideRight; width:parent.width-12*root.unit
            font.family:root.fontFamily;font.pixelSize:root.fontSize;color:root.foreground }
        MouseArea { id:headerMouse;anchors.fill:parent;hoverEnabled:true;onClicked:root.expanded=!root.expanded }
    }
    Column {
        id:body
        y:header.height+6*root.unit;width:parent.width;visible:root.expanded;spacing:4*root.unit
        Text { width:parent.width;visible:root.message !== "";textFormat:Text.PlainText
            text:root.message;color:root.muted;font.family:root.fontFamily;font.pixelSize:root.fontSize
            wrapMode:Text.Wrap }
        Text { width:parent.width;visible:root.status === "ready" && root.rows.length > 0
            textFormat:Text.PlainText;text:"Showing up to 100 items"
            color:root.muted;font.family:root.fontFamily;font.pixelSize:root.fontSize }
        ListView {
            id:list
            width:parent.width;height:Math.min(contentHeight,5*44*root.unit)
            visible:root.rows.length>0;clip:true;model:root.rows
            boundsBehavior:Flickable.StopAtBounds
            delegate:Rectangle {
                id:row
                required property int index
                required property var modelData
                width:list.width;height:44*root.unit;radius:4*root.unit
                color:row.index===root.cursor ? root.highlight : "transparent"
                Column {
                    anchors.left:parent.left;anchors.right:parent.right;anchors.margins:6*root.unit
                    anchors.verticalCenter:parent.verticalCenter
                    Text { width:parent.width;textFormat:Text.PlainText;text:row.modelData.title
                        font.family:root.fontFamily;font.pixelSize:root.fontSize;color:root.foreground;elide:Text.ElideRight }
                    Text { width:parent.width;textFormat:Text.PlainText;text:row.modelData.subtitle
                        font.family:root.fontFamily;font.pixelSize:root.fontSize-1;color:root.muted;elide:Text.ElideRight }
                }
                MouseArea { anchors.fill:parent;onClicked:root.cursor=row.index }
            }
        }
    }
}
