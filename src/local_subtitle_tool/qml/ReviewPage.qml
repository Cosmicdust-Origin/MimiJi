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
    }

    function formatTime(seconds) {
        var total = Math.max(0, Math.floor(seconds))
        var h = Math.floor(total / 3600)
        var m = Math.floor((total % 3600) / 60)
        var s = total % 60
        return (h < 10 ? "0" : "") + h + ":" + (m < 10 ? "0" : "") + m + ":" + (s < 10 ? "0" : "") + s
    }

    readonly property bool idle: !bridge.busy && !bridge.reviewBusy

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        PageHeader {
            Layout.fillWidth: true
            eyebrow: bridge.code
            title: "번역 검토"
            description: bridge.reviewUnresolvedCount > 0
                         ? "미해결 " + bridge.reviewUnresolvedCount + "개가 남아 있어 아직 자막 파일을 만들지 않았습니다."
                         : "모든 항목을 처리했습니다. 아래 '자막 만들기'를 누르십시오."
            AppButton {
                text: "모델 번역 모두 채택 (" + bridge.reviewAcceptableCount + ")"
                visible: bridge.reviewAcceptableCount > 0
                enabled: root.idle
                onClicked: bridge.acceptAllReviewItems()
            }
            AppButton {
                text: "남은 항목 모두 삭제"
                kind: "danger"
                visible: bridge.reviewUnresolvedCount > 0
                enabled: root.idle
                onClicked: omitAllDialog.open()
            }
            AppButton {
                text: "보류 배치 다시 요청"
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
                    text: "아래 줄은 모델이 번역에 확신하지 못했거나(검토 필요) 번역하지 못한(보류) 대사입니다. 줄마다 하나를 고르십시오."
                    color: Theme.ink
                    font.family: Theme.family
                    font.pixelSize: 13
                    wrapMode: Text.WordWrap
                }
                Text {
                    Layout.fillWidth: true
                    text: "• 채택: '모델 번역'을 그대로 자막에 씁니다.\n"
                          + "• 수정 저장: '한국어 자막' 칸을 고친 뒤 저장합니다. 일본어 칸은 원문을 고칠 때만 바꾸면 됩니다.\n"
                          + "• 삭제: 말이 없는 소리·잡음·잘못 인식된 줄처럼 자막에 넣을 필요가 없을 때 뺍니다.\n"
                          + "• 다시 번역: 모델에 한 번 더 요청합니다."
                    color: Theme.inkSoft
                    font.family: Theme.family
                    font.pixelSize: 12
                    lineHeight: 1.25
                    wrapMode: Text.WordWrap
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
                                             ? (row.modelData.held_kind ? "보류 · " + row.modelData.held_kind : "보류")
                                             : "검토 필요")
                                    color: row.modelData.resolved ? Theme.inkSoft : Theme.danger
                                    font.family: Theme.family
                                    font.pixelSize: 12
                                    font.weight: Font.DemiBold
                                }
                                Item { Layout.fillWidth: true }
                                AppButton {
                                    text: "채택"
                                    visible: row.hasModelKo && !row.modelData.resolved
                                    enabled: root.idle
                                    onClicked: bridge.acceptReviewItem(row.modelData.raw_id)
                                }
                                AppButton {
                                    text: "삭제"
                                    kind: "danger"
                                    enabled: root.idle
                                    onClicked: bridge.omitReviewItem(row.modelData.raw_id)
                                }
                                AppButton {
                                    text: bridge.reviewBusy ? "처리 중" : "다시 번역"
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
                                    text: "원문"
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
                                    text: "모델 번역"
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
                                              : (row.held ? "없음 — 직접 입력하거나 삭제하거나 다시 요청하십시오." : "없음")
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
                                text: "모델이 남긴 사유: " + row.modelData.reason
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
                                        text: "한국어 자막 (고칠 때만)"
                                        color: Theme.inkFaint
                                        font.family: Theme.family
                                        font.pixelSize: 11
                                    }
                                    LineField {
                                        id: koField
                                        Layout.fillWidth: true
                                        placeholderText: "자막에 들어갈 한국어 문장"
                                        text: root.draftValue(row.modelData.raw_id, "ko", row.modelData.ko)
                                        enabled: !bridge.busy
                                        onTextEdited: root.setDraft(row.modelData.raw_id, "ko", text)
                                    }
                                }
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    Layout.preferredWidth: 1
                                    spacing: 2
                                    Text {
                                        text: "일본어 원문 보정 (선택)"
                                        color: Theme.inkFaint
                                        font.family: Theme.family
                                        font.pixelSize: 11
                                    }
                                    LineField {
                                        id: jaField
                                        Layout.fillWidth: true
                                        placeholderText: "비워 두면 원문 그대로"
                                        text: root.draftValue(row.modelData.raw_id, "ja", row.modelData.ja_fixed)
                                        enabled: !bridge.busy
                                        onTextEdited: root.setDraft(row.modelData.raw_id, "ja", text)
                                    }
                                }
                                AppButton {
                                    Layout.alignment: Qt.AlignBottom
                                    text: "수정 저장"
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
                    text: "검토할 항목이 없습니다."
                    color: Theme.inkFaint
                    font.family: Theme.family
                    font.pixelSize: 13
                    topPadding: 18
                }
            }
        }

        FooterBar {
            Layout.fillWidth: true
            title: bridge.reviewUnresolvedCount > 0
                   ? "남은 " + bridge.reviewUnresolvedCount + "개를 처리하면 자막을 만들 수 있습니다"
                   : "모든 항목을 처리했습니다"
            detail: "처리한 내용은 바로 저장됩니다. 자막 만들기는 저장된 번역으로 검증과 출력만 다시 실행합니다."
            AppButton {
                text: "목록 새로고침"
                kind: "quiet"
                enabled: root.idle
                onClicked: bridge.loadReviewItems()
            }
            AppButton {
                text: "자막 만들기"
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
                text: "남은 " + bridge.reviewUnresolvedCount + "개를 자막에서 삭제할까요?"
                color: Theme.ink
                font.family: Theme.family
                font.pixelSize: 18
                font.weight: Font.DemiBold
                wrapMode: Text.WordWrap
            }
            Text {
                Layout.fillWidth: true
                text: "아직 처리하지 않은 줄이 모두 자막에서 빠집니다. 실제 대사가 섞여 있으면 그 대사도 빠지니, 먼저 '모델 번역 모두 채택'을 고려하십시오. 자막 만들기 전까지는 삭제한 줄에 직접 입력해 되살릴 수 있습니다."
                color: Theme.inkSoft
                font.family: Theme.family
                font.pixelSize: 13
                wrapMode: Text.WordWrap
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                AppButton {
                    text: "취소"
                    kind: "quiet"
                    onClicked: omitAllDialog.close()
                }
                AppButton {
                    text: "모두 삭제"
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
