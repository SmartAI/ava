pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Pane {
    id: page
    objectName: "skillsPane"
    required property var backend
    required property bool sidebarVisible
    readonly property var skills: backend.skillView
    readonly property var detail: skills.detail
    readonly property bool narrow: width < 720
    signal closeRequested
    signal sidebarRequested
    signal useRequested
    padding: narrow ? Theme.spaceLg : Theme.spaceXl
    background: Rectangle { color: page.palette.window }
    Component.onCompleted: skills.activate(true)
    Component.onDestruction: { if (skills) skills.activate(false); }
    Connections { target: page.skills; function onUsed() { page.useRequested(); } }
    function status(entry) {
        if (entry.state === "removed") return "Removed";
        if (entry.error) return "Needs attention";
        if (entry.shadowed_by) return "Overridden";
        return entry.effective ? "Enabled" : "Disabled";
    }
    ColumnLayout {
        anchors.fill: parent
        spacing: 16
        PageHeader {
            Layout.fillWidth: true
            title: "Skills"
            description: "Reusable instructions for the work you do."
            sidebarVisible: page.sidebarVisible
            onSidebarRequested: page.sidebarRequested()
            NativeButton { objectName: "refreshSkillsButton"; icon.source: "icons/reload.svg"; quiet: true; tip: "Refresh skills"; enabled: page.skills.available && !page.skills.busy; onClicked: page.skills.refresh() }
            NativeButton { objectName: "newSkillButton"; text: "New skill"; primary: true; enabled: page.skills.available && !page.skills.busy; onClicked: { page.skills.beginCreate(); editor.open(); } }
            NativeButton { objectName: "closeSkillsButton"; text: "Done"; quiet: true; onClicked: page.closeRequested() }
        }
        NativeCombo {
            objectName: "skillProjectChoice"
            Layout.fillWidth: true
            model: page.skills.projects
            textRole: "name"
            valueRole: "id"
            currentIndex: model.findIndex(item => item.id === page.skills.project)
            enabled: !page.skills.busy
            onActivated: page.skills.chooseProject(currentValue)
        }
        Label {
            Layout.fillWidth: true
            visible: !!text
            text: page.skills.error || (!page.skills.available ? "Connect or update this machine to manage its skills." : "")
            wrapMode: Text.Wrap
            color: page.palette.link
        }
        SplitView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            handle: Rectangle { implicitWidth: 9; color: "transparent"; Rectangle { width: 1; height: parent.height; anchors.centerIn: parent; color: page.palette.mid } }
            ColumnLayout {
                visible: !page.narrow || !page.skills.selected
                SplitView.preferredWidth: 300
                SplitView.minimumWidth: 220
                SplitView.fillWidth: page.narrow
                spacing: 12
                NativeField {
                    id: search
                    objectName: "skillSearch"
                    Layout.fillWidth: true
                    placeholderText: "Search skills"
                    text: page.skills.filters.search
                    onTextEdited: delay.restart()
                    Timer { id: delay; interval: 120; onTriggered: page.skills.filter("search", search.text) }
                }
                NativeCombo {
                    objectName: "skillStateFilter"
                    Layout.fillWidth: true
                    model: ["Available", "Enabled", "Disabled", "Removed"]
                    currentIndex: ["", "enabled", "disabled", "removed"].indexOf(page.skills.filters.state)
                    onActivated: page.skills.filter("state", ["", "enabled", "disabled", "removed"][currentIndex])
                }
                ListView {
                    id: list
                    objectName: "skillList"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    reuseItems: true
                    cacheBuffer: 100
                    spacing: 6
                    model: page.skills.rows
                    ScrollBar.vertical: ScrollBar {}
                    EmptyState {
                        anchors.centerIn: parent
                        width: parent.width - 24
                        visible: !list.count
                        title: page.skills.busy ? "Loading skills…" : "No skills here yet"
                        description: page.skills.busy ? "" : "Create one, or choose another project."
                    }
                    delegate: ItemDelegate {
                        id: row
                        required property var entry
                        objectName: "skillRow_" + entry.name
                        width: list.width
                        height: 106
                        padding: 12
                        onClicked: page.skills.select(entry.id)
                        background: Surface { border.width: 0; focused: row.visualFocus; color: page.skills.selected === row.entry.id ? Theme.selection : row.hovered ? Theme.hover : "transparent" }
                        contentItem: ColumnLayout {
                            spacing: 5
                            Label { text: row.entry.name; font.weight: Font.DemiBold; Layout.fillWidth: true; elide: Text.ElideRight }
                            Label { text: row.entry.description || row.entry.error; Layout.fillWidth: true; font.pixelSize: 12; color: palette.placeholderText; maximumLineCount: 2; wrapMode: Text.Wrap; elide: Text.ElideRight }
                            Label { text: row.entry.source + " · " + page.status(row.entry); font.pixelSize: 11; color: palette.placeholderText }
                        }
                    }
                }
            }
            ColumnLayout {
                visible: !page.narrow || !!page.skills.selected
                SplitView.fillWidth: true
                SplitView.minimumWidth: 300
                spacing: 12
                NativeButton { visible: page.narrow; text: "‹ All skills"; quiet: true; onClicked: page.skills.select("") }
                ScrollView {
                    id: preview
                    objectName: "skillPreview"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.leftMargin: page.narrow ? 0 : 16
                    clip: true
                    contentWidth: availableWidth
                    ColumnLayout {
                        width: preview.availableWidth
                        spacing: 14
                        Label { Layout.fillWidth: true; text: page.detail.name || "Choose a skill"; font.pixelSize: 21; font.weight: Font.DemiBold; wrapMode: Text.Wrap }
                        Label { Layout.fillWidth: true; text: page.detail.description || "Give Ava reusable instructions for the work you do."; wrapMode: Text.Wrap; color: palette.placeholderText }
                        Label { Layout.fillWidth: true; visible: !!page.detail.path; text: (page.detail.source || "") + " · " + page.status(page.detail) + "\n" + (page.detail.location || ""); wrapMode: Text.WrapAnywhere; font.pixelSize: 11; color: palette.placeholderText }
                        Label { Layout.fillWidth: true; visible: !!text; text: page.detail.error || (page.detail.shadowed_by ? "A higher-priority skill uses this name: " + page.detail.shadowed_by : ""); wrapMode: Text.WrapAnywhere; color: palette.link }
                        Flow {
                            Layout.fillWidth: true
                            spacing: 8
                            visible: !!page.detail.id
                            NativeButton { objectName: "toggleSkillButton"; text: page.detail.state === "removed" ? "Restore" : page.detail.state === "enabled" ? "Disable" : "Enable"; enabled: page.skills.available && !page.skills.busy; onClicked: page.skills.setState(page.detail.state === "enabled" ? "disabled" : "enabled") }
                            NativeButton { objectName: "useSkillButton"; text: "Use in chat"; visible: page.skills.canUse; enabled: !page.skills.busy; onClicked: page.skills.use() }
                            NativeButton { objectName: "removeSkillButton"; text: "Remove from Ava…"; quiet: true; visible: page.detail.state !== "removed"; enabled: page.skills.available && !page.skills.busy; onClicked: removeDialog.open() }
                            NativeButton { text: "Copy path"; quiet: true; tip: page.detail.path || ""; onClicked: page.backend.copyText(page.detail.path) }
                        }
                        Label { Layout.fillWidth: true; visible: !!page.detail.id; text: "Changes apply at the next step. Existing conversations keep instructions already used. Personal and shared skills apply to every project on this machine."; wrapMode: Text.Wrap; font.pixelSize: 11; color: palette.placeholderText }
                        Label { Layout.fillWidth: true; visible: !!page.detail.truncated; text: "Preview limited to 64 KiB. The complete instructions remain in SKILL.md."; wrapMode: Text.Wrap; color: palette.placeholderText }
                        Loader {
                            Layout.fillWidth: true
                            active: !!page.detail.body
                            sourceComponent: MarkdownText {
                                objectName: "skillInstructionsPreview"
                                backend: page.backend
                                readOnly: true
                                selectByMouse: true
                                text: page.detail.body || ""
                                textFormat: page.detail.truncated ? TextEdit.PlainText : TextEdit.MarkdownText
                                wrapMode: TextEdit.Wrap
                                padding: 4
                                font.pixelSize: 14
                                onLinkActivated: function(link) { page.backend.openLink(link); }
                            }
                        }
                    }
                }
            }
        }
    }
    SkillDialog { id: editor; skills: page.skills }
    NativeDialog {
        id: removeDialog
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(450, parent.width - 40)
        modal: true
        padding: 22
        title: "Remove this skill from Ava?"
        contentItem: ColumnLayout {
            spacing: 16
            Label { Layout.fillWidth: true; text: "Ava will stop offering this skill. Source files and other applications are unaffected. You can restore it from the Removed filter."; wrapMode: Text.Wrap }
            RowLayout {
                Item { Layout.fillWidth: true }
                NativeButton { text: "Cancel"; onClicked: removeDialog.close() }
                NativeButton { objectName: "confirmRemoveSkillButton"; text: "Remove"; onClicked: { page.skills.setState("removed"); removeDialog.close(); } }
            }
        }
    }
}
