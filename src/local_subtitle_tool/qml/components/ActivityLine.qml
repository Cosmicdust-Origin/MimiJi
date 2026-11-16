import QtQuick
import ".."

Item {
    id: root
    property bool running: true
    implicitHeight: 2
    clip: true

    Rectangle {
        anchors.fill: parent
        color: Theme.lineDark
    }
    Rectangle {
        id: segment
        width: Math.min(38, root.width * 0.25)
        height: 1
        y: 0
        color: Theme.ink
        visible: root.running
    }
    SequentialAnimation {
        running: root.running && root.visible
        loops: Animation.Infinite
        NumberAnimation {
            target: segment
            property: "x"
            from: 0
            to: Math.max(0, root.width - segment.width)
            duration: 1100
            easing.type: Easing.Linear
        }
        NumberAnimation {
            target: segment
            property: "x"
            from: Math.max(0, root.width - segment.width)
            to: 0
            duration: 1100
            easing.type: Easing.Linear
        }
    }
}
