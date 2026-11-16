// qmllint disable unqualified
import QtQuick
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
            eyebrow: "New job"
            title: bridge.resumeSettingsMode ? "번역 설정을 바꿔서 이어하기" : "자막 작업"
            description: bridge.resumeSettingsMode
                         ? "완료된 배치는 유지하고 남은 배치에만 새 설정을 적용합니다."
                         : "영상과 번역 설정을 확인한 뒤 작업을 시작합니다."
            // The transcription device summary is shown once, in the window's top bar.
            Text {
                visible: bridge.diarizationEnabled
                text: bridge.diarizationReady ? "화자 분리 사용" : "화자 분리 준비 필요"
                color: bridge.diarizationReady ? Theme.inkFaint : Theme.danger
                font.family: Theme.family
                font.pixelSize: 12
            }
            AppButton {
                visible: bridge.resumeSettingsMode
                text: "변경 취소"
                kind: "quiet"
                onClicked: bridge.cancelResumeSettings()
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 0

            ColumnLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.preferredWidth: 620
                Layout.rightMargin: 28
                Layout.topMargin: 22
                spacing: 0

                Text {
                    text: "입력 영상"
                    color: Theme.ink
                    font.family: Theme.family
                    font.pixelSize: 15
                    font.weight: Font.DemiBold
                }
                Text {
                    text: "MP4, MKV, MOV, AVI"
                    color: Theme.inkFaint
                    font.family: Theme.family
                    font.pixelSize: 12
                    Layout.topMargin: 4
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 1
                    Layout.topMargin: 16
                    color: Theme.line
                }

                RowLayout {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 60
                    spacing: 16
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 4
                        Text {
                            Layout.fillWidth: true
                            text: bridge.fileName
                            color: Theme.ink
                            font.family: Theme.family
                            font.pixelSize: 15
                            font.weight: Font.DemiBold
                            elide: Text.ElideMiddle
                        }
                        Text {
                            text: bridge.fileMeta
                            color: Theme.inkFaint
                            font.family: Theme.family
                            font.pixelSize: 12
                        }
                    }
                    AppButton {
                        text: bridge.videoPath ? "다른 파일" : "파일 선택"
                        kind: "quiet"
                        enabled: !bridge.busy && !bridge.resumeSettingsMode
                        onClicked: bridge.chooseVideo()
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 1
                    color: Theme.line
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 122
                    Layout.topMargin: 18
                    color: "transparent"
                    border.width: 1
                    border.color: dropArea.containsDrag ? Theme.ink : Theme.lineDark

                    Column {
                        anchors.centerIn: parent
                        spacing: 6
                        Text {
                            anchors.horizontalCenter: parent.horizontalCenter
                            text: "영상을 끌어 놓거나 파일을 선택합니다."
                            color: Theme.ink
                            font.family: Theme.family
                            font.pixelSize: 14
                            font.weight: Font.DemiBold
                        }
                        Text {
                            anchors.horizontalCenter: parent.horizontalCenter
                            text: "현재 파일을 교체해도 기존 작업 데이터는 임의로 삭제하지 않습니다."
                            color: Theme.inkFaint
                            font.family: Theme.family
                            font.pixelSize: 12
                        }
                    }
                    DropArea {
                        id: dropArea
                        anchors.fill: parent
                        enabled: !bridge.busy && !bridge.resumeSettingsMode
                        onDropped: drop => {
                            if (drop.urls.length > 0)
                                bridge.acceptDroppedUrl(drop.urls[0].toString())
                        }
                    }
                    MouseArea {
                        anchors.fill: parent
                        enabled: !bridge.busy && !bridge.resumeSettingsMode
                        cursorShape: Qt.PointingHandCursor
                        onClicked: bridge.chooseVideo()
                    }
                }
                Item { Layout.fillHeight: true }
            }

            Rectangle {
                Layout.fillHeight: true
                Layout.preferredWidth: 1
                color: Theme.line
            }

            ColumnLayout {
                Layout.preferredWidth: 420
                Layout.fillHeight: true
                Layout.leftMargin: 28
                Layout.topMargin: 22
                spacing: 0

                Text {
                    text: "번역"
                    color: Theme.ink
                    font.family: Theme.family
                    font.pixelSize: 15
                    font.weight: Font.DemiBold
                }
                Text {
                    text: "구독 로그인 · API 과금 사용 안 함"
                    color: Theme.inkFaint
                    font.family: Theme.family
                    font.pixelSize: 12
                    Layout.topMargin: 4
                }
                RowLayout {
                    Layout.fillWidth: true
                    Layout.topMargin: 8
                    spacing: 10
                    Text {
                        Layout.fillWidth: true
                        text: bridge.accountsBusy ? "계정 상태를 확인하고 있습니다." : bridge.providerAccountText
                        color: bridge.providerAccountReady || bridge.accountsBusy ? Theme.inkSoft : Theme.danger
                        font.family: Theme.family
                        font.pixelSize: 12
                        elide: Text.ElideRight
                    }
                    Text {
                        text: "계정 관리"
                        color: Theme.ink
                        font.family: Theme.family
                        font.pixelSize: 12
                        font.underline: accountLink.containsMouse
                        MouseArea {
                            id: accountLink
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            enabled: !bridge.busy
                            onClicked: bridge.showScreen("settings")
                        }
                    }
                }

                GridLayout {
                    Layout.fillWidth: true
                    Layout.topMargin: 14
                    columns: 2
                    columnSpacing: 16
                    rowSpacing: 0

                    Text { text: "언어"; color: Theme.inkSoft; font.family: Theme.family; font.pixelSize: 12 }
                    ChoiceBox {
                        Layout.fillWidth: true
                        model: bridge.languagePairLabels
                        currentIndex: Math.max(0, bridge.languagePairOptions.indexOf(bridge.languagePair))
                        enabled: !bridge.busy && !bridge.resumeSettingsMode
                        onActivated: index => bridge.languagePair = bridge.languagePairOptions[index]
                    }
                    Text { text: "제공자"; color: Theme.inkSoft; font.family: Theme.family; font.pixelSize: 12 }
                    ChoiceBox {
                        Layout.fillWidth: true
                        model: ["codex", "claude"]
                        textRole: ""
                        currentIndex: model.indexOf(bridge.provider)
                        enabled: !bridge.busy
                        displayText: bridge.providerLabel
                        onActivated: bridge.provider = currentText
                    }
                    Text { text: "모델"; color: Theme.inkSoft; font.family: Theme.family; font.pixelSize: 12 }
                    ChoiceBox {
                        Layout.fillWidth: true
                        model: bridge.modelLabels
                        currentIndex: Math.max(0, bridge.modelOptions.indexOf(bridge.model))
                        enabled: !bridge.busy
                        onActivated: index => bridge.model = bridge.modelOptions[index]
                    }
                    Text { text: "사고 수준"; color: Theme.inkSoft; font.family: Theme.family; font.pixelSize: 12 }
                    ChoiceBox {
                        Layout.fillWidth: true
                        model: bridge.reasoningLabels
                        currentIndex: Math.max(0, bridge.reasoningOptions.indexOf(bridge.reasoning))
                        enabled: !bridge.busy
                        onActivated: index => bridge.reasoning = bridge.reasoningOptions[index]
                    }
                    Text { text: "처리 속도"; color: Theme.inkSoft; font.family: Theme.family; font.pixelSize: 12 }
                    ChoiceBox {
                        Layout.fillWidth: true
                        model: bridge.speedLabels
                        currentIndex: Math.max(0, bridge.speedOptions.indexOf(bridge.speedMode))
                        enabled: !bridge.busy && bridge.provider === "codex"
                        onActivated: index => bridge.speedMode = bridge.speedOptions[index]
                    }
                    Text { text: "작품 제목"; color: Theme.inkSoft; font.family: Theme.family; font.pixelSize: 12 }
                    LineField {
                        Layout.fillWidth: true
                        text: bridge.code
                        placeholderText: "영상을 고르면 파일 이름에서 채웁니다"
                        enabled: !bridge.busy && !bridge.resumeSettingsMode
                        onTextEdited: bridge.code = text
                    }
                }
                Text {
                    Layout.fillWidth: true
                    Layout.topMargin: 4
                    text: "파일 이름에서 자동으로 채운 제목입니다. 결과 파일 이름에 쓰이니 필요하면 고치십시오."
                    color: Theme.inkFaint
                    font.family: Theme.family
                    font.pixelSize: 11
                    wrapMode: Text.WordWrap
                }

                Text {
                    text: "출력 위치"
                    color: Theme.ink
                    font.family: Theme.family
                    font.pixelSize: 15
                    font.weight: Font.DemiBold
                    Layout.topMargin: 24
                }
                RowLayout {
                    Layout.fillWidth: true
                    Layout.topMargin: 8
                    LineField {
                        Layout.fillWidth: true
                        text: bridge.outputDir
                        placeholderText: "출력 폴더를 선택하십시오."
                        enabled: !bridge.busy && !bridge.resumeSettingsMode
                        onTextEdited: bridge.outputDir = text
                    }
                    AppButton {
                        text: "선택"
                        kind: "quiet"
                        enabled: !bridge.busy && !bridge.resumeSettingsMode
                        onClicked: bridge.chooseOutput()
                    }
                }
                Text {
                    Layout.fillWidth: true
                    Layout.topMargin: 4
                    text: "따로 고르지 않으면 영상이 있는 폴더 안에 작품 제목 이름의 폴더를 만들어 저장합니다."
                    color: Theme.inkFaint
                    font.family: Theme.family
                    font.pixelSize: 11
                    wrapMode: Text.WordWrap
                }
                Item { Layout.fillHeight: true }
            }
        }

        FooterBar {
            Layout.fillWidth: true
            title: bridge.resumeSettingsMode
                   ? "남은 번역 배치에 적용할 설정을 확인하십시오."
                   : bridge.diarizationEnabled && !bridge.diarizationReady
                   ? "고급 설정에서 화자 분리를 준비하십시오."
                   : bridge.canStart
                     ? "작업을 시작할 수 있습니다."
                     : "영상과 출력 폴더를 선택하십시오."
            detail: bridge.resumeSettingsMode
                    ? "완료된 배치와 보류 배치는 자동으로 다시 요청하지 않습니다."
                    : "완료 파일 6개를 출력합니다. 전사와 번역 체크포인트는 자동 저장됩니다."
            AppButton {
                text: bridge.resumeSettingsMode ? "새 설정으로 이어하기" : "자막 생성"
                kind: "primary"
                enabled: bridge.canStart
                onClicked: bridge.resumeSettingsMode
                           ? bridge.startResumeWithChangedSettings()
                           : bridge.startJob()
            }
        }
    }
}
