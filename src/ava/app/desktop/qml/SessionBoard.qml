pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Pane {
    id: board
    objectName: "sessionBoard"
    required property var backend
    required property bool sidebarVisible
    readonly property var summaries: backend.board
    readonly property bool narrow: width < 800
    property int selectedColumn: 1
    signal openChat(string identity)
    signal closeRequested
    signal sidebarRequested
    padding: narrow ? Theme.spaceLg : Theme.spaceXl
    background: Rectangle { color: board.palette.window }

    Shortcut {
        sequences: [StandardKey.Find]
        enabled: board.visible
        onActivated: {
            search.forceActiveFocus();
            search.selectAll();
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 18
        PageHeader {
            Layout.fillWidth: true
            title: "Session board"
            description: "Keep track of work across your projects."
            sidebarVisible: board.sidebarVisible
            onSidebarRequested: board.sidebarRequested()
            NativeButton {
                objectName: "closeSessionBoardButton"
                text: "Back to chat"
                onClicked: board.closeRequested()
            }
        }
        GridLayout {
            Layout.fillWidth: true
            columns: board.narrow ? 2 : 4
            columnSpacing: 10
            rowSpacing: 10
            NativeField {
                id: search
                objectName: "boardSearch"
                Layout.fillWidth: true
                Layout.minimumWidth: 100
                Layout.columnSpan: board.narrow ? 2 : 1
                placeholderText: "Search sessions (" + (Qt.platform.os === "osx" ? "⌘ F" : "Ctrl F") + ")"
                Component.onCompleted: text = board.summaries.filters.search
                onTextEdited: searchDelay.restart()
                Timer { id: searchDelay; interval: 120; onTriggered: board.summaries.filter("search", search.text) }
            }
            NativeCombo {
                id: machines
                objectName: "boardMachineFilter"
                Layout.fillWidth: true
                Layout.minimumWidth: 100
                model: board.summaries.machineOptions
                currentIndex: model.findIndex(option => option.id === board.summaries.filters.machine)
                textRole: "name"
                valueRole: "id"
                onActivated: board.summaries.filter("machine", currentValue)
            }
            NativeCombo {
                id: projects
                objectName: "boardProjectFilter"
                Layout.fillWidth: true
                Layout.minimumWidth: 100
                model: board.summaries.projectOptions
                currentIndex: model.findIndex(option => option.id === board.summaries.filters.project)
                textRole: "name"
                valueRole: "id"
                onActivated: board.summaries.filter("project", currentValue)
            }
            NativeCombo {
                objectName: "boardOutcomeFilter"
                Layout.fillWidth: true
                Layout.columnSpan: board.narrow ? 2 : 1
                model: ["All outcomes", "Needs attention"]
                currentIndex: board.summaries.filters.outcome === "attention" ? 1 : 0
                onActivated: board.summaries.filter("outcome", currentIndex ? "attention" : "")
            }
        }
        Label {
            Layout.fillWidth: true
            visible: !!board.summaries.error || !!board.summaries.notice
            text: board.summaries.error || board.summaries.notice
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
            color: palette.placeholderText
            font.pixelSize: 12
        }
        RowLayout {
            visible: board.narrow
            Layout.fillWidth: true
            Repeater {
                model: ["In progress", "Needs review", "Reviewed"]
                NativeButton {
                    required property int index
                    required property string modelData
                    objectName: "boardColumnTab_" + index
                    Layout.fillWidth: true
                    text: modelData + " · " + board.summaries.totals[index]
                    primary: board.selectedColumn === index
                    onClicked: board.selectedColumn = index
                }
            }
        }
        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 14
            Repeater {
                model: [
                    {name: "In progress", key: "active", entries: board.summaries.activeSessions, color: Theme.success, empty: "No work in progress", note: "Running and paused sessions appear here."},
                    {name: "Needs review", key: "review", entries: board.summaries.needsReview, color: Theme.warning, empty: "You're all caught up", note: "Results you have not seen in a conversation wait here."},
                    {name: "Reviewed", key: "reviewed", entries: board.summaries.reviewedSessions, color: Theme.secondaryText, empty: "No reviewed results", note: "Opening a result here marks it reviewed; you can also use Mark reviewed."}
                ]
                Rectangle {
                    id: column
                    required property int index
                    required property var modelData
                    visible: !board.narrow || board.selectedColumn === index
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    radius: Theme.cardRadius
                    color: Theme.inset
                    border.color: board.palette.mid
                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: 12
                        spacing: 12
                        RowLayout {
                            Layout.fillWidth: true
                            Rectangle { implicitWidth: 7; implicitHeight: 7; radius: 4; color: column.modelData.color }
                            Label { Layout.fillWidth: true; text: column.modelData.name; font.pixelSize: 13; font.weight: Font.DemiBold }
                            Label { text: board.summaries.totals[column.index]; color: palette.placeholderText; font.pixelSize: 12 }
                        }
                        ListView {
                            id: sessions
                            objectName: "boardList_" + column.modelData.key
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            clip: true
                            reuseItems: true
                            cacheBuffer: 120
                            spacing: 10
                            model: column.visible ? column.modelData.entries : null
                            ScrollBar.vertical: ScrollBar {}
                            delegate: Pane {
                                id: card
                                required property var entry
                                objectName: "boardCard_" + entry.id
                                width: sessions.width
                                implicitHeight: contents.implicitHeight + 24
                                padding: 12
                                background: Surface {}
                                ColumnLayout {
                                    id: contents
                                    width: parent.width
                                    spacing: 8
                                    Label {
                                        Layout.fillWidth: true
                                        text: card.entry.title || "Untitled session"
                                        textFormat: Text.PlainText
                                        wrapMode: Text.WordWrap
                                        maximumLineCount: 2
                                        elide: Text.ElideRight
                                        font.pixelSize: 14
                                        font.weight: Font.Medium
                                    }
                                    Label {
                                        Layout.fillWidth: true
                                        text: card.entry.project + " · " + card.entry.machine
                                        textFormat: Text.PlainText
                                        elide: Text.ElideMiddle
                                        font.pixelSize: 11
                                        color: palette.placeholderText
                                    }
                                    Label {
                                        Layout.fillWidth: true
                                        text: card.entry.online ? card.entry.label : "Offline · last known: " + card.entry.label
                                        color: card.entry.attention ? Theme.warning : palette.placeholderText
                                        font.pixelSize: 11
                                        wrapMode: Text.WordWrap
                                    }
                                    Label {
                                        Layout.fillWidth: true
                                        visible: !!card.entry.time
                                        text: card.entry.time ? Qt.formatDateTime(new Date(card.entry.time), "MMM d, hh:mm") : ""
                                        color: palette.placeholderText
                                        font.pixelSize: 11
                                    }
                                    RowLayout {
                                        Layout.fillWidth: true
                                        NativeButton {
                                            objectName: "openBoardChat_" + column.modelData.key + "_" + card.entry.id
                                            text: card.entry.label === "Paused" ? "Open to resume" : "Open"
                                            enabled: card.entry.online
                                            implicitHeight: 30
                                            onClicked: board.openChat(card.entry.id)
                                        }
                                        Item { Layout.fillWidth: true }
                                        NativeButton {
                                            objectName: "reviewBoardChat_" + card.entry.id
                                            visible: column.modelData.key === "review"
                                            text: card.entry.saving ? "Saving…" : "Mark reviewed"
                                            enabled: card.entry.online && !card.entry.saving
                                            implicitHeight: 30
                                            onClicked: board.summaries.review(card.entry.id, card.entry.completion_seq)
                                        }
                                    }
                                }
                            }
                            footer: Item {
                                width: sessions.width
                                height: more.visible ? 46 : 0
                                NativeButton {
                                    id: more
                                    objectName: "boardMore_" + column.modelData.key
                                    anchors.centerIn: parent
                                    visible: column.modelData.key !== "active" && sessions.count < board.summaries.totals[column.index]
                                    text: "Review more · " + Math.max(0, board.summaries.totals[column.index] - sessions.count)
                                    onClicked: board.summaries.more(column.modelData.key)
                                }
                            }
                            EmptyState {
                                anchors.centerIn: parent
                                width: parent.width - 24
                                visible: board.summaries.totals[column.index] === 0
                                title: column.modelData.empty
                                description: column.modelData.note
                            }
                        }
                    }
                }
            }
        }
    }
}
