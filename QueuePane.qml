import QtQuick
import qs.Commons
import "QueueModel.js" as QueueModel

Column {
    id: root
    property var roonState: null
    property bool expanded: false
    property string fontFamily: ""
    property color foreground: "white"
    property color faint: "gray"
    property color highlight: "#333333"
    property int cursor: -1
    readonly property var rows: QueueModel.rows(root.roonState)
    readonly property bool supported: !!(root.roonState && root.roonState.queue)
    signal opening()
    spacing: Style.space(6)
    onRowsChanged: cursor = QueueModel.cursor(cursor, 0, rows.length)
    onExpandedChanged: if (expanded) opening()
    function moveCursor(delta) {
        cursor = QueueModel.cursor(cursor, delta, rows.length)
        if (cursor >= 0) list.positionViewAtIndex(cursor, ListView.Contain)
    }

    Item {
        width: parent.width
        height: heading.implicitHeight + Style.space(12)
        Text {
            id: heading
            anchors.fill: parent
            textFormat: Text.PlainText
            text: (root.expanded ? "▾ " : "▸ ") + QueueModel.header(root.roonState)
            color: root.supported ? root.foreground : root.faint
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.Wrap
            verticalAlignment: Text.AlignVCenter
        }
        MouseArea {
            anchors.fill: parent
            enabled: root.supported
            onClicked: root.expanded = !root.expanded
        }
    }
    Column {
        visible: root.expanded
        width: parent.width
        spacing: Style.space(6)
        Text {
            width: parent.width
            textFormat: Text.PlainText
            text: QueueModel.message(root.roonState)
            visible: text.length > 0
            color: root.faint
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.Wrap
        }
        ListView {
            id: list
            property var queuePane: root
            width: parent.width
            height: Math.min(contentHeight, Style.space(220))
            visible: root.rows.length > 0
            clip: true
            model: root.rows
            delegate: Rectangle {
                id: queueRow
                property var owningView: ListView.view
                property var pane: owningView.queuePane
                required property var modelData
                required property int index
                width: ListView.view.width
                height: labels.implicitHeight + Style.space(12)
                radius: Style.space(4)
                color: queueRow.pane.cursor === queueRow.index ? queueRow.pane.highlight : "transparent"
                Column {
                    id: labels
                    x: Style.space(6)
                    y: Style.space(6)
                    width: Math.max(1, parent.width - Style.space(12))
                    Text {
                        width: parent.width
                        textFormat: Text.PlainText
                        text: queueRow.modelData.title
                        color: queueRow.pane.foreground
                        font.family: queueRow.pane.fontFamily
                        font.pixelSize: Style.font.bodySmall
                        wrapMode: Text.Wrap
                        maximumLineCount: 2
                        elide: Text.ElideRight
                    }
                    Text {
                        width: parent.width
                        textFormat: Text.PlainText
                        text: queueRow.modelData.subtitle
                        color: queueRow.pane.faint
                        font.family: queueRow.pane.fontFamily
                        font.pixelSize: Style.font.caption
                        wrapMode: Text.Wrap
                        maximumLineCount: 2
                        elide: Text.ElideRight
                    }
                }
                MouseArea { anchors.fill: parent; onClicked: queueRow.pane.cursor = queueRow.index }
            }
        }
        Text {
            width: parent.width
            textFormat: Text.PlainText
            text: "Read-only · At most 100 rows; this window may omit queue items"
            color: root.faint
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.Wrap
        }
    }
}
