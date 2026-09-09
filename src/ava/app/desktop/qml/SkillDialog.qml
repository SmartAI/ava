pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

NativeDialog {
    id: dialog
    required property var skills
    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(660, parent.width - 40)
    height: Math.min(710, parent.height - 40)
    modal: true
    padding: 22
    title: "New skill"
    closePolicy: skills.busy ? Popup.NoAutoClose : Popup.CloseOnEscape
    background: Rectangle { radius: 16; color: dialog.palette.base; border.color: dialog.palette.mid }
    onOpened: name.forceActiveFocus()
    Connections {
        target: dialog.skills
        function onSaved() { name.text = ""; description.text = ""; instructions.text = ""; dialog.close(); }
    }
    contentItem: ColumnLayout {
        spacing: 14
        ScrollView {
            id: scroll
            Layout.fillWidth: true
            Layout.fillHeight: true
            contentWidth: availableWidth
            clip: true
            ColumnLayout {
                width: scroll.availableWidth
                spacing: 8
                Label { text: "Name"; font.weight: Font.DemiBold }
                NativeField { id: name; objectName: "skillNameField"; Layout.fillWidth: true; placeholderText: "review-changes"; maximumLength: 64; enabled: !dialog.skills.busy }
                Label { text: "Description · when should Ava use it?"; Layout.fillWidth: true; wrapMode: Text.Wrap; font.weight: Font.DemiBold }
                NativeField { id: description; objectName: "skillDescriptionField"; Layout.fillWidth: true; placeholderText: "Review a diff for correctness and missing tests."; maximumLength: 1024; enabled: !dialog.skills.busy }
                Label { text: "Location"; font.weight: Font.DemiBold }
                NativeCombo { id: location; objectName: "skillScopeChoice"; Layout.fillWidth: true; model: ["This project", "Personal · all projects on this machine"]; enabled: !dialog.skills.busy }
                Label { text: "Instructions · Markdown supported"; font.weight: Font.DemiBold }
                TextArea {
                    id: instructions
                    objectName: "skillBodyField"
                    Layout.fillWidth: true
                    Layout.minimumHeight: 220
                    Layout.preferredHeight: Math.max(220, implicitHeight)
                    placeholderText: "# Review changes\n\nDescribe the steps, expected results and checks."
                    wrapMode: TextEdit.Wrap
                    selectByMouse: true
                    padding: 12
                    enabled: !dialog.skills.busy
                    ContextMenu.menu: TextMenu { editor: instructions }
                    background: Rectangle { radius: 10; color: dialog.palette.base; border.color: instructions.activeFocus ? dialog.palette.highlight : dialog.palette.mid }
                }
                Label { Layout.fillWidth: true; wrapMode: Text.Wrap; font.pixelSize: 11; color: palette.placeholderText; text: "Creates SKILL.md on the selected machine. Project skills live in .agents/skills; personal skills live in Ava's data folder." }
            }
        }
        Label { Layout.fillWidth: true; visible: !!text; text: dialog.skills.editorError; wrapMode: Text.Wrap; color: palette.link }
        RowLayout {
            Item { Layout.fillWidth: true }
            NativeButton { text: "Cancel"; enabled: !dialog.skills.busy; onClicked: dialog.close() }
            NativeButton {
                objectName: "saveSkillButton"
                text: dialog.skills.busy ? "Saving…" : "Create skill"
                enabled: !dialog.skills.busy && !!name.text.trim() && !!description.text.trim() && !!instructions.text.trim()
                onClicked: dialog.skills.create({name: name.text.trim(), description: description.text.trim(), body: instructions.text, scope: location.currentIndex ? "global" : "project"})
            }
        }
    }
}
