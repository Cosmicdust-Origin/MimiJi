// qmllint disable unqualified
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."
import "components"

Item {
    id: root

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        PageHeader {
            Layout.fillWidth: true
            eyebrow: "Saved jobs"
            title: "이전 작업"
            description: "완료되지 않은 작업은 마지막 안전 지점부터 이어집니다."
            AppButton {
                text: "새 작업"
                onClicked: bridge.newJob()
            }
        }

        ScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            contentWidth: availableWidth

            Column {
                width: parent.width
                topPadding: 5

                Repeater {
                    model: bridge.jobs
                    delegate: Item {
                        required property var modelData
                        width: parent.width
                        height: 82

                        Rectangle {
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.bottom: parent.bottom
                            height: 1
                            color: Theme.line
                        }
                        RowLayout {
                            anchors.fill: parent
                            spacing: 20
                            ColumnLayout {
                                Layout.fillWidth: true
                                Layout.preferredWidth: 280
                                spacing: 4
                                Text { text: modelData.code; color: Theme.ink; font.family: Theme.family; font.pixelSize: 16; font.weight: Font.DemiBold }
                                Text { Layout.fillWidth: true; text: modelData.file_name; color: Theme.inkFaint; font.family: Theme.family; font.pixelSize: 11; elide: Text.ElideMiddle }
                            }
                            ColumnLayout {
                                Layout.fillWidth: true
                                Layout.preferredWidth: 340
                                spacing: 4
                                Text { text: modelData.stage_title; color: Theme.ink; font.family: Theme.family; font.pixelSize: 13; font.weight: Font.DemiBold }
                                Text { text: modelData.stage_detail; color: Theme.inkSoft; font.family: Theme.family; font.pixelSize: 12 }
                                Text {
                                    visible: Boolean(modelData.registry_error)
                                    text: modelData.registry_error || ""
                                    color: Theme.danger
                                    font.family: Theme.family
                                    font.pixelSize: 11
                                }
                            }
                            ColumnLayout {
                                Layout.preferredWidth: 130
                                spacing: 4
                                Text { text: modelData.updated_text; color: Theme.inkFaint; font.family: Theme.family; font.pixelSize: 11 }
                                Text { text: "누적 " + modelData.elapsed_text; color: Theme.inkFaint; font.family: Theme.family; font.pixelSize: 11 }
                            }
                            AppButton {
                                text: "목록 제거"
                                kind: "quiet"
                                enabled: !bridge.busy
                                onClicked: bridge.removeJob(modelData.job_id)
                            }
                            AppButton {
                                text: "이어서 진행"
                                kind: index === 0 ? "primary" : "default"
                                enabled: !bridge.busy && !modelData.registry_error
                                onClicked: bridge.resumeJob(modelData.job_id)
                            }
                        }
                    }
                }

                Text {
                    visible: bridge.jobs.length === 0
                    width: parent.width
                    topPadding: 36
                    text: "이어갈 미완료 작업이 없습니다."
                    color: Theme.inkFaint
                    font.family: Theme.family
                    font.pixelSize: 14
                    horizontalAlignment: Text.AlignHCenter
                }
            }
        }

        FooterBar {
            Layout.fillWidth: true
            title: "작업 파일은 원래 출력 폴더에 유지됩니다."
            detail: "목록에서 제거해도 영상과 결과 파일을 자동으로 삭제하지 않습니다."
        }
    }

    Dialog {
        id: resumeChoice
        anchors.centerIn: parent
        width: Math.min(560, root.width - 48)
        modal: true
        visible: bridge.resumeChoiceVisible
        closePolicy: Popup.NoAutoClose
        padding: 24
        background: Rectangle {
            color: Theme.paper
            border.color: Theme.lineDark
            border.width: 1
        }
        contentItem: ColumnLayout {
            spacing: 14
            Text {
                Layout.fillWidth: true
                text: "번역 설정을 확인합니다"
                color: Theme.ink
                font.family: Theme.family
                font.pixelSize: 19
                font.weight: Font.DemiBold
            }
            Text {
                Layout.fillWidth: true
                text: bridge.resumeChoiceText
                color: Theme.inkSoft
                font.family: Theme.family
                font.pixelSize: 13
                wrapMode: Text.WordWrap
            }
            Text {
                Layout.fillWidth: true
                text: "현재 설정  " + bridge.resumeOriginalProfileText
                color: Theme.ink
                font.family: Theme.family
                font.pixelSize: 13
            }
            RowLayout {
                Layout.fillWidth: true
                Layout.topMargin: 8
                Item { Layout.fillWidth: true }
                AppButton {
                    text: "취소"
                    kind: "quiet"
                    onClicked: bridge.cancelResumeChoice()
                }
                AppButton {
                    text: "그대로 이어하기"
                    onClicked: bridge.resumeWithSavedSettings()
                }
                AppButton {
                    text: "설정 바꿔서 이어하기"
                    kind: "primary"
                    onClicked: bridge.editResumeTranslationSettings()
                }
            }
        }
    }

    Dialog {
        id: unverifiedChoice
        anchors.centerIn: parent
        width: Math.min(540, root.width - 48)
        modal: true
        visible: bridge.unverifiedBatchChoiceVisible
        closePolicy: Popup.NoAutoClose
        padding: 24
        background: Rectangle {
            color: Theme.paper
            border.color: Theme.lineDark
            border.width: 1
        }
        contentItem: ColumnLayout {
            spacing: 14
            Text {
                Layout.fillWidth: true
                text: "기존 배치를 검증할 수 없습니다"
                color: Theme.ink
                font.family: Theme.family
                font.pixelSize: 19
                font.weight: Font.DemiBold
            }
            Text {
                Layout.fillWidth: true
                text: bridge.unverifiedBatchText + "\n새 번역 설정으로 해당 배치를 다시 번역할 수 있습니다. 기존 파일은 임의로 재사용하지 않습니다."
                color: Theme.inkSoft
                font.family: Theme.family
                font.pixelSize: 13
                wrapMode: Text.WordWrap
            }
            RowLayout {
                Layout.fillWidth: true
                Layout.topMargin: 8
                Item { Layout.fillWidth: true }
                AppButton {
                    text: "취소"
                    kind: "quiet"
                    onClicked: bridge.cancelUnverifiedBatchChoice()
                }
                AppButton {
                    text: "새 설정으로 다시 번역"
                    kind: "primary"
                    onClicked: bridge.confirmRetranslateUnverifiedBatches()
                }
            }
        }
    }
}
