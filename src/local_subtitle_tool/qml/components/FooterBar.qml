import QtQuick
import QtQuick.Layouts
import ".."

Item {
    id: root
    property string title: ""
    property string detail: ""
    default property alias actions: actionArea.data
    implicitHeight: 62

    Rectangle {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        height: 1
        color: Theme.lineDark
    }
    RowLayout {
        anchors.fill: parent
        spacing: 18
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 3
            Text {
                text: root.title
                color: Theme.ink
                font.family: Theme.family
                font.pixelSize: 13
                font.weight: Font.DemiBold
            }
            Text {
                text: root.detail
                color: Theme.inkFaint
                font.family: Theme.family
                font.pixelSize: 12
                elide: Text.ElideRight
                Layout.fillWidth: true
            }
        }
        RowLayout {
            id: actionArea
            spacing: 8
        }
    }
}
