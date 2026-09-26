pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: finder
    required property var replay
    readonly property var replayState: replay.state
    readonly property var report: replayState.catalog || ({})
    readonly property var categories: ["failures", "followups", "unfinished", "all"]
    spacing: Theme.spaceMd
    onVisibleChanged: {
        if (visible) {
            query.text = replayState.catalog_query || "";
            category.currentIndex = Math.max(0, categories.indexOf(replayState.catalog_category));
        }
    }
    RowLayout {
        Layout.fillWidth: true
        NativeField {
            id: query
            objectName: "stopFinderSearch"
            Layout.fillWidth: true
            placeholderText: "Session title or project"
            maximumLength: 1000
            onAccepted: finder.replay.searchSessions(text, finder.categories[category.currentIndex], 0)
        }
        NativeButton {
            text: "Search"
            enabled: !finder.replayState.loading
            onClicked: finder.replay.searchSessions(query.text, finder.categories[category.currentIndex], 0)
        }
    }
    NativeCombo {
        id: category
        objectName: "stopFinderCategory"
        Layout.fillWidth: true
        model: ["Failed, interrupted or blocked turns", "User requested continuation after a stop", "Unfinished-work language · heuristic", "All recorded turn endings"]
        onActivated: finder.replay.searchSessions(query.text, finder.categories[currentIndex], 0)
    }
    Label {
        Layout.fillWidth: true
        text: "Historical evidence, not a task-completion score. Continuation wording and unfinished-work language are search heuristics; pauses and user stops are not failures."
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        font.pixelSize: Theme.caption
        color: Theme.secondaryText
    }
    Label {
        objectName: "stopFinderScope"
        Layout.fillWidth: true
        text: finder.replayState.loading ? "Reading session logs on this backend… No models or tools are executed."
              : (finder.report.scanned || 0) + " sessions scanned · " + (finder.report.total || 0) + " matches · Includes archived sessions, excludes hidden projects."
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        font.pixelSize: Theme.caption
        color: Theme.secondaryText
    }
    Label {
        objectName: "stopFinderErrors"
        Layout.fillWidth: true
        visible: !!finder.report.error_count
        text: (finder.report.error_count || 0) + " logs could not be read. Results are incomplete."
              + ((finder.report.errors || []).length ? "\nFirst: " + finder.report.errors[0].title + " — " + finder.report.errors[0].error : "")
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        font.pixelSize: Theme.caption
        color: Theme.danger
    }
    ListView {
        id: sessions
        objectName: "stopFinderList"
        Layout.fillWidth: true
        Layout.fillHeight: true
        clip: true
        reuseItems: true
        spacing: 4
        model: finder.report.rows || []
        ScrollBar.vertical: ScrollBar {}
        delegate: ItemDelegate {
            id: row
            required property var modelData
            objectName: "stopFinderSession_" + modelData.chat_id
            width: sessions.width
            height: 112
            enabled: !finder.replayState.loading
            onClicked: finder.replay.openStop(modelData.chat_id, modelData.match.seq, modelData.through)
            contentItem: ColumnLayout {
                spacing: 5
                Label {
                    Layout.fillWidth: true
                    text: row.modelData.title + (row.modelData.archived ? " · Archived" : "")
                    textFormat: Text.PlainText
                    font.pixelSize: Theme.body
                    font.weight: Font.DemiBold
                    elide: Text.ElideRight
                }
                Label {
                    Layout.fillWidth: true
                    text: row.modelData.project + " · Current state: " + row.modelData.status
                    textFormat: Text.PlainText
                    font.pixelSize: Theme.caption
                    color: Theme.secondaryText
                    elide: Text.ElideRight
                }
                Label {
                    Layout.fillWidth: true
                    text: row.modelData.summary.failures + " failed/interrupted/blocked turns · " + row.modelData.summary.followups + " continuation requests · " + row.modelData.summary.unfinished_hints + " unfinished-work hints"
                    textFormat: Text.PlainText
                    font.pixelSize: Theme.caption
                    color: Theme.secondaryText
                    elide: Text.ElideRight
                }
                Label {
                    Layout.fillWidth: true
                    text: "Latest matching stop: #" + row.modelData.match.seq + " · " + row.modelData.match.label + " →"
                    textFormat: Text.PlainText
                    font.pixelSize: Theme.caption
                    color: Theme.accent
                    elide: Text.ElideRight
                }
            }
            background: Rectangle {
                radius: Theme.controlRadius
                color: row.hovered ? Theme.hover : "transparent"
                border.width: row.visualFocus ? 1 : 0
                border.color: Theme.accent
            }
        }
        Label {
            anchors.centerIn: parent
            width: parent.width - 32
            visible: sessions.count === 0
            text: finder.replayState.loading ? "Scanning…" : finder.replayState.error ? "No results loaded." : "No matching recorded stops. This does not prove every task was completed."
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            horizontalAlignment: Text.AlignHCenter
            color: Theme.secondaryText
        }
    }
    RowLayout {
        Layout.fillWidth: true
        NativeButton {
            text: "Previous"
            enabled: !finder.replayState.loading && (finder.report.offset || 0) > 0
            onClicked: finder.replay.searchSessions(query.text, finder.categories[category.currentIndex], Math.max(0, finder.report.offset - 100))
        }
        Item { Layout.fillWidth: true }
        Label {
            text: "Each search reads a new snapshot. Open a result to freeze its evidence."
            font.pixelSize: Theme.captionSmall
            color: Theme.secondaryText
        }
        Item { Layout.fillWidth: true }
        NativeButton {
            objectName: "stopFinderNextPage"
            text: "Next"
            enabled: !finder.replayState.loading && finder.report.next_offset !== undefined && finder.report.next_offset !== null
            onClicked: finder.replay.searchSessions(query.text, finder.categories[category.currentIndex], finder.report.next_offset)
        }
    }
}
