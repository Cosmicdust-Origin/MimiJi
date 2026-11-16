import QtQuick
import QtQuick.Controls
import ".."

Control {
    id: control
    property string text: ""
    property string kind: "default"
    signal clicked()

    implicitWidth: Math.max(78, label.implicitWidth + 28)
    implicitHeight: 34
    hoverEnabled: true
    focusPolicy: Qt.StrongFocus
    scale: mouse.pressed ? 0.975 : 1
    Accessible.role: Accessible.Button
    Accessible.name: text
    Accessible.onPressAction: control.clicked()

    Behavior on scale {
        NumberAnimation { duration: 110; easing.type: Easing.OutCubic }
    }

    contentItem: Text {
        id: label
        text: control.text
        color: control.kind === "primary" && control.enabled ? Theme.onPrimary
             : control.kind === "danger" ? Theme.danger : Theme.ink
        font.family: Theme.family
        font.pixelSize: 14
        font.weight: Font.DemiBold
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        opacity: control.enabled ? 1 : 0.55
    }

    background: Rectangle {
        radius: 5
        color: control.kind === "primary" && !control.enabled ? Theme.paperDeep
             : control.kind === "primary" ? (mouse.containsMouse ? Theme.primaryHover : Theme.primary)
             : control.kind === "danger" && mouse.containsMouse ? Theme.dangerSoft
             : control.kind === "quiet" ? "transparent"
             : mouse.containsMouse ? Theme.hover : Theme.paper
        border.width: control.activeFocus ? 2 : (control.kind === "quiet" ? 0 : 1)
        border.color: control.activeFocus ? Theme.ink
                    : control.kind === "primary" ? (control.enabled ? Theme.primaryBorder : Theme.lineDark)
                    : control.kind === "danger" ? Theme.dangerLine : Theme.lineDark
    }

    MouseArea {
        id: mouse
        anchors.fill: parent
        hoverEnabled: true
        enabled: control.enabled
        cursorShape: Qt.PointingHandCursor
        onClicked: {
            control.forceActiveFocus()
            control.clicked()
        }
    }

    Keys.onReturnPressed: control.clicked()
    Keys.onEnterPressed: control.clicked()
    Keys.onSpacePressed: control.clicked()
}
