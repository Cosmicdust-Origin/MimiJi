// qmllint disable unqualified
import QtQuick
import QtQuick.Layouts
import "."
import "components"

Item {
    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        PageHeader {
            Layout.fillWidth: true
            eyebrow: bridge.code
            title: "작업이 끝났습니다"
            description: "결과 파일을 저장하고 자동 검사를 마쳤습니다."
            AppButton { text: "새 작업"; onClicked: bridge.newJob() }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 0

            ColumnLayout {
                Layout.preferredWidth: 430
                Layout.fillHeight: true
                Layout.rightMargin: 30
                Layout.topMargin: 22
                spacing: 0
                Text { text: "실제 작업 시간"; color: Theme.inkFaint; font.family: Theme.family; font.pixelSize: 12 }
                Text { text: bridge.elapsedText; color: Theme.ink; font.family: Theme.family; font.pixelSize: 29; font.weight: Font.Medium; Layout.topMargin: 7 }

                Column {
                    Layout.fillWidth: true
                    Layout.topMargin: 20
                    Repeater {
                        model: bridge.resultMetrics
                        delegate: Item {
                            required property var modelData
                            width: parent.width
                            height: 38
                            Rectangle { anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom; height: 1; color: Theme.line }
                            Text { anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter; text: modelData.label; color: Theme.inkSoft; font.family: Theme.family; font.pixelSize: 12 }
                            Text { anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; text: modelData.value; color: Theme.ink; font.family: Theme.family; font.pixelSize: 13; font.weight: Font.DemiBold }
                        }
                    }
                }
                Item { Layout.fillHeight: true }
            }

            Rectangle { Layout.fillHeight: true; Layout.preferredWidth: 1; color: Theme.line }

            ColumnLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.leftMargin: 28
                Layout.topMargin: 22
                spacing: 0
                Text { text: "결과 파일"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 15; font.weight: Font.DemiBold }
                Text { text: "파일 이름을 누르면 기본 프로그램으로 엽니다."; color: Theme.inkFaint; font.family: Theme.family; font.pixelSize: 12; Layout.topMargin: 4 }

                Column {
                    Layout.fillWidth: true
                    Layout.topMargin: 13
                    Repeater {
                        model: bridge.resultFiles
                        delegate: Item {
                            required property var modelData
                            width: parent.width
                            height: 42
                            Rectangle { anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom; height: 1; color: Theme.line }
                            Text { anchors.left: parent.left; anchors.right: sizeText.left; anchors.rightMargin: 15; anchors.verticalCenter: parent.verticalCenter; text: modelData.name; color: Theme.ink; font.family: Theme.family; font.pixelSize: 12; font.weight: Font.DemiBold; elide: Text.ElideMiddle }
                            Text { id: sizeText; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; text: modelData.size; color: Theme.inkFaint; font.family: Theme.family; font.pixelSize: 11 }
                            MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: bridge.openPath(modelData.path) }
                        }
                    }
                }
                Item { Layout.fillHeight: true }
            }
        }

        FooterBar {
            Layout.fillWidth: true
            title: "SRT 구조와 번역 품질 검사를 완료했습니다."
            detail: "결과 보고서에서 CUDA 사용 여부와 검증 수치를 확인할 수 있습니다."
            AppButton { text: "출력 폴더 열기"; kind: "primary"; onClicked: bridge.openOutputFolder() }
        }
    }
}
