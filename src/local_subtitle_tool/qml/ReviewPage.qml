// qmllint disable unqualified
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."
import "components"

Item {
    id: root
    // Unsaved edits per raw_id survive list reloads (the Repeater rebuilds its rows).
    property var drafts: ({})
    readonly property string jobKey: bridge.reviewJobKey
    onJobKeyChanged: drafts = ({})

    function draftValue(rawId, field, fallback) {
        var entry = root.drafts[rawId]
        return entry !== undefined && entry[field] !== undefined ? entry[field] : fallback
    }

    function setDraft(rawId, field, value) {
        var entry = root.drafts[rawId] || {}
        entry[field] = value
        root.drafts[rawId] = entry
        bridge.noteReviewActivity()
    }

    function formatTime(seconds) {
        var total = Math.max(0, Math.floor(seconds))
        var h = Math.floor(total / 3600)
        var m = Math.floor((total % 3600) / 60)
        var s = total % 60
        return (h < 10 ? "0" : "") + h + ":" + (m < 10 ? "0" : "") + m + ":" + (s < 10 ? "0" : "") + s
    }

    readonly property bool idle: !bridge.busy && !bridge.reviewBusy

    TapHandler {
        acceptedButtons: Qt.LeftButton
        onTapped: bridge.noteReviewActivity()
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        PageHeader {
            Layout.fillWidth: true
            eyebrow: bridge.code
            title: bridge.tr("번역 검토", bridge.uiLanguage)
            description: bridge.reviewUnresolvedCount > 0
                         ? bridge.tr("미해결 ", bridge.uiLanguage) + bridge.reviewUnresolvedCount + bridge.tr("개가 남아 있어 아직 자막 파일을 만들지 않았습니다.", bridge.uiLanguage)
                         : bridge.tr("모든 항목을 처리했습니다. 아래 '자막 만들기'를 누르십시오.", bridge.uiLanguage)
            AppButton {
                text: bridge.tr("모델 번역 모두 채택 (", bridge.uiLanguage) + bridge.reviewAcceptableCount + ")"
                visible: bridge.reviewAcceptableCount > 0
                enabled: root.idle
                onClicked: bridge.acceptAllReviewItems()
            }
            AppButton {
                text: bridge.tr("남은 항목 모두 삭제", bridge.uiLanguage)
                kind: "danger"
                visible: bridge.reviewUnresolvedCount > 0
                enabled: root.idle
                onClicked: omitAllDialog.open()
            }
            AppButton {
                text: bridge.tr("보류 배치 다시 요청", bridge.uiLanguage)
                visible: bridge.reviewHasHeld
                enabled: root.idle
                onClicked: bridge.retryHeldBatches()
            }
        }

        // What the user is asked to do, in plain words.
        Rectangle {
            Layout.fillWidth: true
            Layout.bottomMargin: 8
            visible: bridge.reviewUnresolvedCount > 0
            color: Theme.paperDeep
            border.color: Theme.line
            border.width: 1
            implicitHeight: guide.implicitHeight + 24

            ColumnLayout {
                id: guide
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: 12
                spacing: 4
                Text {
                    Layout.fillWidth: true
                    text: bridge.tr("아래 줄은 모델이 번역에 확신하지 못했거나(검토 필요) 번역하지 못한(보류) 대사입니다. 줄마다 하나를 고르십시오.", bridge.uiLanguage)
                    color: Theme.ink
                    font.family: Theme.family
                    font.pixelSize: 13
                    wrapMode: Text.WordWrap
                }
                Text {
                    Layout.fillWidth: true
                    text: bridge.tr("• 채택: '모델 번역'을 그대로 자막에 씁니다.\n", bridge.uiLanguage)
                          + bridge.tr("• 수정 저장: '", bridge.uiLanguage) + bridge.targetLanguageName + bridge.tr(" 자막' 칸을 고친 뒤 저장합니다. ", bridge.uiLanguage) + bridge.sourceLanguageName + bridge.tr(" 칸은 원문을 고칠 때만 바꾸면 됩니다.\n", bridge.uiLanguage)
                          + bridge.tr("• 삭제: 말이 없는 소리·잡음·잘못 인식된 줄처럼 자막에 넣을 필요가 없을 때 뺍니다.\n", bridge.uiLanguage)
                          + bridge.tr("• 다시 번역: 모델에 한 번 더 요청합니다.", bridge.uiLanguage)
                    color: Theme.inkSoft
                    font.family: Theme.family
                    font.pixelSize: 12
                    lineHeight: 1.25
                    wrapMode: Text.WordWrap
                }
                Text {
                    Layout.fillWidth: true
                    visible: bridge.reviewAutoAcceptActive
                    text: bridge.tr("입력이 없으면 ", bridge.uiLanguage)
                          + bridge.reviewAutoAcceptSeconds
                          + bridge.tr("초 후 유효한 모델 번역을 자동 채택합니다.", bridge.uiLanguage)
                    color: Theme.ink
                    font.family: Theme.family
                    font.pixelSize: 12
                    font.weight: Font.DemiBold
                    wrapMode: Text.WordWrap
                }
            }
        }

        ScrollView {
            id: reviewScroll
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            contentWidth: availableWidth

            Column {
                width: parent.width
                topPadding: 4

                Repeater {
                    model: bridge.reviewItems
                    delegate: Item {
                        id: row
                        required property var modelData
                        readonly property bool held: modelData.kind === "held"
                        readonly property bool hasModelKo: !held && modelData.ko.length > 0
                        width: parent.width
                        height: content.implicitHeight + 24

                        Rectangle {
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.bottom: parent.bottom
                            height: 1
                            color: Theme.line
                        }

                        ColumnLayout {
                            id: content
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.topMargin: 12
                            spacing: 6
                            opacity: row.modelData.resolved ? 0.6 : 1.0

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 12
                                Text {
                                    text: root.formatTime(row.modelData.start) + " – " + root.formatTime(row.modelData.end)
                                    color: Theme.ink
                                    font.family: Theme.family
                                    font.pixelSize: 14
                                    font.weight: Font.DemiBold
                                }
                                Text {
                                    text: "#" + row.modelData.raw_id
                                    color: Theme.inkFaint
                                    font.family: Theme.family
                                    font.pixelSize: 12
                                }
                                Text {
                                    text: row.modelData.resolved
                                          ? "✓ " + row.modelData.resolution_label
                                          : (row.held
                                             ? (row.modelData.held_kind ? bridge.tr("보류 · ", bridge.uiLanguage) + row.modelData.held_kind : bridge.tr("보류", bridge.uiLanguage))
                                             : bridge.tr("검토 필요", bridge.uiLanguage))
                                    color: row.modelData.resolved ? Theme.inkSoft : Theme.danger
                                    font.family: Theme.family
                                    font.pixelSize: 12
                                    font.weight: Font.DemiBold
                                }
                                Item { Layout.fillWidth: true }
                                AppButton {
                                    text: bridge.tr("채택", bridge.uiLanguage)
                                    visible: row.hasModelKo && !row.modelData.resolved
                                    enabled: root.idle
                                    onClicked: bridge.acceptReviewItem(row.modelData.raw_id)
                                }
                                AppButton {
                                    text: bridge.tr("삭제", bridge.uiLanguage)
                                    kind: "danger"
                                    enabled: root.idle
                                    onClicked: bridge.omitReviewItem(row.modelData.raw_id)
                                }
                                AppButton {
                                    text: bridge.reviewBusy ? bridge.tr("처리 중", bridge.uiLanguage) : bridge.tr("다시 번역", bridge.uiLanguage)
                                    kind: "quiet"
                                    enabled: root.idle
                                    onClicked: bridge.retryReviewItem(row.modelData.raw_id)
                                }
                            }
                            // Label column (small, faint) and value column, so a label never
                            // reads as part of the sentence.
                            GridLayout {
                                Layout.fillWidth: true
                                columns: 2
                                columnSpacing: 12
                                rowSpacing: 6
                                Text {
                                    Layout.preferredWidth: 64
                                    Layout.alignment: Qt.AlignTop
                                    topPadding: 2
                                    text: bridge.tr("원문", bridge.uiLanguage)
                                    color: Theme.inkFaint
                                    font.family: Theme.family
                                    font.pixelSize: 12
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: row.modelData.raw_ja
                                    color: Theme.inkSoft
                                    font.family: Theme.family
                                    font.pixelSize: 13
                                    wrapMode: Text.Wrap
                                }
                                Text {
                                    Layout.preferredWidth: 64
                                    Layout.alignment: Qt.AlignVCenter
                                    text: bridge.tr("모델 번역", bridge.uiLanguage)
                                    color: Theme.inkFaint
                                    font.family: Theme.family
                                    font.pixelSize: 12
                                }
                                Rectangle {
                                    Layout.fillWidth: true
                                    implicitHeight: modelKo.implicitHeight + 14
                                    color: row.hasModelKo ? Theme.selection : "transparent"
                                    border.color: row.hasModelKo ? "transparent" : Theme.line
                                    border.width: 1
                                    radius: 4
                                    Text {
                                        id: modelKo
                                        anchors.left: parent.left
                                        anchors.right: parent.right
                                        anchors.verticalCenter: parent.verticalCenter
                                        anchors.leftMargin: 10
                                        anchors.rightMargin: 10
                                        text: row.hasModelKo
                                              ? row.modelData.ko
                                              : (row.held ? bridge.tr("없음 — 직접 입력하거나 삭제하거나 다시 요청하십시오.", bridge.uiLanguage) : bridge.tr("없음", bridge.uiLanguage))
                                        color: row.hasModelKo ? Theme.ink : Theme.inkFaint
                                        font.family: Theme.family
                                        font.pixelSize: 15
                                        font.weight: row.hasModelKo ? Font.DemiBold : Font.Normal
                                        wrapMode: Text.Wrap
                                    }
                                }
                            }
                            Text {
                                Layout.fillWidth: true
                                Layout.leftMargin: 76
                                visible: row.modelData.reason.length > 0
                                text: bridge.tr("모델이 남긴 사유: ", bridge.uiLanguage) + row.modelData.reason
                                color: Theme.inkFaint
                                font.family: Theme.family
                                font.pixelSize: 12
                                wrapMode: Text.Wrap
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 12
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    Layout.preferredWidth: 2
                                    spacing: 2
                                    Text {
                                        text: bridge.targetLanguageName + bridge.tr(" 자막 (고칠 때만)", bridge.uiLanguage)
                                        color: Theme.inkFaint
                                        font.family: Theme.family
                                        font.pixelSize: 11
                                    }
                                    LineField {
                                        id: koField
                                        Layout.fillWidth: true
                                        placeholderText: bridge.tr("자막에 들어갈 ", bridge.uiLanguage) + bridge.targetLanguageName + bridge.tr(" 문장", bridge.uiLanguage)
                                        text: root.draftValue(row.modelData.raw_id, "ko", row.modelData.ko)
                                        enabled: !bridge.busy
                                        onActiveFocusChanged: {
                                            if (activeFocus)
                                                bridge.noteReviewActivity()
                                        }
                                        onTextEdited: root.setDraft(row.modelData.raw_id, "ko", text)
                                    }
                                }
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    Layout.preferredWidth: 1
                                    spacing: 2
                                    Text {
                                        text: bridge.sourceLanguageName + bridge.tr(" 원문 보정 (선택)", bridge.uiLanguage)
                                        color: Theme.inkFaint
                                        font.family: Theme.family
                                        font.pixelSize: 11
                                    }
                                    LineField {
                                        id: jaField
                                        Layout.fillWidth: true
                                        placeholderText: bridge.tr("비워 두면 원문 그대로", bridge.uiLanguage)
                                        text: root.draftValue(row.modelData.raw_id, "ja", row.modelData.ja_fixed)
                                        enabled: !bridge.busy
                                        onActiveFocusChanged: {
                                            if (activeFocus)
                                                bridge.noteReviewActivity()
                                        }
                                        onTextEdited: root.setDraft(row.modelData.raw_id, "ja", text)
                                    }
                                }
                                AppButton {
                                    Layout.alignment: Qt.AlignBottom
                                    text: bridge.tr("수정 저장", bridge.uiLanguage)
                                    enabled: root.idle && koField.text.length > 0
                                    onClicked: {
                                        var rawId = row.modelData.raw_id
                                        if (bridge.saveManualTranslation(rawId, jaField.text, koField.text))
                                            delete root.drafts[rawId]
                                    }
                                }
                            }
                        }
                    }
                }

                Text {
                    visible: bridge.reviewItems.length === 0
                    text: bridge.tr("검토할 항목이 없습니다.", bridge.uiLanguage)
                    color: Theme.inkFaint
                    font.family: Theme.family
                    font.pixelSize: 13
                    topPadding: 18
                }
            }
        }

        Connections {
            target: reviewScroll.contentItem
            ignoreUnknownSignals: true
            function onContentYChanged() {
                bridge.noteReviewActivity()
            }
        }

        FooterBar {
            Layout.fillWidth: true
            title: bridge.reviewUnresolvedCount > 0
                   ? bridge.tr("남은 ", bridge.uiLanguage) + bridge.reviewUnresolvedCount + bridge.tr("개를 처리하면 자막을 만들 수 있습니다", bridge.uiLanguage)
                   : bridge.tr("모든 항목을 처리했습니다", bridge.uiLanguage)
            detail: bridge.tr("처리한 내용은 바로 저장됩니다. 자막 만들기는 저장된 번역으로 검증과 출력만 다시 실행합니다.", bridge.uiLanguage)
            AppButton {
                text: bridge.tr("목록 새로고침", bridge.uiLanguage)
                kind: "quiet"
                enabled: root.idle
                onClicked: bridge.loadReviewItems()
            }
            AppButton {
                text: bridge.tr("자막 만들기", bridge.uiLanguage)
                kind: "primary"
                enabled: root.idle && bridge.reviewUnresolvedCount === 0
                onClicked: bridge.finishReview()
            }
        }
    }

    Dialog {
        id: omitAllDialog
        anchors.centerIn: parent
        width: Math.min(520, root.width - 48)
        modal: true
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
                text: bridge.tr("남은 ", bridge.uiLanguage) + bridge.reviewUnresolvedCount + bridge.tr("개를 자막에서 삭제할까요?", bridge.uiLanguage)
                color: Theme.ink
                font.family: Theme.family
                font.pixelSize: 18
                font.weight: Font.DemiBold
                wrapMode: Text.WordWrap
            }
            Text {
                Layout.fillWidth: true
                text: bridge.tr("아직 처리하지 않은 줄이 모두 자막에서 빠집니다. 실제 대사가 섞여 있으면 그 대사도 빠지니, 먼저 '모델 번역 모두 채택'을 고려하십시오. 자막 만들기 전까지는 삭제한 줄에 직접 입력해 되살릴 수 있습니다.", bridge.uiLanguage)
                color: Theme.inkSoft
                font.family: Theme.family
                font.pixelSize: 13
                wrapMode: Text.WordWrap
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                AppButton {
                    text: bridge.tr("취소", bridge.uiLanguage)
                    kind: "quiet"
                    onClicked: omitAllDialog.close()
                }
                AppButton {
                    text: bridge.tr("모두 삭제", bridge.uiLanguage)
                    kind: "danger"
                    onClicked: {
                        omitAllDialog.close()
                        bridge.omitAllUnresolvedReviewItems()
                    }
                }
            }
        }
    }
}
