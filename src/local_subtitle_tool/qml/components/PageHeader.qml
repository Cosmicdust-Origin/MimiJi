import QtQuick
import QtQuick.Layouts
import ".."

Item {
    id: root
    property string eyebrow: ""
    property string title: ""
    property string description: ""
    default property alias actions: actionArea.data
    implicitHeight: Math.max(98, headerLayout.implicitHeight + 20)

    RowLayout {
        id: headerLayout
        anchors.fill: parent
        anchors.bottomMargin: 20
        spacing: 24
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 5
            Text {
                text: root.eyebrow.toUpperCase()
                color: Theme.inkFaint
                font.family: Theme.family
                font.pixelSize: 11
                font.letterSpacing: 1.0
            }
            Text {
                text: root.title
                color: Theme.ink
                font.family: Theme.family
                font.pixelSize: 27
                font.weight: Font.DemiBold
                font.letterSpacing: -0.8
            }
            Text {
                Layout.fillWidth: true
                text: root.description
                color: Theme.inkSoft
                font.family: Theme.family
                font.pixelSize: 14
                wrapMode: Text.Wrap
                maximumLineCount: 2
                elide: Text.ElideRight
            }
        }
        RowLayout {
            id: actionArea
            spacing: 8
            Layout.alignment: Qt.AlignTop
        }
    }
    Rectangle {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        height: 1
        color: Theme.lineDark
    }
}
