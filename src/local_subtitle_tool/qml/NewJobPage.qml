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
            eyebrow: "New job"
            title: bridge.resumeSettingsMode ? bridge.tr("번역 설정을 바꿔서 이어하기", bridge.uiLanguage) : bridge.tr("자막 작업", bridge.uiLanguage)
            description: bridge.resumeSettingsMode
                         ? bridge.tr("완료된 배치는 유지하고 남은 배치에만 새 설정을 적용합니다.", bridge.uiLanguage)
                         : bridge.tr("영상과 번역 설정을 확인한 뒤 작업을 시작합니다.", bridge.uiLanguage)
            // The transcription device summary is shown once, in the window's top bar.
            Text {
                visible: bridge.diarizationEnabled
                text: bridge.diarizationReady ? bridge.tr("화자 분리 사용", bridge.uiLanguage) : bridge.tr("화자 분리 준비 필요", bridge.uiLanguage)
                color: bridge.diarizationReady ? Theme.inkFaint : Theme.danger
                font.family: Theme.family
                font.pixelSize: 12
            }
            AppButton {
                visible: bridge.resumeSettingsMode
                text: bridge.tr("변경 취소", bridge.uiLanguage)
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
                    text: bridge.tr("입력 영상", bridge.uiLanguage)
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
                        text: bridge.videoPath ? bridge.tr("다른 파일", bridge.uiLanguage) : bridge.tr("파일 선택", bridge.uiLanguage)
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
                            text: bridge.tr("영상을 끌어 놓거나 파일을 선택합니다.", bridge.uiLanguage)
                            color: Theme.ink
                            font.family: Theme.family
                            font.pixelSize: 14
                            font.weight: Font.DemiBold
                        }
                        Text {
                            anchors.horizontalCenter: parent.horizontalCenter
                            text: bridge.tr("현재 파일을 교체해도 기존 작업 데이터는 임의로 삭제하지 않습니다.", bridge.uiLanguage)
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

            // The settings column scrolls when it is taller than the window.
            ScrollView {
                id: settingsScroll
                Layout.preferredWidth: 440
                Layout.fillHeight: true
                Layout.leftMargin: 28
                clip: true
                contentWidth: availableWidth
                contentHeight: settingsColumn.implicitHeight + 40

            ColumnLayout {
                id: settingsColumn
                y: 22
                width: settingsScroll.availableWidth - 14
                spacing: 0

                Text {
                    text: bridge.tr("번역", bridge.uiLanguage)
                    color: Theme.ink
                    font.family: Theme.family
                    font.pixelSize: 15
                    font.weight: Font.DemiBold
                }
                Text {
                    text: bridge.tr("구독 로그인 · API 과금 사용 안 함", bridge.uiLanguage)
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
                        text: bridge.accountsBusy ? bridge.tr("계정 상태를 확인하고 있습니다.", bridge.uiLanguage) : bridge.providerAccountText
                        color: bridge.providerAccountReady || bridge.accountsBusy ? Theme.inkSoft : Theme.danger
                        font.family: Theme.family
                        font.pixelSize: 12
                        elide: Text.ElideRight
                    }
                    Text {
                        text: bridge.tr("계정 관리", bridge.uiLanguage)
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

                    Text { text: bridge.tr("언어", bridge.uiLanguage); color: Theme.inkSoft; font.family: Theme.family; font.pixelSize: 12 }
                    ChoiceBox {
                        Layout.fillWidth: true
                        model: bridge.languagePairLabels
                        currentIndex: Math.max(0, bridge.languagePairOptions.indexOf(bridge.languagePair))
                        enabled: !bridge.busy && !bridge.resumeSettingsMode
                        onActivated: index => bridge.languagePair = bridge.languagePairOptions[index]
                    }
                    Text { text: bridge.tr("제공자", bridge.uiLanguage); color: Theme.inkSoft; font.family: Theme.family; font.pixelSize: 12 }
                    ChoiceBox {
                        Layout.fillWidth: true
                        model: ["codex", "claude"]
                        textRole: ""
                        currentIndex: model.indexOf(bridge.provider)
                        enabled: !bridge.busy
                        displayText: bridge.providerLabel
                        onActivated: bridge.provider = currentText
                    }
                    Text { text: bridge.tr("모델", bridge.uiLanguage); color: Theme.inkSoft; font.family: Theme.family; font.pixelSize: 12 }
                    ChoiceBox {
                        Layout.fillWidth: true
                        model: bridge.modelLabels
                        currentIndex: Math.max(0, bridge.modelOptions.indexOf(bridge.model))
                        enabled: !bridge.busy
                        onActivated: index => bridge.model = bridge.modelOptions[index]
                    }
                    Text { text: bridge.tr("사고 수준", bridge.uiLanguage); color: Theme.inkSoft; font.family: Theme.family; font.pixelSize: 12 }
                    ChoiceBox {
                        Layout.fillWidth: true
                        model: bridge.reasoningLabels
                        currentIndex: Math.max(0, bridge.reasoningOptions.indexOf(bridge.reasoning))
                        enabled: !bridge.busy
                        onActivated: index => bridge.reasoning = bridge.reasoningOptions[index]
                    }
                    Text { text: bridge.tr("처리 속도", bridge.uiLanguage); color: Theme.inkSoft; font.family: Theme.family; font.pixelSize: 12 }
                    ChoiceBox {
                        Layout.fillWidth: true
                        model: bridge.speedLabels
                        currentIndex: Math.max(0, bridge.speedOptions.indexOf(bridge.speedMode))
                        enabled: !bridge.busy && bridge.provider === "codex"
                        onActivated: index => bridge.speedMode = bridge.speedOptions[index]
                    }
                    Text { text: bridge.tr("작품 제목", bridge.uiLanguage); color: Theme.inkSoft; font.family: Theme.family; font.pixelSize: 12 }
                    LineField {
                        Layout.fillWidth: true
                        text: bridge.code
                        placeholderText: bridge.tr("영상을 고르면 파일 이름에서 채웁니다", bridge.uiLanguage)
                        enabled: !bridge.busy && !bridge.resumeSettingsMode
                        onTextEdited: bridge.code = text
                    }
                }
                Text {
                    Layout.fillWidth: true
                    Layout.topMargin: 4
                    text: bridge.tr("파일 이름에서 자동으로 채운 제목입니다. 결과 파일 이름에 쓰이니 필요하면 고치십시오.", bridge.uiLanguage)
                    color: Theme.inkFaint
                    font.family: Theme.family
                    font.pixelSize: 11
                    wrapMode: Text.WordWrap
                }
                Text {
                    text: bridge.tr("용어·고유명사 힌트 (선택)", bridge.uiLanguage)
                    color: Theme.inkSoft
                    font.family: Theme.family
                    font.pixelSize: 12
                    Layout.topMargin: 14
                }
                LineField {
                    Layout.fillWidth: true
                    Layout.topMargin: 4
                    text: bridge.termHints
                    placeholderText: bridge.tr("예: 역모, 도승지, 중전 (쉼표로 구분)", bridge.uiLanguage)
                    enabled: !bridge.busy && !bridge.resumeSettingsMode
                    onTextEdited: bridge.termHints = text
                }
                Text {
                    Layout.fillWidth: true
                    Layout.topMargin: 4
                    text: bridge.tr("자주 나오는 이름이나 용어를 적으면 전사와 번역이 그 표기로 맞춰집니다.", bridge.uiLanguage)
                    color: Theme.inkFaint
                    font.family: Theme.family
                    font.pixelSize: 11
                    wrapMode: Text.WordWrap
                }

                Text {
                    text: bridge.tr("출력 위치", bridge.uiLanguage)
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
                        placeholderText: bridge.tr("출력 폴더를 선택하십시오.", bridge.uiLanguage)
                        enabled: !bridge.busy && !bridge.resumeSettingsMode
                        onTextEdited: bridge.outputDir = text
                    }
                    AppButton {
                        text: bridge.tr("선택", bridge.uiLanguage)
                        kind: "quiet"
                        enabled: !bridge.busy && !bridge.resumeSettingsMode
                        onClicked: bridge.chooseOutput()
                    }
                }
                Text {
                    Layout.fillWidth: true
                    Layout.topMargin: 4
                    text: bridge.tr("따로 고르지 않으면 영상이 있는 폴더 안에 작품 제목 이름의 폴더를 만들어 저장합니다.", bridge.uiLanguage)
                    color: Theme.inkFaint
                    font.family: Theme.family
                    font.pixelSize: 11
                    wrapMode: Text.WordWrap
                }
            }
            }
        }

        FooterBar {
            Layout.fillWidth: true
            title: bridge.resumeSettingsMode
                   ? bridge.tr("남은 번역 배치에 적용할 설정을 확인하십시오.", bridge.uiLanguage)
                   : bridge.diarizationEnabled && !bridge.diarizationReady
                   ? bridge.tr("고급 설정에서 화자 분리를 준비하십시오.", bridge.uiLanguage)
                   : bridge.canStart
                     ? bridge.tr("작업을 시작할 수 있습니다.", bridge.uiLanguage)
                     : bridge.tr("영상과 출력 폴더를 선택하십시오.", bridge.uiLanguage)
            detail: bridge.resumeSettingsMode
                    ? bridge.tr("완료된 배치와 보류 배치는 자동으로 다시 요청하지 않습니다.", bridge.uiLanguage)
                    : bridge.tr("완료 파일 6개를 출력합니다. 전사와 번역 체크포인트는 자동 저장됩니다.", bridge.uiLanguage)
            AppButton {
                text: bridge.resumeSettingsMode ? bridge.tr("새 설정으로 이어하기", bridge.uiLanguage) : bridge.tr("자막 생성", bridge.uiLanguage)
                kind: "primary"
                enabled: bridge.canStart
                onClicked: bridge.resumeSettingsMode
                           ? bridge.startResumeWithChangedSettings()
                           : bridge.startJob()
            }
        }
    }
}
