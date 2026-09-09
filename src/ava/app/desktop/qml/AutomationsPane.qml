pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Pane {
    id: page
    objectName: "automationsPane"
    required property var backend
    required property bool sidebarVisible
    readonly property var tasks: backend.automations
    readonly property var detail: tasks.detail
    readonly property bool narrow: width < 750
    signal openChat(string identity)
    signal closeRequested
    signal sidebarRequested
    padding: narrow ? 16 : 24
    background: Rectangle { color: page.palette.window }
    Component.onCompleted: tasks.activate(true)
    Component.onDestruction: { if (tasks) tasks.activate(false); }
    function timeLabel(value) {
        return value ? Qt.formatDateTime(new Date(value * 1000), "MMM d, yyyy · hh:mm") : "No upcoming runs";
    }
    function statusLabel(value) {
        return ({active: "Scheduled", paused: "Paused", completed: "Completed", starting: "Starting", running: "Running", failed: "Failed", interrupted: "Interrupted", stopped: "Stopped"})[value] || value;
    }
    function cadenceLabel(schedule) {
        if (schedule.cadence === "once") return "Once";
        if (schedule.every === 1) return ({minutes: "Every minute", hours: "Hourly", days: "Daily", weeks: "Weekly"})[schedule.cadence];
        return "Every " + schedule.every + " " + schedule.cadence;
    }
    ColumnLayout {
        anchors.fill: parent
        spacing: 18
        RowLayout {
            Layout.fillWidth: true
            NativeButton {
                visible: !page.sidebarVisible
                icon.source: "icons/left.svg"
                quiet: true
                tip: "Show sidebar"
                onClicked: page.sidebarRequested()
            }
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 4
                Label { text: "Automations"; font.pixelSize: 23; font.weight: Font.DemiBold }
                Label {
                    Layout.fillWidth: true
                    text: "Schedule work. Come back to the results."
                    color: palette.placeholderText
                    wrapMode: Text.WordWrap
                    font.pixelSize: 12
                }
            }
            NativeButton {
                objectName: "newAutomationButton"
                text: page.narrow ? "+ New" : "+ New automation"
                primary: true
                enabled: page.tasks.canCreate
                onClicked: page.tasks.edit("")
            }
            NativeButton {
                objectName: "closeAutomationsButton"
                text: "Back to chat"
                visible: !page.narrow
                onClicked: page.closeRequested()
            }
        }
        RowLayout {
            Layout.fillWidth: true
            visible: !!page.tasks.error || !!page.tasks.notice
            Label {
                Layout.fillWidth: true
                text: page.tasks.error || page.tasks.notice
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                color: palette.placeholderText
                font.pixelSize: 12
            }
            NativeButton { text: "Retry"; onClicked: page.tasks.refresh() }
        }
        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 24
            ColumnLayout {
                visible: !page.narrow || !page.tasks.selected
                Layout.fillWidth: page.narrow
                Layout.preferredWidth: 280
                Layout.fillHeight: true
                spacing: 10
                NativeField {
                    id: search
                    objectName: "automationSearch"
                    Layout.fillWidth: true
                    placeholderText: "Search tasks, projects, machines"
                    Component.onCompleted: text = page.tasks.filters.search
                    onTextEdited: searchDelay.restart()
                    Timer { id: searchDelay; interval: 120; onTriggered: page.tasks.filter("search", search.text) }
                }
                NativeCombo {
                    objectName: "automationStatusFilter"
                    Layout.fillWidth: true
                    model: ["All tasks", "Scheduled", "Paused", "Completed"]
                    currentIndex: ["", "active", "paused", "completed"].indexOf(page.tasks.filters.state)
                    onActivated: page.tasks.filter("state", ["", "active", "paused", "completed"][currentIndex])
                }
                ListView {
                    id: taskList
                    objectName: "automationList"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    reuseItems: true
                    cacheBuffer: 100
                    spacing: 6
                    model: page.tasks.rows
                    ScrollBar.vertical: ScrollBar {}
                    Label {
                        anchors.centerIn: parent
                        width: parent.width - 24
                        horizontalAlignment: Text.AlignHCenter
                        wrapMode: Text.WordWrap
                        visible: !taskList.count
                        text: page.tasks.loading ? "Loading tasks…" : "No tasks here yet.\nCreate an automation to get started."
                        color: palette.placeholderText
                        font.pixelSize: 13
                    }
                    delegate: ItemDelegate {
                        id: task
                        required property var entry
                        objectName: "automationTask_" + entry.id
                        width: taskList.width
                        height: 94
                        padding: 12
                        onClicked: page.tasks.select(entry.id)
                        background: Rectangle {
                            radius: 11
                            color: page.tasks.selected === task.entry.id ? page.palette.alternateBase : task.hovered ? page.palette.light : "transparent"
                            border.color: task.activeFocus ? page.palette.highlight : "transparent"
                        }
                        contentItem: ColumnLayout {
                            spacing: 5
                            Label {
                                Layout.fillWidth: true
                                text: task.entry.name
                                textFormat: Text.PlainText
                                elide: Text.ElideRight
                                font.pixelSize: 14
                                font.weight: Font.Medium
                            }
                            Label {
                                Layout.fillWidth: true
                                text: task.entry.project + " · " + task.entry.machine
                                textFormat: Text.PlainText
                                elide: Text.ElideMiddle
                                color: palette.placeholderText
                                font.pixelSize: 11
                            }
                            Label {
                                text: (task.entry.online ? "" : "Offline · ") + page.statusLabel(task.entry.active_run || task.entry.state)
                                color: task.entry.active_run ? "#4b9b70" : palette.placeholderText
                                font.pixelSize: 11
                            }
                        }
                    }
                }
                NativeButton { visible: page.narrow; text: "Back to chat"; onClicked: page.closeRequested() }
            }
            Rectangle { visible: !page.narrow; Layout.fillHeight: true; implicitWidth: 1; color: page.palette.mid }
            ColumnLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                visible: !page.narrow || !!page.tasks.selected
                NativeButton {
                    visible: page.narrow
                    text: "‹ All tasks"
                    quiet: true
                    onClicked: page.tasks.select("")
                }
                Label {
                    visible: !page.tasks.selected
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    text: "Your next routine, taken care of.\nSelect a task to view its schedule and results."
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                    wrapMode: Text.WordWrap
                    color: palette.placeholderText
                }
                ListView {
                    id: history
                    objectName: "automationRunHistory"
                    visible: !!page.tasks.selected
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    model: page.tasks.runRows
                    currentIndex: -1
                    keyNavigationEnabled: false
                    highlightFollowsCurrentItem: false
                    clip: true
                    reuseItems: true
                    cacheBuffer: 100
                    spacing: 10
                    property bool readingStart: true
                    onMovementEnded: readingStart = atYBeginning
                    onOriginYChanged: {
                        if (readingStart && !moving)
                            Qt.callLater(() => { if (history.readingStart && !history.moving) history.positionViewAtBeginning(); });
                    }
                    ScrollBar.vertical: ScrollBar {
                        onPressedChanged: { if (!pressed) history.readingStart = history.atYBeginning; }
                    }
                    header: ColumnLayout {
                        width: history.width
                        height: implicitHeight
                        spacing: 14
                        Label {
                            objectName: "automationDetailTitle"
                            Layout.fillWidth: true
                            Layout.minimumHeight: implicitHeight
                            text: page.detail.name || ""
                            textFormat: Text.PlainText
                            wrapMode: Text.WordWrap
                            font.pixelSize: 21
                            font.weight: Font.DemiBold
                        }
                        Label {
                            Layout.fillWidth: true
                            text: (page.detail.project || "") + " · " + (page.detail.machine || "") + (page.detail.workspace === "worktree" ? " · New worktree per run" : " · Current folder")
                            textFormat: Text.PlainText
                            wrapMode: Text.WordWrap
                            color: palette.placeholderText
                            font.pixelSize: 12
                        }
                        Label {
                            Layout.fillWidth: true
                            text: (page.detail.prompt || "").slice(0, 1500)
                            textFormat: Text.PlainText
                            wrapMode: Text.WordWrap
                            maximumLineCount: 5
                            elide: Text.ElideRight
                            font.pixelSize: 13
                        }
                        Rectangle {
                            Layout.fillWidth: true
                            implicitHeight: scheduleInfo.implicitHeight + 24
                            radius: 12
                            color: page.palette.window.hslLightness < 0.5 ? "#292929" : "#f7f7f8"
                            ColumnLayout {
                                id: scheduleInfo
                                anchors.fill: parent
                                anchors.margins: 12
                                spacing: 7
                                Label {
                                    Layout.fillWidth: true
                                    text: page.statusLabel(page.detail.state || "") + " · " + (page.detail.remaining || 0) + " scheduled runs left"
                                    wrapMode: Text.WordWrap
                                    font.weight: Font.Medium
                                    font.pixelSize: 12
                                }
                                Label {
                                    Layout.fillWidth: true
                                    text: page.detail.enabled && page.detail.next_due ? "Next: " + page.timeLabel(page.detail.next_due) + " (your local time)" : "No upcoming runs"
                                    wrapMode: Text.WordWrap
                                    font.pixelSize: 12
                                    color: palette.placeholderText
                                }
                                Label {
                                    Layout.fillWidth: true
                                    text: page.detail.schedule ? page.cadenceLabel(page.detail.schedule) + " · " + page.detail.schedule.timezone + (page.detail.skipped ? " · " + page.detail.skipped + " skipped" : "") : ""
                                    wrapMode: Text.WordWrap
                                    font.pixelSize: 11
                                    color: palette.placeholderText
                                }
                            }
                        }
                        Flow {
                            Layout.fillWidth: true
                            spacing: 8
                            NativeButton {
                                objectName: "runAutomationNowButton"
                                text: "Run now"
                                primary: true
                                enabled: !!page.detail.online && !page.tasks.busy && !page.detail.active_run
                                tip: "Start an extra run without using a scheduled repetition"
                                onClicked: page.tasks.action("run")
                            }
                            NativeButton {
                                objectName: "pauseAutomationButton"
                                text: page.detail.enabled ? "Pause schedule" : "Resume schedule"
                                enabled: !!page.detail.online && !page.tasks.busy && page.detail.remaining > 0
                                onClicked: page.tasks.action("pause")
                            }
                            NativeButton {
                                objectName: "editAutomationButton"
                                text: "Edit"
                                enabled: !!page.detail.online && !page.tasks.busy
                                onClicked: page.tasks.edit(page.tasks.selected)
                            }
                            NativeButton {
                                objectName: "removeAutomationButton"
                                text: "Remove"
                                quiet: true
                                enabled: !!page.detail.online && !page.tasks.busy
                                onClicked: removeDialog.open()
                            }
                        }
                        Label {
                            Layout.fillWidth: true
                            text: "Runs on " + (page.detail.machine || "this machine") + " while its Ava backend is running. Pausing the schedule leaves any current run active."
                            textFormat: Text.PlainText
                            wrapMode: Text.WordWrap
                            color: palette.placeholderText
                            font.pixelSize: 11
                        }
                        Label { text: "Run history"; font.weight: Font.DemiBold; font.pixelSize: 14; Layout.topMargin: 8 }
                        Label {
                            visible: !history.count
                            text: "Each run creates a new conversation. Results will appear here."
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            font.pixelSize: 12
                            color: palette.placeholderText
                        }
                        Item { implicitHeight: 2 }
                    }
                    delegate: Pane {
                        id: run
                        required property var entry
                        width: history.width
                        padding: 12
                        implicitHeight: runContents.implicitHeight + 24
                        background: Rectangle { radius: 11; color: page.palette.base; border.color: page.palette.mid }
                        ColumnLayout {
                            id: runContents
                            width: parent.width
                            spacing: 7
                            RowLayout {
                                Layout.fillWidth: true
                                Label {
                                    Layout.fillWidth: true
                                    text: page.statusLabel(run.entry.status)
                                    font.pixelSize: 13
                                    font.weight: Font.Medium
                                }
                                NativeButton {
                                    objectName: "openAutomationRun_" + run.entry.id
                                    text: "Open chat"
                                    enabled: !!run.entry.chat_id && !!page.detail.online
                                    onClicked: page.openChat(run.entry.chat_id)
                                }
                                NativeButton {
                                    objectName: "stopAutomationRun_" + run.entry.id
                                    visible: ["running", "starting", "paused"].includes(run.entry.status)
                                    text: "Stop"
                                    enabled: !!page.detail.online && !page.tasks.busy
                                    onClicked: page.tasks.action("stop:" + run.entry.id)
                                }
                            }
                            Label {
                                Layout.fillWidth: true
                                text: page.timeLabel(run.entry.scheduled_for) + (run.entry.occurrence == null ? " · Manual" : " · Scheduled")
                                wrapMode: Text.WordWrap
                                font.pixelSize: 11
                                color: palette.placeholderText
                            }
                            Label {
                                Layout.fillWidth: true
                                visible: !!run.entry.error
                                text: run.entry.error || ""
                                textFormat: Text.PlainText
                                wrapMode: Text.WrapAnywhere
                                color: "#ba884b"
                                font.pixelSize: 12
                            }
                        }
                    }
                    footer: NativeButton {
                        visible: !!page.detail.has_more
                        text: "Review more"
                        onClicked: page.tasks.more()
                    }
                }
            }
        }
    }
    AutomationDialog { tasks: page.tasks }
    NativeDialog {
        id: removeDialog
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(440, parent.width - 40)
        modal: true
        title: "Remove automation?"
        padding: 22
        background: Rectangle { radius: 16; color: page.palette.base; border.color: page.palette.mid }
        ColumnLayout {
            width: parent.width
            spacing: 18
            Label {
                Layout.fillWidth: true
                text: "Future runs will stop. Existing conversations and any run already in progress are kept."
                wrapMode: Text.WordWrap
            }
            RowLayout {
                Layout.alignment: Qt.AlignRight
                NativeButton { text: "Cancel"; onClicked: removeDialog.close() }
                NativeButton { objectName: "confirmRemoveAutomationButton"; text: "Remove"; primary: true; onClicked: { page.tasks.action("remove"); removeDialog.close(); } }
            }
        }
    }
}
