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
            title: bridge.tr("이전 작업", bridge.uiLanguage)
            description: bridge.jobsFilter === "all"
                         ? bridge.tr("완료된 작업과 이어갈 작업을 함께 표시합니다.", bridge.uiLanguage)
                         : bridge.tr("완료되지 않은 작업은 마지막 안전 지점부터 이어집니다.", bridge.uiLanguage)
            AppButton {
                text: bridge.tr("새 작업", bridge.uiLanguage)
                onClicked: bridge.newJob()
            }
        }

        Rectangle {
            Layout.fillWidth: true
            implicitHeight: 56
            color: Theme.paperDeep
            border.color: Theme.line
            border.width: 1

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 16
                anchors.rightMargin: 16
                spacing: 12

                Text {
                    text: bridge.tr("표시", bridge.uiLanguage)
                    color: Theme.inkFaint
                    font.family: Theme.family
                    font.pixelSize: 12
                }
                ChoiceBox {
                    Layout.preferredWidth: 132
                    model: [bridge.tr("전체 작업", bridge.uiLanguage), bridge.tr("미완료만", bridge.uiLanguage)]
                    currentIndex: bridge.jobsFilter === "all" ? 0 : 1
                    onActivated: function(index) {
                        bridge.jobsFilter = index === 0 ? "all" : "incomplete"
                    }
                }
                Item { Layout.preferredWidth: 12 }
                Text {
                    text: bridge.tr("정렬", bridge.uiLanguage)
                    color: Theme.inkFaint
                    font.family: Theme.family
                    font.pixelSize: 12
                }
                ChoiceBox {
                    Layout.preferredWidth: 150
                    model: [
                        bridge.tr("최근 변경순", bridge.uiLanguage),
                        bridge.tr("완료한 순", bridge.uiLanguage),
                        bridge.tr("생성한 순", bridge.uiLanguage)
                    ]
                    currentIndex: bridge.jobsSort === "completed" ? 1
                                  : bridge.jobsSort === "created" ? 2 : 0
                    onActivated: function(index) {
                        bridge.jobsSort = index === 1 ? "completed"
                                        : index === 2 ? "created" : "recent"
                    }
                }
                Item { Layout.fillWidth: true }
                Text {
                    text: bridge.tr("선택은 다음 실행에도 유지됩니다.", bridge.uiLanguage)
                    color: Theme.inkFaint
                    font.family: Theme.family
                    font.pixelSize: 11
                }
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
                                Text { text: modelData.stage_title; color: modelData.incomplete ? Theme.ink : Theme.inkSoft; font.family: Theme.family; font.pixelSize: 13; font.weight: Font.DemiBold }
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
                                Text { text: bridge.tr(modelData.time_label, bridge.uiLanguage) + "  " + modelData.updated_text; color: Theme.inkFaint; font.family: Theme.family; font.pixelSize: 11 }
                                Text { text: bridge.tr("누적 ", bridge.uiLanguage) + modelData.elapsed_text; color: Theme.inkFaint; font.family: Theme.family; font.pixelSize: 11 }
                            }
                            AppButton {
                                text: bridge.tr("목록 제거", bridge.uiLanguage)
                                kind: "quiet"
                                enabled: !bridge.busy
                                onClicked: bridge.removeJob(modelData.job_id)
                            }
                            AppButton {
                                text: modelData.incomplete
                                      ? bridge.tr("이어서 진행", bridge.uiLanguage)
                                      : bridge.tr("결과 폴더", bridge.uiLanguage)
                                kind: index === 0 && modelData.incomplete ? "primary" : "default"
                                enabled: !bridge.busy && !modelData.registry_error
                                onClicked: {
                                    if (modelData.incomplete)
                                        bridge.resumeJob(modelData.job_id)
                                    else
                                        bridge.openPath(modelData.output_dir)
                                }
                            }
                        }
                    }
                }

                Text {
                    visible: bridge.jobs.length === 0
                    width: parent.width
                    topPadding: 36
                    text: bridge.jobsFilter === "all"
                          ? bridge.tr("저장된 작업이 없습니다.", bridge.uiLanguage)
                          : bridge.tr("이어갈 미완료 작업이 없습니다.", bridge.uiLanguage)
                    color: Theme.inkFaint
                    font.family: Theme.family
                    font.pixelSize: 14
                    horizontalAlignment: Text.AlignHCenter
                }
            }
        }

        FooterBar {
            Layout.fillWidth: true
            title: bridge.tr("작업 파일은 원래 출력 폴더에 유지됩니다.", bridge.uiLanguage)
            detail: bridge.tr("목록에서 제거해도 영상과 결과 파일을 자동으로 삭제하지 않습니다.", bridge.uiLanguage)
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
                text: bridge.tr("번역 설정을 확인합니다", bridge.uiLanguage)
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
                text: bridge.tr("현재 설정  ", bridge.uiLanguage) + bridge.resumeOriginalProfileText
                color: Theme.ink
                font.family: Theme.family
                font.pixelSize: 13
            }
            RowLayout {
                Layout.fillWidth: true
                Layout.topMargin: 8
                Item { Layout.fillWidth: true }
                AppButton {
                    text: bridge.tr("취소", bridge.uiLanguage)
                    kind: "quiet"
                    onClicked: bridge.cancelResumeChoice()
                }
                AppButton {
                    text: bridge.tr("그대로 이어하기", bridge.uiLanguage)
                    onClicked: bridge.resumeWithSavedSettings()
                }
                AppButton {
                    text: bridge.tr("설정 바꿔서 이어하기", bridge.uiLanguage)
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
                text: bridge.tr("기존 배치를 검증할 수 없습니다", bridge.uiLanguage)
                color: Theme.ink
                font.family: Theme.family
                font.pixelSize: 19
                font.weight: Font.DemiBold
            }
            Text {
                Layout.fillWidth: true
                text: bridge.unverifiedBatchText + bridge.tr("\n새 번역 설정으로 해당 배치를 다시 번역할 수 있습니다. 기존 파일은 임의로 재사용하지 않습니다.", bridge.uiLanguage)
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
                    text: bridge.tr("취소", bridge.uiLanguage)
                    kind: "quiet"
                    onClicked: bridge.cancelUnverifiedBatchChoice()
                }
                AppButton {
                    text: bridge.tr("새 설정으로 다시 번역", bridge.uiLanguage)
                    kind: "primary"
                    onClicked: bridge.confirmRetranslateUnverifiedBatches()
                }
            }
        }
    }
}
