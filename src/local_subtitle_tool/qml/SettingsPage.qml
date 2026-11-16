// qmllint disable unqualified
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."
import "components"

Item {
    component SettingLabel: ColumnLayout {
        property string title: ""
        property string detail: ""
        spacing: 3
        Text { text: parent.title; color: Theme.ink; font.family: Theme.family; font.pixelSize: 13; font.weight: Font.DemiBold }
        Text { text: parent.detail; color: Theme.inkFaint; font.family: Theme.family; font.pixelSize: 11; wrapMode: Text.Wrap; Layout.fillWidth: true }
    }

    Dialog {
        id: logoutDialog
        property string provider: ""
        property string providerLabel: ""
        anchors.centerIn: parent
        modal: true
        title: bridge.tr("로그아웃", bridge.uiLanguage)
        standardButtons: Dialog.Ok | Dialog.Cancel
        Text {
            text: logoutDialog.providerLabel + bridge.tr("에서 로그아웃합니다.\n다시 번역하려면 공식 로그인 창에서 다시 로그인해야 합니다.", bridge.uiLanguage)
            font.family: Theme.family
            font.pixelSize: 14
            color: Theme.ink
        }
        onAccepted: bridge.logoutProvider(logoutDialog.provider)
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        PageHeader {
            Layout.fillWidth: true
            eyebrow: "Settings"
            title: bridge.tr("고급 설정", bridge.uiLanguage)
            description: bridge.tr("기본 설정만으로도 작업할 수 있습니다. 필요한 항목만 변경하십시오.", bridge.uiLanguage)
            AppButton { text: bridge.tr("기본값 복원", bridge.uiLanguage); enabled: !bridge.busy; onClicked: bridge.resetSettings() }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 0

            ScrollView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.rightMargin: 30
                contentWidth: availableWidth
                clip: true

                ColumnLayout {
                    width: parent.width
                    spacing: 0

                    RowLayout {
                        Layout.fillWidth: true
                        Layout.topMargin: 18
                        Text { Layout.fillWidth: true; text: bridge.tr("번역 계정", bridge.uiLanguage); color: Theme.ink; font.family: Theme.family; font.pixelSize: 15; font.weight: Font.DemiBold }
                        AppButton { text: bridge.accountsBusy ? bridge.tr("확인 중", bridge.uiLanguage) : bridge.tr("상태 확인", bridge.uiLanguage); kind: "quiet"; enabled: !bridge.busy && !bridge.accountsBusy; onClicked: bridge.refreshAccounts() }
                    }
                    Text {
                        Layout.fillWidth: true
                        Layout.topMargin: 2
                        text: bridge.tr("로그인과 로그아웃은 공식 CLI 창에서 처리합니다. 앱은 계정 토큰을 저장하지 않습니다.", bridge.uiLanguage)
                        color: Theme.inkFaint
                        font.family: Theme.family
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
                    }
                    Repeater {
                        model: bridge.accounts
                        delegate: ColumnLayout {
                            id: accountRow
                            required property var modelData
                            Layout.fillWidth: true
                            spacing: 0
                            RowLayout {
                                Layout.fillWidth: true
                                Layout.minimumHeight: 58
                                spacing: 12
                                SettingLabel {
                                    Layout.fillWidth: true
                                    Layout.preferredWidth: 320
                                    title: accountRow.modelData.label + (accountRow.modelData.version ? "  " + accountRow.modelData.version : "")
                                    detail: accountRow.modelData.detail
                                }
                                Text {
                                    text: accountRow.modelData.state
                                    color: accountRow.modelData.problem ? Theme.danger : Theme.inkSoft
                                    font.family: Theme.family
                                    font.pixelSize: 12
                                }
                                AppButton {
                                    text: accountRow.modelData.logged_in ? bridge.tr("계정 전환", bridge.uiLanguage) : bridge.tr("로그인", bridge.uiLanguage)
                                    kind: "quiet"
                                    visible: accountRow.modelData.installed
                                    enabled: !bridge.busy && !bridge.accountsBusy
                                    onClicked: bridge.loginProvider(accountRow.modelData.provider)
                                }
                                AppButton {
                                    text: bridge.tr("로그아웃", bridge.uiLanguage)
                                    kind: "danger"
                                    visible: accountRow.modelData.logged_in
                                    enabled: !bridge.busy && !bridge.accountsBusy
                                    onClicked: {
                                        logoutDialog.provider = accountRow.modelData.provider
                                        logoutDialog.providerLabel = accountRow.modelData.label
                                        logoutDialog.open()
                                    }
                                }
                            }
                            Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.line }
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        Layout.minimumHeight: 58
                        SettingLabel { Layout.fillWidth: true; Layout.preferredWidth: 320; title: bridge.tr("Codex 세션 유지", bridge.uiLanguage); detail: bridge.tr("한 작업의 번역 배치를 같은 Codex 세션에서 이어 처리합니다. 거절이 나오면 세션을 폐기하고 새 세션으로 계속합니다. 끄면 배치마다 독립 세션을 사용합니다.", bridge.uiLanguage) }
                        ChoiceBox {
                            Layout.preferredWidth: 210
                            model: [bridge.tr("유지 (권장)", bridge.uiLanguage), bridge.tr("배치마다 새 세션", bridge.uiLanguage)]
                            currentIndex: bridge.codexSessions ? 0 : 1
                            enabled: !bridge.busy
                            onActivated: bridge.codexSessions = currentIndex === 0
                        }
                    }
                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.lineDark }

                    Text { text: bridge.tr("전사", bridge.uiLanguage); color: Theme.ink; font.family: Theme.family; font.pixelSize: 15; font.weight: Font.DemiBold; Layout.topMargin: 22 }
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.minimumHeight: 58
                        SettingLabel { Layout.fillWidth: true; Layout.preferredWidth: 320; title: bridge.tr("연산 장치", bridge.uiLanguage); detail: bridge.tr("CUDA를 우선하며 사용할 수 없을 때 자동으로 CPU로 바꾸지 않습니다.", bridge.uiLanguage) }
                        ChoiceBox {
                            Layout.preferredWidth: 210
                            model: ["auto", "cuda", "cpu"]
                            currentIndex: model.indexOf(bridge.accelerator)
                            enabled: !bridge.busy
                            onActivated: bridge.accelerator = currentText
                        }
                    }
                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.line }
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.minimumHeight: 58
                        SettingLabel { Layout.fillWidth: true; Layout.preferredWidth: 320; title: bridge.tr("CPU 스레드", bridge.uiLanguage); detail: bridge.tr("0은 자동입니다. CPU 모드에서도 시스템 응답성을 남깁니다.", bridge.uiLanguage) }
                        LineField {
                            Layout.preferredWidth: 210
                            text: String(bridge.cpuThreads)
                            enabled: !bridge.busy
                            validator: IntValidator { bottom: 0; top: 256 }
                            onEditingFinished: bridge.cpuThreads = Number(text)
                        }
                    }
                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.lineDark }

                    Text { text: bridge.tr("화자 분리", bridge.uiLanguage); color: Theme.ink; font.family: Theme.family; font.pixelSize: 15; font.weight: Font.DemiBold; Layout.topMargin: 22 }
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.minimumHeight: 58
                        SettingLabel {
                            Layout.fillWidth: true
                            Layout.preferredWidth: 320
                            title: bridge.tr("화자 분리 사용", bridge.uiLanguage)
                            detail: bridge.diarizationAvailable
                                    ? bridge.tr("번역에 화자 문맥을 전달하고 화자가 바뀌는 지점에서 큐 병합을 막습니다.", bridge.uiLanguage)
                                    : bridge.tr("이 배포판에는 화자 분리가 포함되어 있지 않습니다. MimiJi-Diarization 배포판을 사용하십시오.", bridge.uiLanguage)
                        }
                        ChoiceBox {
                            objectName: "diarizationEnabledChoice"
                            Layout.preferredWidth: 210
                            model: [bridge.tr("사용 안 함", bridge.uiLanguage), bridge.tr("사용", bridge.uiLanguage)]
                            currentIndex: bridge.diarizationEnabled ? 1 : 0
                            enabled: !bridge.busy && bridge.diarizationAvailable
                            onActivated: bridge.diarizationEnabled = currentIndex === 1
                        }
                    }
                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.line }
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.minimumHeight: 58
                        SettingLabel { Layout.fillWidth: true; Layout.preferredWidth: 320; title: bridge.tr("준비 상태", bridge.uiLanguage); detail: bridge.tr("PyTorch를 포함한 수 GB 규모의 선택 설치 → 이용 조건 동의 → HF 로그인 → 모델 준비 순서입니다.", bridge.uiLanguage) }
                        ColumnLayout {
                            Layout.preferredWidth: 420
                            spacing: 7
                            RowLayout {
                                Layout.fillWidth: true
                                Text { Layout.fillWidth: true; text: bridge.diarizationStatus; color: bridge.diarizationReady ? Theme.ink : Theme.inkSoft; font.family: Theme.family; font.pixelSize: 12; elide: Text.ElideRight }
                                AppButton { text: bridge.tr("상태 확인", bridge.uiLanguage); kind: "quiet"; enabled: !bridge.busy && !bridge.diarizationBusy; onClicked: bridge.refreshDiarizationStatus() }
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                AppButton { text: bridge.tr("설치", bridge.uiLanguage); kind: "quiet"; visible: bridge.canInstallPackages; enabled: !bridge.busy && !bridge.diarizationBusy; onClicked: bridge.installDiarizationDependencies() }
                                AppButton { objectName: "diarizationAgreementButton"; text: bridge.tr("이용 조건", bridge.uiLanguage); kind: "quiet"; enabled: !bridge.busy && !bridge.diarizationBusy && bridge.diarizationAvailable; onClicked: bridge.openDiarizationAgreement() }
                                AppButton { objectName: "diarizationLoginButton"; text: bridge.tr("HF 로그인", bridge.uiLanguage); kind: "quiet"; enabled: !bridge.busy && !bridge.diarizationBusy && bridge.diarizationAvailable; onClicked: bridge.loginHuggingFace() }
                                AppButton { objectName: "diarizationDownloadButton"; text: bridge.diarizationBusy ? bridge.tr("준비 중", bridge.uiLanguage) : bridge.tr("모델 받기", bridge.uiLanguage); kind: "quiet"; enabled: !bridge.busy && !bridge.diarizationBusy && bridge.diarizationAvailable; onClicked: bridge.downloadDiarizationModel() }
                            }
                        }
                    }
                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.line }
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.minimumHeight: 58
                        SettingLabel { Layout.fillWidth: true; Layout.preferredWidth: 320; title: bridge.tr("예상 화자 수", bridge.uiLanguage); detail: bridge.tr("자동은 화자 수를 모델이 판단합니다. 알고 있다면 직접 지정하는 편이 안정적입니다.", bridge.uiLanguage) }
                        ChoiceBox {
                            Layout.preferredWidth: 210
                            model: [bridge.tr("자동", bridge.uiLanguage), bridge.tr("2명", bridge.uiLanguage), bridge.tr("3명", bridge.uiLanguage), bridge.tr("4명", bridge.uiLanguage), bridge.tr("5명", bridge.uiLanguage), bridge.tr("6명", bridge.uiLanguage)]
                            currentIndex: bridge.diarizationSpeakers === 0 ? 0 : Math.max(1, Math.min(5, bridge.diarizationSpeakers - 1))
                            enabled: !bridge.busy && bridge.diarizationAvailable && bridge.diarizationEnabled
                            onActivated: bridge.diarizationSpeakers = currentIndex === 0 ? 0 : currentIndex + 1
                        }
                    }
                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.line }
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.minimumHeight: 58
                        SettingLabel { Layout.fillWidth: true; Layout.preferredWidth: 320; title: bridge.tr("화자 정렬", bridge.uiLanguage); detail: bridge.tr("정밀은 large-v3의 단어 타임스탬프까지 사용하며 처리량이 조금 늘어납니다.", bridge.uiLanguage) }
                        ChoiceBox {
                            Layout.preferredWidth: 210
                            model: [bridge.tr("기본", bridge.uiLanguage), bridge.tr("정밀", bridge.uiLanguage)]
                            currentIndex: bridge.diarizationAlignment === "precise" ? 1 : 0
                            enabled: !bridge.busy && bridge.diarizationAvailable && bridge.diarizationEnabled
                            onActivated: bridge.diarizationAlignment = currentIndex === 1 ? "precise" : "basic"
                        }
                    }
                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.line }
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.minimumHeight: 58
                        SettingLabel { Layout.fillWidth: true; Layout.preferredWidth: 320; title: bridge.tr("화자 분리 장치", bridge.uiLanguage); detail: bridge.tr("자동은 PyTorch CUDA를 먼저 사용하고 사용할 수 없으면 CPU를 사용합니다.", bridge.uiLanguage) }
                        ChoiceBox {
                            Layout.preferredWidth: 210
                            model: [bridge.tr("자동", bridge.uiLanguage), "CUDA", "CPU"]
                            currentIndex: bridge.diarizationDevice === "cuda" ? 1 : bridge.diarizationDevice === "cpu" ? 2 : 0
                            enabled: !bridge.busy && bridge.diarizationAvailable && bridge.diarizationEnabled
                            onActivated: bridge.diarizationDevice = currentIndex === 1 ? "cuda" : currentIndex === 2 ? "cpu" : "auto"
                        }
                    }
                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.line }
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.minimumHeight: 58
                        SettingLabel { Layout.fillWidth: true; Layout.preferredWidth: 320; title: bridge.tr("SRT 화자 표시", bridge.uiLanguage); detail: bridge.tr("기본값은 표시 안 함입니다. 내부 화자 문맥은 이 설정과 무관하게 사용합니다.", bridge.uiLanguage) }
                        ChoiceBox {
                            Layout.preferredWidth: 210
                            model: [bridge.tr("표시 안 함", bridge.uiLanguage), bridge.tr("표시", bridge.uiLanguage)]
                            currentIndex: bridge.diarizationSrtLabels ? 1 : 0
                            enabled: !bridge.busy && bridge.diarizationAvailable && bridge.diarizationEnabled
                            onActivated: bridge.diarizationSrtLabels = currentIndex === 1
                        }
                    }
                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.lineDark }

                    Text { text: bridge.tr("복구", bridge.uiLanguage); color: Theme.ink; font.family: Theme.family; font.pixelSize: 15; font.weight: Font.DemiBold; Layout.topMargin: 22 }
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.minimumHeight: 58
                        SettingLabel { Layout.fillWidth: true; title: bridge.tr("미완료 작업", bridge.uiLanguage); detail: bridge.tr("앱을 다시 열면 마지막 안전 지점부터 이어갈 작업을 표시합니다.", bridge.uiLanguage) }
                        Text { text: bridge.tr("자동 저장", bridge.uiLanguage); color: Theme.ink; font.family: Theme.family; font.pixelSize: 12; font.weight: Font.DemiBold }
                    }
                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.line }
                    Item { Layout.fillHeight: true; Layout.minimumHeight: 30 }
                }
            }

            Rectangle { Layout.fillHeight: true; Layout.preferredWidth: 1; color: Theme.line }

            Text {
                Layout.preferredWidth: 250
                Layout.fillHeight: true
                Layout.leftMargin: 28
                Layout.topMargin: 22
                text: bridge.tr("고급 설정은 새 작업에 적용됩니다.\n\n진행 중인 작업의 모델, 연산 방식 또는 화자 설정을 바꾸면 기존 체크포인트와 호환되지 않을 수 있습니다. 앱은 설정 지문이 다르면 자동으로 재사용하지 않습니다.", bridge.uiLanguage)
                color: Theme.inkSoft
                font.family: Theme.family
                font.pixelSize: 12
                wrapMode: Text.Wrap
                lineHeight: 1.2
            }
        }

        FooterBar {
            Layout.fillWidth: true
            title: bridge.tr("설정은 이 컴퓨터의 사용자 프로필에 저장됩니다.", bridge.uiLanguage)
            detail: bridge.tr("진행 중에는 설정을 변경할 수 없습니다.", bridge.uiLanguage)
            AppButton { text: bridge.tr("설정 저장", bridge.uiLanguage); kind: "primary"; enabled: !bridge.busy; onClicked: bridge.saveSettings() }
        }
    }
}
