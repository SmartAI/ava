pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    id: panel
    objectName: "sessionReplayWindow"
    required property var backend
    required property string codeFont
    required property var appPalette
    signal openMain()
    readonly property var replay: backend.replay
    readonly property var state: replay.state
    readonly property var snapshot: state.data || ({})
    readonly property var detail: state.detail || ({})
    property var collapsed: ({})
    readonly property bool finding: state.mode === "catalog"
    readonly property var categories: ["all", "issues", "model", "tool", "input", "state", "raw", "stops", "failures", "followups", "unfinished"]
    palette: appPalette
    color: Theme.workspace
    title: finding ? "Find stopped sessions · " + state.scope : "Session Replay · " + (snapshot.title || "Ava")
    width: 1120
    height: 760
    minimumWidth: 800
    minimumHeight: 600
    visible: false
    onClosing: replay.close()
    function showReplay() {
        collapsed = ({});
        search.text = "";
        filters.currentIndex = Math.max(0, categories.indexOf(state.category));
        show();
        raise();
        requestActivate();
    }
    function showCategory(name) {
        search.text = "";
        collapsed = ({});
        filters.currentIndex = categories.indexOf(name);
        replay.filter("", name);
    }
    function timelineRows() {
        const rows = [];
        let turn = -1;
        for (const row of snapshot.rows || []) {
            if (row.turn !== turn) {
                turn = row.turn;
                rows.push({header: true, turn: turn, seq: -turn - 2});
            }
            if (!collapsed[turn]) rows.push(row);
        }
        return rows;
    }
    Connections {
        target: panel.replay
        function onOpened() { panel.showReplay(); }
    }
    Shortcut { sequences: [StandardKey.Close]; onActivated: panel.close() }
    ColumnLayout {
        anchors.fill: parent
        anchors.margins: Theme.spaceXl
        spacing: Theme.spaceMd
        RowLayout {
            Layout.fillWidth: true
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 4
                Label {
                    textFormat: Text.PlainText
                    Layout.fillWidth: true
                    text: panel.finding ? "Find stopped sessions" : "Session Replay"
                    font.pixelSize: Theme.pageTitle
                    font.weight: Font.DemiBold
                }
                Label {
                    textFormat: Text.PlainText
                    Layout.fillWidth: true
                    objectName: "replaySessionTitle"
                    text: panel.finding ? panel.state.scope : panel.snapshot.title || (panel.state.loading ? "Loading session…" : panel.state.error ? "Session replay unavailable" : "No session loaded")
                    font.pixelSize: Theme.body
                    color: Theme.secondaryText
                    elide: Text.ElideRight
                }
            }
            NativeButton {
                objectName: "replayOpenConversation"
                visible: !panel.finding
                text: "Open conversation"
                onClicked: {
                    panel.backend.openChat(panel.state.identity);
                    panel.openMain();
                }
            }
            NativeButton {
                objectName: "replayFindSessions"
                visible: !panel.finding
                text: "Find sessions"
                onClicked: panel.replay.findSessions()
            }
            NativeButton {
                objectName: "replayRefresh"
                text: !panel.finding && panel.state.has_new ? "New records · Refresh" : "Refresh"
                enabled: !panel.state.loading
                onClicked: panel.replay.refresh()
            }
        }
        RowLayout {
            Layout.fillWidth: true
            visible: !panel.finding
            Label {
                textFormat: Text.PlainText
                objectName: "replaySnapshot"
                Layout.fillWidth: true
                text: panel.snapshot.through !== undefined ? "Read-only snapshot through #" + panel.snapshot.through + " · " + panel.snapshot.event_count + " events" : "Read-only · no tools or requests are executed"
                color: Theme.secondaryText
                font.pixelSize: Theme.caption
                elide: Text.ElideRight
            }
            NativeButton {
                objectName: "replayPreviousIssue"
                text: "Previous"
                tip: "Previous observation"
                enabled: !!panel.snapshot.issue_events
                onClicked: panel.replay.jumpIssue(-1)
            }
            NativeButton {
                objectName: "replayNextIssue"
                text: "Next"
                tip: "Next observation"
                enabled: !!panel.snapshot.issue_events
                onClicked: panel.replay.jumpIssue(1)
            }
            NativeButton {
                objectName: "replayIssues"
                text: (panel.snapshot.issue_events || 0) + " observations"
                enabled: !!panel.snapshot.issue_events && !panel.state.loading
                onClicked: {
                    panel.showCategory("issues");
                }
            }
        }
        Label {
            textFormat: Text.PlainText
            objectName: "replayError"
            Layout.fillWidth: true
            visible: !!panel.state.error
            text: panel.state.error || ""
            color: Theme.danger
            wrapMode: Text.Wrap
            font.pixelSize: Theme.body
        }
        StopFinder {
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: panel.finding
            replay: panel.replay
        }
        SplitView {
            visible: !panel.finding
            Layout.fillWidth: true
            Layout.fillHeight: true
            orientation: Qt.Horizontal
            handle: Rectangle {
                implicitWidth: 5
                color: SplitHandle.hovered || SplitHandle.pressed ? Theme.accent : Theme.border
            }
            ColumnLayout {
                SplitView.preferredWidth: 320
                SplitView.minimumWidth: 260
                spacing: Theme.spaceSm
                RowLayout {
                    Layout.fillWidth: true
                    NativeField {
                        id: search
                        objectName: "replaySearch"
                        Layout.fillWidth: true
                        placeholderText: "Search recorded content"
                        maximumLength: 1000
                        onAccepted: panel.replay.filter(text, panel.categories[filters.currentIndex])
                    }
                    NativeButton {
                        text: "Search"
                        enabled: !panel.state.loading
                        onClicked: panel.replay.filter(search.text, panel.categories[filters.currentIndex])
                    }
                }
                NativeCombo {
                    id: filters
                    objectName: "replayFilter"
                    Layout.fillWidth: true
                    model: ["Execution trace", "Observations · not task verdicts", "Model requests & responses", "Tool results & verification", "User inputs & queue", "State changes", "Raw events · includes streaming", "Why stopped · all turn endings", "Failed, interrupted or blocked turns", "User requested continuation", "Unfinished-work language · heuristic"]
                    onActivated: panel.replay.filter(search.text, panel.categories[currentIndex])
                }
                NativeButton {
                    objectName: "replayOverview"
                    Layout.fillWidth: true
                    text: "Session overview"
                    selected: panel.state.selected < 0
                    onClicked: panel.replay.select(-1)
                }
                RowLayout {
                    Layout.fillWidth: true
                    NativeButton {
                        objectName: "replayStops"
                        Layout.fillWidth: true
                        text: "Why stopped"
                        enabled: !!panel.snapshot.stops && panel.snapshot.stops.total > 0
                        onClicked: panel.showCategory("stops")
                    }
                    NativeButton {
                        objectName: "replayFollowups"
                        Layout.fillWidth: true
                        text: "Continuation requests"
                        enabled: !!panel.snapshot.stops && panel.snapshot.stops.followups > 0
                        onClicked: panel.showCategory("followups")
                    }
                }
                ListView {
                    id: timeline
                    objectName: "replayTimeline"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    reuseItems: true
                    spacing: 3
                    model: panel.timelineRows()
                    ScrollBar.vertical: ScrollBar {}
                    delegate: ItemDelegate {
                        id: row
                        required property var modelData
                        width: timeline.width
                        height: modelData.header ? 32 : 64
                        objectName: modelData.header ? "replayTurn_" + modelData.turn : "replayEvent_" + modelData.seq
                        highlighted: !modelData.header && panel.state.selected === modelData.seq
                        onClicked: {
                            if (modelData.header) {
                                const next = Object.assign({}, panel.collapsed);
                                next[modelData.turn] = !next[modelData.turn];
                                panel.collapsed = next;
                            } else panel.replay.select(modelData.seq);
                        }
                        background: Rectangle {
                            radius: Theme.controlRadius
                            color: row.highlighted ? Theme.selection : row.hovered ? Theme.hover : "transparent"
                            border.width: row.visualFocus ? 1 : 0
                            border.color: Theme.accent
                        }
                        contentItem: ColumnLayout {
                            spacing: 3
                            Label {
                                textFormat: Text.PlainText
                                Layout.fillWidth: true
                                text: row.modelData.header ? (panel.collapsed[row.modelData.turn] ? "▸ " : "▾ ") + (row.modelData.turn ? "Turn " + row.modelData.turn : "Session setup") : row.modelData.title
                                font.pixelSize: Theme.body
                                font.weight: row.modelData.header ? Font.DemiBold : Font.Normal
                                elide: Text.ElideRight
                            }
                            Label {
                                textFormat: Text.PlainText
                                Layout.fillWidth: true
                                visible: !row.modelData.header
                                text: "#" + row.modelData.seq + (row.modelData.step ? " · Step " + row.modelData.step : "") + ((row.modelData.observations || []).some(n => n.level === "error") ? " · Error" : (row.modelData.observations || []).length ? " · Observation" : "") + (row.modelData.followup_count ? " · Continuation requested" : "") + (row.modelData.unfinished_hint ? " · Unfinished-work hint" : "")
                                color: (row.modelData.observations || []).some(n => n.level === "error") ? Theme.danger : Theme.secondaryText
                                font.pixelSize: Theme.captionSmall
                                elide: Text.ElideRight
                            }
                        }
                    }
                    Label {
                        textFormat: Text.PlainText
                        anchors.centerIn: parent
                        width: parent.width - 24
                        visible: timeline.count === 0
                        text: panel.state.loading ? "Loading trace…" : panel.state.error ? "No history loaded." : "No matching records."
                        horizontalAlignment: Text.AlignHCenter
                        wrapMode: Text.Wrap
                        color: Theme.secondaryText
                    }
                }
                RowLayout {
                    Layout.fillWidth: true
                    NativeButton {
                        text: "Previous"
                        enabled: !panel.state.loading && (panel.snapshot.offset || 0) > 0
                        onClicked: panel.replay.page(Math.max(0, panel.snapshot.offset - 200))
                    }
                    Label {
                        textFormat: Text.PlainText
                        Layout.fillWidth: true
                        horizontalAlignment: Text.AlignHCenter
                        text: panel.state.loading ? "Loading…" : (panel.snapshot.total || 0) + " matches"
                        font.pixelSize: Theme.captionSmall
                        color: Theme.secondaryText
                    }
                    NativeButton {
                        objectName: "replayNextPage"
                        text: "Next"
                        enabled: !panel.state.loading && panel.snapshot.next_offset !== undefined && panel.snapshot.next_offset !== null
                        onClicked: panel.replay.page(panel.snapshot.next_offset)
                    }
                }
            }
            ColumnLayout {
                SplitView.fillWidth: true
                SplitView.minimumWidth: 390
                spacing: Theme.spaceSm
                Layout.leftMargin: Theme.spaceLg
                Label {
                    textFormat: Text.PlainText
                    Layout.fillWidth: true
                    text: panel.state.selected < 0 ? (panel.state.error && panel.snapshot.through === undefined ? "Replay unavailable" : "Understand this session") : (panel.detail.title || (panel.state.detail_loading ? "Loading event…" : "Event unavailable"))
                    font.pixelSize: Theme.sectionTitle
                    font.weight: Font.DemiBold
                    wrapMode: Text.Wrap
                }
                Label {
                    textFormat: Text.PlainText
                    Layout.fillWidth: true
                    visible: panel.state.selected >= 0
                    text: "#" + panel.state.selected + " · " + (panel.detail.kind || "") + "\n" + (panel.detail.at || "")
                    font.pixelSize: Theme.captionSmall
                    color: Theme.secondaryText
                    wrapMode: Text.Wrap
                }
                RowLayout {
                    Layout.fillWidth: true
                    visible: panel.state.selected >= 0
                    spacing: 4
                    Repeater {
                        model: [{id: "detail", label: "Details"}, {id: "context", label: "Context"}, {id: "state", label: "State changes"}, {id: "raw", label: "Raw"}]
                        delegate: NativeButton {
                            required property var modelData
                            objectName: "replayTab_" + modelData.id
                            Layout.fillWidth: true
                            text: modelData.label
                            selected: panel.state.view === modelData.id
                            onClicked: panel.replay.view(modelData.id)
                        }
                    }
                }
                ScrollView {
                    id: detailScroll
                    objectName: "replayDetailScroll"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    contentWidth: availableWidth
                    ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                    TextArea {
                        id: body
                        objectName: "replayDetailText"
                        width: detailScroll.availableWidth
                        readOnly: true
                        selectByMouse: true
                        textFormat: TextEdit.PlainText
                        wrapMode: TextEdit.Wrap
                        font.family: panel.state.view === "raw" ? panel.codeFont : panel.font.family
                        font.pixelSize: Theme.body
                        color: Theme.text
                        padding: Theme.spaceLg
                        background: Rectangle { color: Theme.surface; radius: Theme.controlRadius }
                        text: panel.state.error && panel.snapshot.through === undefined
                            ? "No session history was loaded. The session and any running tasks were left unchanged.\n\nOnce the backend is available, choose Refresh."
                            : panel.state.selected < 0
                            ? "Inspect the execution, not just the conversation.\n\n" + (panel.snapshot.notice || "Loading a read-only snapshot…")
                              + "\n\nWHY STOPPED\n\nLast recorded turn ending: " + (panel.snapshot.stops && panel.snapshot.stops.latest ? panel.snapshot.stops.latest.label : "Not recorded")
                              + "\n" + (panel.snapshot.stops ? panel.snapshot.stops.failures : 0) + " turns ended due to failure, interruption or blocking.\n" + (panel.snapshot.stops ? panel.snapshot.stops.followups : 0) + " user continuation requests after stops.\n" + (panel.snapshot.stops ? panel.snapshot.stops.unfinished_hints : 0) + " final responses mention unfinished work (heuristic, not a verdict)."
                              + "\n\nOBSERVATIONS\n\n" + (panel.snapshot.issue_events || 0) + " events have errors, truncation, repetition or compression to inspect. These are not session-level task verdicts.\nAn open run cannot be classified as stuck from this snapshot alone."
                              + "\n\nHOW TO EXPLORE\n\nSelect an event to inspect its details, context and state changes. Related records link requests, responses and tool results. Search covers recorded text, not just row titles.\n\n"
                              + (panel.snapshot.request_boundaries_recorded ? panel.snapshot.requests + " normal-turn request boundaries recorded. Context for those requests is reconstructed at the preparation boundary." : "This history has no recorded request boundaries. Event-position context is available, but is not an exact historical request capture.")
                              + "\n\nLIMITS\n\nThis is not a filesystem snapshot, a provider wire capture, or access to hidden model reasoning. Images and opaque state remain available in raw records. Auxiliary compaction and goal-check requests do not have normal-turn boundaries."
                            : panel.state.detail_loading ? "Loading evidence…"
                            : (panel.state.view === "detail" && panel.detail.offset === 0 ? (panel.detail.observations || []).map(n => (n.level === "error" ? "ERROR · " : "NOTE · ") + n.text).join("\n\n") + ((panel.detail.observations || []).length ? "\n\n────────────────────\n\n" : "") : "") + (panel.detail.text || "")
                        onTextChanged: detailScroll.ScrollBar.vertical.position = 0
                        TextMenu { id: textMenu; editor: body }
                        TapHandler {
                            acceptedButtons: Qt.RightButton
                            onTapped: function(eventPoint) { textMenu.popup(body, eventPoint.position.x, eventPoint.position.y); }
                        }
                    }
                }
                NativeButton {
                    objectName: "replaySlowestRequest"
                    Layout.fillWidth: true
                    visible: panel.state.selected < 0 && !!panel.snapshot.slowest_request
                    text: "Longest recorded request · " + (panel.snapshot.slowest_request ? (panel.snapshot.slowest_request.elapsed_ms / 1000).toFixed(2) : "0") + " s →"
                    onClicked: panel.replay.select(panel.snapshot.slowest_request.seq)
                }
                NativeCombo {
                    id: related
                    objectName: "replayRelated"
                    Layout.fillWidth: true
                    visible: panel.state.selected >= 0 && (panel.detail.links || []).length > 0
                    model: [{seq: -1, title: "Related evidence…"}].concat(panel.detail.links || [])
                    textRole: "title"
                    contentItem: Text {
                        text: related.displayText
                        textFormat: Text.PlainText
                        color: Theme.text
                        font.pixelSize: Theme.body
                        verticalAlignment: Text.AlignVCenter
                        elide: Text.ElideRight
                    }
                    delegate: ItemDelegate {
                        id: linkRow
                        required property var modelData
                        required property int index
                        width: related.width - 10
                        height: Theme.controlHeight
                        highlighted: related.highlightedIndex === index
                        contentItem: Text {
                            text: linkRow.modelData.title
                            textFormat: Text.PlainText
                            color: Theme.text
                            font.pixelSize: Theme.body
                            verticalAlignment: Text.AlignVCenter
                            elide: Text.ElideRight
                        }
                        background: Rectangle {
                            radius: Theme.controlRadius
                            color: linkRow.highlighted ? Theme.selection : "transparent"
                        }
                    }
                    onActivated: {
                        const link = model[currentIndex];
                        if (link.seq >= 0) panel.replay.select(link.seq);
                        currentIndex = 0;
                    }
                }
                RowLayout {
                    Layout.fillWidth: true
                    visible: panel.state.selected >= 0 && ((panel.detail.offset || 0) > 0 || (panel.detail.next_offset !== null && panel.detail.next_offset !== undefined))
                    NativeButton {
                        text: "Previous text"
                        enabled: !panel.state.detail_loading && (panel.detail.offset || 0) > 0
                        onClicked: panel.replay.detailPage(Math.max(0, panel.detail.offset - 24000))
                    }
                    Label {
                        textFormat: Text.PlainText
                        Layout.fillWidth: true
                        text: "Characters " + ((panel.detail.offset || 0) + 1) + "–" + Math.min((panel.detail.offset || 0) + 24000, panel.detail.text_length || 0) + " / " + (panel.detail.text_length || 0)
                        horizontalAlignment: Text.AlignHCenter
                        color: Theme.secondaryText
                        font.pixelSize: Theme.captionSmall
                        wrapMode: Text.Wrap
                    }
                    NativeButton {
                        objectName: "replayNextText"
                        text: "Next text"
                        enabled: !panel.state.detail_loading && panel.detail.next_offset !== undefined && panel.detail.next_offset !== null
                        onClicked: panel.replay.detailPage(panel.detail.next_offset)
                    }
                }
            }
        }
    }
}
