import QtQuick
import QtQuick.Controls
import ".."

Control {
    id: control
    property string text: ""
    property bool selected: false
    signal clicked()

    implicitHeight: 32
    hoverEnabled: true
    focusPolicy: Qt.StrongFocus
    Accessible.role: Accessible.Button
    Accessible.name: text

    contentItem: Text {
        text: control.text
        leftPadding: 10
        color: control.selected ? Theme.ink : Theme.inkSoft
        font.family: Theme.family
        font.pixelSize: 14
        font.weight: control.selected ? Font.DemiBold : Font.Normal
        verticalAlignment: Text.AlignVCenter
        opacity: control.enabled ? 1 : 0.35
    }
    background: Rectangle {
        color: mouse.containsMouse && control.enabled ? Theme.hover : "transparent"
        Rectangle {
            width: 2
            height: parent.height
            color: control.selected ? Theme.ink : "transparent"
        }
        border.width: control.activeFocus ? 1 : 0
        border.color: Theme.lineDark
    }
    MouseArea {
        id: mouse
        anchors.fill: parent
        hoverEnabled: true
        enabled: control.enabled
        onClicked: {
            control.forceActiveFocus()
            control.clicked()
        }
    }
    Keys.onReturnPressed: control.clicked()
    Keys.onEnterPressed: control.clicked()
    Keys.onSpacePressed: control.clicked()
}
