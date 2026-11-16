// qmllint disable unqualified
import QtQuick
import QtQuick.Layouts
import "."
import "components"

Item {
    id: root
    readonly property var phaseNames: [bridge.tr("환경 확인", bridge.uiLanguage), bridge.tr("음성 전사", bridge.uiLanguage), bridge.tr("직접 번역", bridge.uiLanguage), bridge.tr("검증·출력", bridge.uiLanguage)]

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        PageHeader {
            Layout.fillWidth: true
            eyebrow: bridge.code
            title: bridge.taskTitle
            description: bridge.busy
                         ? bridge.tr("설정은 작업이 끝나거나 중지될 때까지 잠깁니다.", bridge.uiLanguage)
                         : bridge.taskDetail
            AppButton {
                text: bridge.tr("작업 중지", bridge.uiLanguage)
                kind: "danger"
                visible: bridge.busy
                onClicked: bridge.stopJob()
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 0

            ColumnLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.rightMargin: 28
                Layout.topMargin: 22
                spacing: 0

                RowLayout {
                    Layout.fillWidth: true
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 5
                        Text {
                            text: bridge.tr("현재 단계", bridge.uiLanguage)
                            color: Theme.inkSoft
                            font.family: Theme.family
                            font.pixelSize: 12
                        }
                        Text {
                            text: root.phaseNames[Math.max(0, Math.min(3, bridge.phaseIndex))]
                            color: Theme.ink
                            font.family: Theme.family
                            font.pixelSize: 19
                            font.weight: Font.DemiBold
                        }
                    }
                    Text {
                        text: Math.round(bridge.overallProgress * 100) + "%"
                        color: Theme.ink
                        font.family: Theme.family
                        font.pixelSize: 42
                        font.weight: Font.Medium
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 2
                    Layout.topMargin: 20
                    color: Theme.line
                    Rectangle {
                        width: parent.width * bridge.overallProgress
                        height: parent.height
                        color: Theme.ink
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    Layout.topMargin: 16
                    spacing: 0
                    Repeater {
                        model: root.phaseNames
                        delegate: ColumnLayout {
                            required property string modelData
                            required property int index
                            Layout.fillWidth: true
                            spacing: 3
                            Text {
                                text: modelData
                                color: index === bridge.phaseIndex ? Theme.ink : Theme.inkFaint
                                font.family: Theme.family
                                font.pixelSize: 13
                                font.weight: index === bridge.phaseIndex ? Font.DemiBold : Font.Normal
                            }
                            Text {
                                text: index < bridge.phaseIndex ? bridge.tr("완료", bridge.uiLanguage)
                                      : index === bridge.phaseIndex ? bridge.taskDetail : bridge.tr("대기", bridge.uiLanguage)
                                color: index === bridge.phaseIndex ? Theme.inkSoft : Theme.inkFaint
                                font.family: Theme.family
                                font.pixelSize: 11
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }
                        }
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 1
                    Layout.topMargin: 12
                    color: Theme.line
                }

                RowLayout {
                    Layout.fillWidth: true
                    Layout.topMargin: 18
                    spacing: 14
                    ActivityLine {
                        Layout.preferredWidth: 155
                        running: bridge.busy
                    }
                    Text {
                        Layout.fillWidth: true
                        text: bridge.taskDetail
                        color: Theme.inkSoft
                        font.family: Theme.family
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                        maximumLineCount: 2
                        elide: Text.ElideRight
                    }
                    Text {
                        text: bridge.lastSavedText
                        color: Theme.inkFaint
                        font.family: Theme.family
                        font.pixelSize: 11
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: bridge.lastError ? 52 : 0
                    Layout.topMargin: bridge.lastError ? 16 : 0
                    visible: bridge.lastError.length > 0
                    color: Theme.dangerSoft
                    border.width: 1
                    border.color: Theme.dangerLine
                    Text {
                        anchors.fill: parent
                        anchors.margins: 12
                        text: bridge.lastError
                        color: Theme.danger
                        font.family: Theme.family
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                        maximumLineCount: 3
                        elide: Text.ElideRight
                    }
                }

                Text {
                    text: bridge.tr("최근 로그", bridge.uiLanguage)
                    color: Theme.inkFaint
                    font.family: Theme.family
                    font.pixelSize: 11
                    font.letterSpacing: 0.7
                    Layout.topMargin: 22
                }
                ListView {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.topMargin: 8
                    clip: true
                    model: bridge.logs
                    spacing: 0
                    delegate: Item {
                        required property var modelData
                        width: ListView.view.width
                        height: 44

                        RowLayout {
                            anchors.fill: parent
                            anchors.bottomMargin: 1
                            spacing: 14
                            Text {
                                text: modelData.time
                                color: Theme.inkFaint
                                font.family: Theme.family
                                font.pixelSize: 11
                                Layout.preferredWidth: 64
                            }
                            Text {
                                Layout.fillWidth: true
                                text: modelData.message
                                color: Theme.ink
                                font.family: Theme.family
                                font.pixelSize: 12
                                wrapMode: Text.Wrap
                                maximumLineCount: 2
                                elide: Text.ElideRight
                            }
                        }
                        Rectangle {
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.bottom: parent.bottom
                            height: 1
                            color: Theme.line
                        }
                    }
                    onCountChanged: positionViewAtEnd()
                }
            }

            Rectangle {
                Layout.fillHeight: true
                Layout.preferredWidth: 1
                color: Theme.line
            }

            ColumnLayout {
                Layout.preferredWidth: 282
                Layout.fillHeight: true
                Layout.leftMargin: 28
                Layout.topMargin: 22
                spacing: 0

                RowLayout {
                    Layout.fillWidth: true
                    Repeater {
                        model: [
                            {label: bridge.tr("경과", bridge.uiLanguage), value: bridge.elapsedText},
                            {label: bridge.tr("남은 시간", bridge.uiLanguage), value: bridge.etaText},
                            {label: bridge.tr("저장", bridge.uiLanguage), value: bridge.lastSavedText}
                        ]
                        delegate: ColumnLayout {
                            required property var modelData
                            Layout.fillWidth: true
                            spacing: 4
                            Text { text: modelData.label; color: Theme.inkFaint; font.family: Theme.family; font.pixelSize: 11 }
                            Text { text: modelData.value; color: Theme.ink; font.family: Theme.family; font.pixelSize: 12; font.weight: Font.DemiBold }
                        }
                    }
                }
                Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; Layout.topMargin: 12; color: Theme.line }

                Text {
                    text: bridge.tr("실행 정보", bridge.uiLanguage)
                    color: Theme.ink
                    font.family: Theme.family
                    font.pixelSize: 12
                    font.weight: Font.DemiBold
                    Layout.topMargin: 20
                }
                GridLayout {
                    Layout.fillWidth: true
                    Layout.topMargin: 9
                    columns: 2
                    rowSpacing: 10
                    columnSpacing: 12
                    Text { text: bridge.tr("전사", bridge.uiLanguage); color: Theme.inkFaint; font.family: Theme.family; font.pixelSize: 11 }
                    Text { text: "large-v3"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 11; font.weight: Font.DemiBold }
                    Text { text: bridge.tr("번역", bridge.uiLanguage); color: Theme.inkFaint; font.family: Theme.family; font.pixelSize: 11 }
                    Text { text: bridge.model; color: Theme.ink; font.family: Theme.family; font.pixelSize: 11; font.weight: Font.DemiBold; elide: Text.ElideRight; Layout.fillWidth: true }
                    Text { text: bridge.tr("사고", bridge.uiLanguage); color: Theme.inkFaint; font.family: Theme.family; font.pixelSize: 11 }
                    Text { text: bridge.reasoning; color: Theme.ink; font.family: Theme.family; font.pixelSize: 11; font.weight: Font.DemiBold }
                    Text { text: bridge.tr("속도", bridge.uiLanguage); color: Theme.inkFaint; font.family: Theme.family; font.pixelSize: 11 }
                    Text { text: bridge.speedMode; color: Theme.ink; font.family: Theme.family; font.pixelSize: 11; font.weight: Font.DemiBold }
                    Text { text: bridge.tr("화자", bridge.uiLanguage); color: Theme.inkFaint; font.family: Theme.family; font.pixelSize: 11 }
                    Text { text: bridge.diarizationEnabled ? bridge.diarizationAlignment : bridge.tr("사용 안 함", bridge.uiLanguage); color: Theme.ink; font.family: Theme.family; font.pixelSize: 11; font.weight: Font.DemiBold }
                    Text { visible: bridge.requestPhase.length > 0; text: bridge.tr("요청", bridge.uiLanguage); color: Theme.inkFaint; font.family: Theme.family; font.pixelSize: 11 }
                    Text { visible: bridge.requestPhase.length > 0; text: bridge.requestPhase; color: Theme.ink; font.family: Theme.family; font.pixelSize: 11; font.weight: Font.DemiBold; elide: Text.ElideRight; Layout.fillWidth: true }
                    Text { visible: bridge.requestPhase.length > 0; text: bridge.tr("배치 경과", bridge.uiLanguage); color: Theme.inkFaint; font.family: Theme.family; font.pixelSize: 11 }
                    Text { visible: bridge.requestPhase.length > 0; text: bridge.requestElapsedText; color: Theme.ink; font.family: Theme.family; font.pixelSize: 11; font.weight: Font.DemiBold }
                    Text { visible: bridge.requestPhase.length > 0; text: bridge.tr("첫 응답", bridge.uiLanguage); color: Theme.inkFaint; font.family: Theme.family; font.pixelSize: 11 }
                    Text { visible: bridge.requestPhase.length > 0; text: bridge.firstEventText; color: Theme.ink; font.family: Theme.family; font.pixelSize: 11; font.weight: Font.DemiBold }
                }
                Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; Layout.topMargin: 18; color: Theme.line }
                Text {
                    Layout.fillWidth: true
                    Layout.topMargin: 18
                    text: bridge.tr("중지해도 완료된 전사 클립과 번역 배치는 보존됩니다.", bridge.uiLanguage)
                    color: Theme.inkSoft
                    font.family: Theme.family
                    font.pixelSize: 12
                    wrapMode: Text.Wrap
                }
                Item { Layout.fillHeight: true }
            }
        }

        FooterBar {
            Layout.fillWidth: true
            title: bridge.tr("작업 데이터 자동 저장", bridge.uiLanguage)
            detail: bridge.tr("다시 실행하면 마지막 안전 지점부터 이어집니다.", bridge.uiLanguage)
            AppButton {
                text: bridge.tr("작업 폴더 열기", bridge.uiLanguage)
                enabled: bridge.outputDir.length > 0
                onClicked: bridge.openOutputFolder()
            }
        }
    }
}
