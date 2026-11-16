// qmllint disable unqualified
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."
import "components"

Item {
    Dialog {
        id: modelDownloadDialog
        objectName: "modelDownloadDialog"
        anchors.centerIn: parent
        width: Math.min(620, parent.width - 48)
        modal: true
        title: "전사 모델 다운로드"
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: bridge.confirmModelDownload()

        ColumnLayout {
            width: parent.width
            spacing: 10
            Text {
                Layout.fillWidth: true
                text: "faster-whisper large-v3 모델을 다운로드합니다."
                color: Theme.ink
                font.family: Theme.family
                font.pixelSize: 14
                font.weight: Font.DemiBold
            }
            Text {
                Layout.fillWidth: true
                text: "용량  " + bridge.modelDownloadSize
                      + "\n저장 위치  " + bridge.modelDownloadCachePath
                      + "\n현재 여유 공간  " + bridge.modelDownloadFreeSpace
                color: Theme.inkSoft
                font.family: Theme.family
                font.pixelSize: 12
                wrapMode: Text.Wrap
            }
            Text {
                Layout.fillWidth: true
                text: "다운로드가 끝날 때까지 앱을 종료하지 마십시오."
                color: Theme.inkFaint
                font.family: Theme.family
                font.pixelSize: 11
                wrapMode: Text.Wrap
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        PageHeader {
            Layout.fillWidth: true
            eyebrow: "First run"
            title: "환경 진단"
            description: "이 컴퓨터에서 사용할 전사 방식과 번역 계정을 확인합니다."
            Text {
                text: bridge.diagnosticBusy ? "진단 중" : "전체 진단 · 약 10초"
                color: Theme.inkFaint
                font.family: Theme.family
                font.pixelSize: 12
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 0

            ColumnLayout {
                Layout.preferredWidth: 180
                Layout.fillHeight: true
                Layout.rightMargin: 26
                Layout.topMargin: 23
                spacing: 18
                Repeater {
                    model: [
                        {title: "1. 실행 환경", detail: "하드웨어와 필수 도구"},
                        {title: "2. 전사 모델", detail: "다운로드와 저장 위치"},
                        {title: "3. 번역 계정", detail: "Codex 또는 Claude"}
                    ]
                    delegate: ColumnLayout {
                        required property var modelData
                        required property int index
                        spacing: 4
                        Text { text: modelData.title; color: index === 0 ? Theme.ink : Theme.inkFaint; font.family: Theme.family; font.pixelSize: 13; font.weight: index === 0 ? Font.DemiBold : Font.Normal }
                        Text { text: modelData.detail; color: Theme.inkFaint; font.family: Theme.family; font.pixelSize: 11 }
                    }
                }
                Item { Layout.fillHeight: true }
            }

            Rectangle { Layout.fillHeight: true; Layout.preferredWidth: 1; color: Theme.line }

            ColumnLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.leftMargin: 28
                Layout.topMargin: 23
                spacing: 0

                Text { text: "실행 환경"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 15; font.weight: Font.DemiBold }
                Text { text: "설치 여부가 아니라 실제 실행 가능 여부를 확인합니다."; color: Theme.inkFaint; font.family: Theme.family; font.pixelSize: 12; Layout.topMargin: 4 }

                ScrollView {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.topMargin: 14
                    clip: true
                    contentWidth: availableWidth

                    Column {
                        width: parent.width
                        Repeater {
                            model: bridge.diagnosticRows
                            delegate: Item {
                                required property var modelData
                                width: parent.width
                                height: modelData.action === "download_model" ? 62 : 43
                                Rectangle { anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom; height: 1; color: Theme.line }
                                RowLayout {
                                    anchors.fill: parent
                                    spacing: 14
                                    Text { Layout.preferredWidth: 128; text: modelData.name; color: Theme.inkSoft; font.family: Theme.family; font.pixelSize: 12 }
                                    Text {
                                        Layout.fillWidth: true
                                        text: modelData.detail
                                        color: Theme.ink
                                        font.family: Theme.family
                                        font.pixelSize: 12
                                        font.weight: Font.DemiBold
                                        wrapMode: modelData.action === "download_model" ? Text.Wrap : Text.NoWrap
                                        maximumLineCount: modelData.action === "download_model" ? 2 : 1
                                        elide: Text.ElideRight
                                    }
                                    Text { Layout.preferredWidth: 74; text: modelData.state; color: modelData.problem ? Theme.danger : Theme.inkFaint; font.family: Theme.family; font.pixelSize: 11; horizontalAlignment: Text.AlignRight }
                                    AppButton {
                                        visible: Boolean(modelData.action)
                                        Layout.preferredWidth: visible ? 82 : 0
                                        text: modelData.action_label || "조치"
                                        kind: modelData.problem ? "danger" : "default"
                                        enabled: !bridge.diagnosticBusy && !bridge.busy
                                        onClicked: {
                                            if (modelData.action === "download_model")
                                                modelDownloadDialog.open()
                                            else
                                                bridge.performDiagnosticAction(modelData.action)
                                        }
                                    }
                                }
                            }
                        }
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 58
                    Layout.bottomMargin: 16
                    color: "transparent"
                    border.width: 1
                    border.color: Theme.line
                    Text {
                        anchors.fill: parent
                        anchors.margins: 12
                        text: bridge.diagnosticSummary
                        color: Theme.inkSoft
                        font.family: Theme.family
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                        verticalAlignment: Text.AlignVCenter
                    }
                }
            }
        }

        FooterBar {
            Layout.fillWidth: true
            title: bridge.diagnosticBusy ? "환경을 확인하고 있습니다." : bridge.systemSummary
            detail: "CUDA를 사용할 수 없으면 자동 전환하지 않고 CPU 사용 여부를 선택합니다."
            AppButton {
                text: bridge.diagnosticBusy
                      ? "진단 중"
                      : bridge.diagnosticRows.length > 0 ? "다시 진단" : "진단 시작"
                enabled: !bridge.diagnosticBusy && !bridge.busy
                onClicked: bridge.runDiagnostic()
            }
            AppButton {
                text: "새 작업"
                kind: "primary"
                enabled: !bridge.diagnosticBusy && !bridge.busy
                onClicked: bridge.showScreen("home")
            }
        }
    }
}
