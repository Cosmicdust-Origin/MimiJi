// qmllint disable unqualified
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."
import "components"

ApplicationWindow {
    id: window
    visible: true
    width: 1240
    height: 760
    minimumWidth: 900
    minimumHeight: 620
    title: bridge.appName
    color: Theme.paper
    font.family: Theme.family
    // Built-in controls (dialogs, scroll bars, text cursors) follow the theme too.
    palette.window: Theme.paper
    palette.windowText: Theme.ink
    palette.base: Theme.paper
    palette.alternateBase: Theme.paperDeep
    palette.text: Theme.ink
    palette.button: Theme.paperDeep
    palette.buttonText: Theme.ink
    palette.brightText: Theme.ink
    palette.highlight: Theme.selection
    palette.highlightedText: Theme.ink
    palette.placeholderText: Theme.inkFaint
    palette.light: Theme.hover
    palette.midlight: Theme.line
    palette.mid: Theme.lineDark
    palette.dark: Theme.lineDark
    palette.shadow: Theme.canvas

    Binding {
        target: Theme
        property: "dark"
        value: bridge.darkMode
    }

    function screenIndex(name) {
        if (name === "home") return 0
        if (name === "running") return 1
        if (name === "jobs") return 2
        if (name === "diagnostic") return 3
        if (name === "settings") return 4
        if (name === "done") return 5
        if (name === "review") return 6
        return 0
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 42
            color: Theme.rail
            Rectangle { anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom; height: 1; color: Theme.line }
            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 17
                anchors.rightMargin: 17
                spacing: 18
                Text { text: bridge.appName; color: Theme.ink; font.family: Theme.family; font.pixelSize: 14; font.weight: Font.DemiBold; Layout.preferredWidth: 178 }
                Text { Layout.fillWidth: true; text: bridge.contextLabel; color: Theme.inkFaint; font.family: Theme.family; font.pixelSize: 13; elide: Text.ElideRight }
                Text { text: bridge.systemSummary; color: Theme.inkSoft; font.family: Theme.family; font.pixelSize: 12 }
                Rectangle { Layout.preferredWidth: 1; Layout.preferredHeight: 16; color: Theme.line }
                // Screen language toggle: 한국어 | 日本語
                Rectangle {
                    id: languageToggle
                    Layout.preferredWidth: languageRow.implicitWidth + 8
                    Layout.preferredHeight: 26
                    radius: 13
                    color: "transparent"
                    border.width: 1
                    border.color: Theme.line
                    Row {
                        id: languageRow
                        anchors.centerIn: parent
                        spacing: 2
                        Repeater {
                            model: [["ko", "한국어"], ["ja", "日本語"]]
                            delegate: Rectangle {
                                id: languageChip
                                required property var modelData
                                readonly property bool active: bridge.uiLanguage === modelData[0]
                                width: chipLabel.implicitWidth + 16
                                height: 20
                                radius: 10
                                color: active ? Theme.selection : (chipMouse.containsMouse ? Theme.hover : "transparent")
                                Accessible.role: Accessible.Button
                                Accessible.name: modelData[1]
                                Text {
                                    id: chipLabel
                                    anchors.centerIn: parent
                                    text: languageChip.modelData[1]
                                    color: languageChip.active ? Theme.ink : Theme.inkSoft
                                    font.family: Theme.family
                                    font.pixelSize: 12
                                    font.weight: languageChip.active ? Font.DemiBold : Font.Normal
                                }
                                MouseArea {
                                    id: chipMouse
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: bridge.uiLanguage = languageChip.modelData[0]
                                }
                            }
                        }
                    }
                }
                // Light / dark theme toggle
                Rectangle {
                    id: themeToggle
                    Layout.preferredWidth: themeLabel.implicitWidth + 22
                    Layout.preferredHeight: 26
                    radius: 13
                    color: themeMouse.containsMouse ? Theme.hover : "transparent"
                    border.width: 1
                    border.color: Theme.line
                    Accessible.role: Accessible.Button
                    Accessible.name: themeLabel.text
                    Text {
                        id: themeLabel
                        anchors.centerIn: parent
                        text: bridge.darkMode ? bridge.tr("☀ 라이트 모드", bridge.uiLanguage) : bridge.tr("☾ 다크 모드", bridge.uiLanguage)
                        color: Theme.ink
                        font.family: Theme.family
                        font.pixelSize: 12
                    }
                    MouseArea {
                        id: themeMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: bridge.darkMode = !bridge.darkMode
                    }
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 0

            Rectangle {
                Layout.preferredWidth: window.width < 1020 ? 144 : 164
                Layout.fillHeight: true
                color: Theme.rail
                Rectangle { anchors.top: parent.top; anchors.bottom: parent.bottom; anchors.right: parent.right; width: 1; color: Theme.line }

                ColumnLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 14
                    anchors.rightMargin: 14
                    anchors.topMargin: 22
                    anchors.bottomMargin: 16
                    spacing: 2

                    Text { text: bridge.tr("작업", bridge.uiLanguage); color: Theme.inkFaint; font.family: Theme.family; font.pixelSize: 11; font.letterSpacing: 0.8; Layout.leftMargin: 8; Layout.bottomMargin: 7 }
                    NavButton { Layout.fillWidth: true; text: bridge.tr("새 작업", bridge.uiLanguage); selected: bridge.screen === "home"; enabled: !bridge.busy; onClicked: bridge.showScreen("home") }
                    NavButton { Layout.fillWidth: true; text: bridge.tr("실행 중", bridge.uiLanguage); selected: bridge.screen === "running"; enabled: bridge.busy || bridge.screen === "running"; onClicked: bridge.showScreen("running") }
                    NavButton { Layout.fillWidth: true; text: bridge.tr("번역 검토", bridge.uiLanguage); visible: bridge.reviewItems.length > 0; selected: bridge.screen === "review"; enabled: !bridge.busy; onClicked: bridge.showScreen("review") }
                    NavButton { Layout.fillWidth: true; text: bridge.tr("이전 작업", bridge.uiLanguage); selected: bridge.screen === "jobs"; enabled: !bridge.busy; onClicked: bridge.showScreen("jobs") }
                    Text { text: bridge.tr("시스템", bridge.uiLanguage); color: Theme.inkFaint; font.family: Theme.family; font.pixelSize: 11; font.letterSpacing: 0.8; Layout.leftMargin: 8; Layout.topMargin: 16; Layout.bottomMargin: 7 }
                    NavButton { Layout.fillWidth: true; text: bridge.tr("환경 진단", bridge.uiLanguage); selected: bridge.screen === "diagnostic"; enabled: !bridge.busy; onClicked: bridge.showScreen("diagnostic") }
                    NavButton { Layout.fillWidth: true; text: bridge.tr("고급 설정", bridge.uiLanguage); selected: bridge.screen === "settings"; enabled: !bridge.busy; onClicked: bridge.showScreen("settings") }
                    NavButton { Layout.fillWidth: true; text: bridge.tr("완료된 작업", bridge.uiLanguage); selected: bridge.screen === "done"; enabled: !bridge.busy && bridge.resultFiles.length > 0; onClicked: bridge.showScreen("done") }
                    Item { Layout.fillHeight: true }
                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.line }
                    Text { text: bridge.appName + " " + bridge.appVersion + bridge.tr("\n로컬 처리 · 자동 저장", bridge.uiLanguage); color: Theme.inkFaint; font.family: Theme.family; font.pixelSize: 11; lineHeight: 1.2; Layout.leftMargin: 8; Layout.topMargin: 10 }
                }
            }

            // StackLayout ignores Layout.margins on its children, so each page sits in a
            // plain Item and gets its gutter through anchors instead.
            StackLayout {
                id: pages
                Layout.fillWidth: true
                Layout.fillHeight: true
                currentIndex: window.screenIndex(bridge.screen)

                readonly property int sideMargin: window.width < 1020 ? 28 : 40
                readonly property int topGap: window.width < 1020 ? 22 : 30
                readonly property int bottomGap: 20

                Item { NewJobPage { anchors.fill: parent; anchors.leftMargin: pages.sideMargin; anchors.rightMargin: pages.sideMargin; anchors.topMargin: pages.topGap; anchors.bottomMargin: pages.bottomGap } }
                Item { RunningPage { anchors.fill: parent; anchors.leftMargin: pages.sideMargin; anchors.rightMargin: pages.sideMargin; anchors.topMargin: pages.topGap; anchors.bottomMargin: pages.bottomGap } }
                Item { JobsPage { anchors.fill: parent; anchors.leftMargin: pages.sideMargin; anchors.rightMargin: pages.sideMargin; anchors.topMargin: pages.topGap; anchors.bottomMargin: pages.bottomGap } }
                Item { DiagnosticPage { anchors.fill: parent; anchors.leftMargin: pages.sideMargin; anchors.rightMargin: pages.sideMargin; anchors.topMargin: pages.topGap; anchors.bottomMargin: pages.bottomGap } }
                Item { SettingsPage { anchors.fill: parent; anchors.leftMargin: pages.sideMargin; anchors.rightMargin: pages.sideMargin; anchors.topMargin: pages.topGap; anchors.bottomMargin: pages.bottomGap } }
                Item { DonePage { anchors.fill: parent; anchors.leftMargin: pages.sideMargin; anchors.rightMargin: pages.sideMargin; anchors.topMargin: pages.topGap; anchors.bottomMargin: pages.bottomGap } }
                Item { ReviewPage { anchors.fill: parent; anchors.leftMargin: pages.sideMargin; anchors.rightMargin: pages.sideMargin; anchors.topMargin: pages.topGap; anchors.bottomMargin: pages.bottomGap } }
            }
        }
    }

    Rectangle {
        id: notice
        width: Math.min(440, window.width - 60)
        height: noticeText.implicitHeight + 26
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.topMargin: 56
        anchors.rightMargin: 20
        color: Theme.notice
        radius: 5
        opacity: 0
        visible: opacity > 0
        z: 100
        Text {
            id: noticeText
            anchors.fill: parent
            anchors.margins: 13
            color: Theme.onNotice
            font.family: Theme.family
            font.pixelSize: 13
            wrapMode: Text.Wrap
            verticalAlignment: Text.AlignVCenter
        }
        Behavior on opacity { NumberAnimation { duration: 150; easing.type: Easing.OutCubic } }
        Timer {
            id: noticeTimer
            interval: 4200
            onTriggered: notice.opacity = 0
        }
    }

    Connections {
        target: bridge
        function onNotification(message) {
            noticeText.text = message
            notice.opacity = 1
            noticeTimer.restart()
        }
    }

    onClosing: function(close) {
        if (bridge.busy || bridge.reviewBusy) {
            close.accepted = false
            quitDialog.open()
        }
    }

    Dialog {
        id: quitDialog
        anchors.centerIn: parent
        modal: true
        title: bridge.tr("작업 중 종료", bridge.uiLanguage)
        standardButtons: Dialog.Ok | Dialog.Cancel
        Text {
            text: bridge.tr("실행 중인 작업을 안전 지점까지 저장하고 종료합니다.\n번역 CLI 프로세스도 함께 종료합니다.", bridge.uiLanguage)
            font.family: Theme.family
            font.pixelSize: 14
            color: Theme.ink
        }
        onAccepted: bridge.requestQuit()
    }

    // Re-check login state when the user comes back from an official login console.
    onActiveChanged: {
        if (active)
            bridge.refreshAccountsIfStale()
    }

    Component.onCompleted: bridge.initialize()
}
