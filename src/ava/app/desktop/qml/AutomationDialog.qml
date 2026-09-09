pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

NativeDialog {
    id: dialog
    required property var tasks
    readonly property var state: tasks.editorState
    property var draft: ({})
    property bool loadingDraft: false
    readonly property var projectOptions: tasks.projects.filter(project => project.machine === machine.currentValue)
    readonly property var cadences: ["once", "minutes", "hours", "days", "weeks"]
    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(680, parent.width - 40)
    height: Math.min(760, parent.height - 40)
    modal: true
    padding: 22
    title: draft.id ? "Edit automation" : "New automation"
    closePolicy: state.busy ? Popup.NoAutoClose : Popup.CloseOnEscape
    background: Rectangle { radius: 16; color: dialog.palette.base; border.color: dialog.palette.mid }
    function schedule() {
        return {start_local: dateField.text.trim() + "T" + timeField.text.trim() + (timeField.text.trim().length === 5 ? ":00" : ""), timezone: zoneField.text.trim(), cadence: cadences[cadence.currentIndex], every: cadence.currentIndex ? Number(everyField.text) : 1, count: cadence.currentIndex ? Number(countField.text) : 1};
    }
    function requestPreview() { if (opened && !loadingDraft) previewDelay.restart(); }
    function save() {
        const body = {name: nameField.text.trim(), prompt: promptField.text.trim(), project_id: project.currentValue, workspace: workspace.currentIndex ? "worktree" : "current", base_ref: baseField.text.trim() || "HEAD", schedule: schedule()};
        if (providerField.text.trim()) body.provider = providerField.text.trim();
        if (modelField.text.trim()) body.model = modelField.text.trim();
        if (effort.currentIndex) body.effort = effort.currentText;
        if (draft.id) body.version = draft.version;
        tasks.save(machine.currentValue, draft.id || "", body);
    }
    Connections {
        target: dialog.tasks
        function onEditRequested(data) {
            dialog.loadingDraft = true;
            dialog.draft = data;
            nameField.text = data.name;
            promptField.text = data.prompt;
            machine.currentIndex = machine.model.findIndex(item => item.id === data.machine_id);
            project.currentIndex = dialog.projectOptions.findIndex(item => item.id === data.project_id);
            workspace.currentIndex = data.workspace === "worktree" ? 1 : 0;
            baseField.text = data.base_ref || "HEAD";
            providerField.text = data.provider || "";
            modelField.text = data.model || "";
            effort.currentIndex = Math.max(0, effort.model.indexOf(data.effort));
            const parts = data.schedule.start_local.split("T");
            dateField.text = parts[0];
            timeField.text = parts[1].endsWith(":00") ? parts[1].slice(0, 5) : parts[1];
            zoneField.text = data.schedule.timezone;
            cadence.currentIndex = dialog.cadences.indexOf(data.schedule.cadence);
            everyField.text = String(data.schedule.every);
            countField.text = String(data.schedule.count);
            dialog.loadingDraft = false;
            dialog.open();
        }
        function onSaved() { dialog.close(); }
    }
    onOpened: { nameField.forceActiveFocus(); dialog.requestPreview(); }
    Timer { id: previewDelay; interval: 300; onTriggered: dialog.tasks.preview(machine.currentValue || "", dialog.schedule()) }
    contentItem: ColumnLayout {
        spacing: 16
        ScrollView {
            id: scroll
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            contentWidth: availableWidth
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            ColumnLayout {
                width: scroll.availableWidth
                spacing: 14
                enabled: !dialog.state.busy
                Label {
                    Layout.fillWidth: true
                    text: "Give Ava a task and choose when it should run. Each result opens in its own conversation."
                    wrapMode: Text.WordWrap
                    font.pixelSize: 12
                    color: palette.placeholderText
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 6
                    Label { text: "Name"; font.pixelSize: 12 }
                    NativeField { id: nameField; objectName: "automationNameField"; Layout.fillWidth: true; maximumLength: 120; placeholderText: "Daily project brief" }
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 6
                    Label { text: "What should Ava do?"; font.pixelSize: 12 }
                    ScrollView {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 100
                        clip: true
                        TextArea {
                            id: promptField
                            objectName: "automationPromptField"
                            placeholderText: "Review recent changes and summarize what needs attention…"
                            wrapMode: TextEdit.Wrap
                            selectByMouse: true
                            font.pixelSize: 13
                            padding: 10
                            ContextMenu.menu: TextMenu { editor: promptField }
                            background: Rectangle { radius: 9; color: dialog.palette.base; border.color: promptField.activeFocus ? dialog.palette.highlight : dialog.palette.mid }
                        }
                    }
                }
                GridLayout {
                    Layout.fillWidth: true
                    columns: 2
                    columnSpacing: 12
                    rowSpacing: 6
                    Label { text: "Run on machine"; font.pixelSize: 12 }
                    Label { text: "Project"; font.pixelSize: 12 }
                    NativeCombo {
                        id: machine
                        objectName: "automationMachineChoice"
                        Layout.fillWidth: true
                        Layout.minimumWidth: 100
                        model: dialog.tasks.machines
                        textRole: "name"
                        valueRole: "id"
                        enabled: !dialog.draft.id
                        onActivated: { project.currentIndex = 0; dialog.requestPreview(); }
                    }
                    NativeCombo {
                        id: project
                        objectName: "automationProjectChoice"
                        Layout.fillWidth: true
                        Layout.minimumWidth: 100
                        model: dialog.projectOptions
                        textRole: "name"
                        valueRole: "id"
                    }
                    Label { text: "Workspace"; font.pixelSize: 12; Layout.topMargin: 6 }
                    Label { text: "Start worktree from"; font.pixelSize: 12; Layout.topMargin: 6; opacity: workspace.currentIndex ? 1 : 0.5 }
                    NativeCombo { id: workspace; Layout.fillWidth: true; model: ["Current folder", "New worktree per run"] }
                    NativeField { id: baseField; Layout.fillWidth: true; enabled: workspace.currentIndex > 0; placeholderText: "HEAD" }
                }
                Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: dialog.palette.mid; Layout.topMargin: 4; Layout.bottomMargin: 4 }
                Label { text: "Schedule"; font.pixelSize: 14; font.weight: Font.DemiBold }
                GridLayout {
                    Layout.fillWidth: true
                    columns: 2
                    columnSpacing: 12
                    rowSpacing: 6
                    Label { text: "First date · YYYY-MM-DD"; font.pixelSize: 12 }
                    Label { text: "Time · 24-hour"; font.pixelSize: 12 }
                    NativeField { id: dateField; Layout.fillWidth: true; placeholderText: "2026-09-10"; maximumLength: 10; onTextChanged: dialog.requestPreview() }
                    NativeField { id: timeField; Layout.fillWidth: true; placeholderText: "09:00"; maximumLength: 8; onTextChanged: dialog.requestPreview() }
                    Label { text: "Time zone"; font.pixelSize: 12; Layout.topMargin: 6 }
                    Label { text: "Repeat"; font.pixelSize: 12; Layout.topMargin: 6 }
                    NativeField { id: zoneField; Layout.fillWidth: true; placeholderText: "America/Vancouver"; onTextChanged: dialog.requestPreview() }
                    NativeCombo {
                        id: cadence
                        Layout.fillWidth: true
                        model: ["Once", "Every N minutes", "Every N hours", "Every N days", "Every N weeks"]
                        onActivated: dialog.requestPreview()
                    }
                    Label { text: "Interval"; font.pixelSize: 12; Layout.topMargin: 6; visible: cadence.currentIndex > 0 }
                    Label { text: "Total scheduled runs"; font.pixelSize: 12; Layout.topMargin: 6; visible: cadence.currentIndex > 0 }
                    NativeField {
                        id: everyField
                        Layout.fillWidth: true
                        visible: cadence.currentIndex > 0
                        validator: IntValidator { bottom: 1; top: 36500 }
                        onTextChanged: dialog.requestPreview()
                    }
                    NativeField {
                        id: countField
                        objectName: "automationCountField"
                        Layout.fillWidth: true
                        visible: cadence.currentIndex > 0
                        validator: IntValidator { bottom: 1; top: 1000000 }
                        onTextChanged: dialog.requestPreview()
                    }
                }
                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: previewContents.implicitHeight + 24
                    radius: 11
                    color: dialog.palette.window.hslLightness < 0.5 ? "#292929" : "#f7f7f8"
                    ColumnLayout {
                        id: previewContents
                        anchors.fill: parent
                        anchors.margins: 12
                        spacing: 6
                        Label { text: "Upcoming runs"; font.pixelSize: 12; font.weight: Font.Medium }
                        Repeater {
                            model: dialog.state.preview.occurrences || []
                            Label { required property var modelData; text: modelData.label; font.pixelSize: 12 }
                        }
                        Label {
                            Layout.fillWidth: true
                            visible: !!dialog.state.preview.error || !!dialog.state.preview.skipped
                            text: dialog.state.preview.error || (dialog.state.preview.skipped + " past occurrences will be skipped. Only the latest missed run will execute.")
                            textFormat: Text.PlainText
                            wrapMode: Text.WordWrap
                            color: palette.placeholderText
                            font.pixelSize: 11
                        }
                    }
                }
                Label {
                    Layout.fillWidth: true
                    text: dialog.draft.id ? "Changing the schedule starts a new series with this total. Content edits keep the existing progress."
                        : "Missed times count toward the total; only the latest runs when Ava returns. Runs of the same task never overlap. Keep the execution machine awake with its Ava backend running."
                    wrapMode: Text.WordWrap
                    color: palette.placeholderText
                    font.pixelSize: 11
                }
                NativeButton {
                    id: modelOptions
                    text: checked ? "⌄ Model options" : "› Model options"
                    quiet: true
                    checkable: true
                }
                GridLayout {
                    visible: modelOptions.checked
                    Layout.fillWidth: true
                    columns: 2
                    columnSpacing: 12
                    rowSpacing: 6
                    Label { text: "Provider · optional"; font.pixelSize: 12 }
                    Label { text: "Model · optional"; font.pixelSize: 12 }
                    NativeField { id: providerField; Layout.fillWidth: true; placeholderText: "Machine default" }
                    NativeField { id: modelField; Layout.fillWidth: true; placeholderText: "Machine default" }
                    Label { text: "Reasoning effort"; font.pixelSize: 12; Layout.topMargin: 6 }
                    NativeCombo { id: effort; Layout.fillWidth: true; model: ["Machine default", "low", "medium", "high", "xhigh", "max"] }
                }
            }
        }
        Label {
            Layout.fillWidth: true
            visible: !!dialog.state.error
            text: dialog.state.error || ""
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
            color: "#bf5148"
            font.pixelSize: 12
        }
        RowLayout {
            Layout.fillWidth: true
            Item { Layout.fillWidth: true }
            NativeButton { text: "Cancel"; enabled: !dialog.state.busy; onClicked: dialog.close() }
            NativeButton {
                objectName: "saveAutomationButton"
                text: dialog.state.busy ? "Saving…" : "Save automation"
                primary: true
                enabled: !dialog.state.busy && !!nameField.text.trim() && !!promptField.text.trim() && !!project.currentValue
                onClicked: dialog.save()
            }
        }
    }
}
