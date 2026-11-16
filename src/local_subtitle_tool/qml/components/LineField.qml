import QtQuick
import QtQuick.Controls
import ".."

TextField {
    id: field
    implicitHeight: 40
    leftPadding: 0
    rightPadding: 4
    color: Theme.ink
    placeholderTextColor: Theme.inkFaint
    selectionColor: Theme.selection
    selectedTextColor: Theme.ink
    font.family: Theme.family
    font.pixelSize: 14
    background: Rectangle {
        color: "transparent"
        border.width: 0
        Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            height: field.activeFocus ? 2 : 1
            color: field.activeFocus ? Theme.ink : Theme.line
        }
    }
}
