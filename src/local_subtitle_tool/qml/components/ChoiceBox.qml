pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import ".."

ComboBox {
    id: box
    implicitHeight: 40
    leftPadding: 0
    rightPadding: 24
    font.family: Theme.family
    font.pixelSize: 14

    contentItem: Text {
        leftPadding: 0
        rightPadding: 22
        text: box.displayText
        color: box.enabled ? Theme.ink : Theme.inkFaint
        font: box.font
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }
    indicator: Text {
        x: box.width - width - 5
        y: Math.round((box.height - height) / 2)
        text: "⌄"
        color: Theme.inkFaint
        font.family: Theme.family
        font.pixelSize: 16
    }
    background: Rectangle {
        color: "transparent"
        Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            height: box.activeFocus ? 2 : 1
            color: box.activeFocus ? Theme.ink : Theme.line
        }
    }

    // Rows are drawn with theme colors only: the hovered/keyboard row gets a subtle
    // fill, the current value is bold, and text always stays in the ink color.
    delegate: ItemDelegate {
        id: row
        required property var modelData
        required property int index
        width: ListView.view ? ListView.view.width : box.width
        height: 34
        hoverEnabled: true
        highlighted: box.highlightedIndex === row.index
        contentItem: Text {
            leftPadding: 10
            text: row.modelData
            color: Theme.ink
            font.family: Theme.family
            font.pixelSize: 14
            font.weight: box.currentIndex === row.index ? Font.DemiBold : Font.Normal
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }
        background: Rectangle {
            color: row.highlighted || row.hovered ? Theme.selection : Theme.paper
            Rectangle {
                width: 2
                height: parent.height
                color: box.currentIndex === row.index ? Theme.ink : "transparent"
            }
        }
    }

    popup: Popup {
        y: box.height + 2
        width: box.width
        padding: 1
        implicitHeight: Math.min(contentItem.implicitHeight + 2, 280)
        contentItem: ListView {
            clip: true
            implicitHeight: contentHeight
            model: box.popup.visible ? box.delegateModel : null
            currentIndex: box.highlightedIndex
            ScrollIndicator.vertical: ScrollIndicator {}
        }
        background: Rectangle {
            color: Theme.paper
            border.width: 1
            border.color: Theme.lineDark
            radius: 4
        }
    }
}
