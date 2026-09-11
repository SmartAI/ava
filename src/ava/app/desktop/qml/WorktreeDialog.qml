pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

NativeDialog {
    id: dialog
    required property var backend
    readonly property var state: backend.worktreeState
    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(520, parent.width - 40)
    modal: true
    title: "New chat in a worktree"
    closePolicy: state.creating ? Popup.NoAutoClose : Popup.CloseOnEscape
    Connections {
        target: dialog.backend
        function onWorktreeRequested() {
            branchField.text = "ava/" + Qt.formatDateTime(new Date(), "yyyyMMdd-hhmmss");
            dialog.open();
        }
        function onWorktreeCreated() { dialog.close(); }
    }
    onOpened: {
        branchField.forceActiveFocus();
        branchField.selectAll();
    }
    ColumnLayout {
        width: parent.width
        spacing: 16
        Label {
            Layout.fillWidth: true
            text: dialog.backend.workspaceLabel(dialog.state.project || "")
            elide: Text.ElideMiddle
            font.pixelSize: 12
            color: palette.placeholderText
        }
        Label {
            Layout.fillWidth: true
            text: "Work on a new branch in a separate folder. Chats stay grouped under this project."
            wrapMode: Text.WordWrap
            font.pixelSize: 13
        }
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 6
            Label { text: "New branch"; font.pixelSize: 12 }
            NativeField {
                id: branchField
                objectName: "worktreeBranchField"
                Layout.fillWidth: true
                enabled: !dialog.state.creating
                placeholderText: "ava/my-task"
                maximumLength: 200
                onTextChanged: dialog.backend.clearWorktreeError()
                onAccepted: { if (createButton.enabled) createButton.clicked(); }
            }
        }
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 6
            Label { text: "Start from"; font.pixelSize: 12 }
            NativeCombo {
                id: baseChoice
                objectName: "worktreeBaseChoice"
                Layout.fillWidth: true
                model: dialog.state.refs || []
                textRole: "name"
                valueRole: "ref"
                enabled: !dialog.state.loading && !dialog.state.creating
            }
        }
        Label {
            Layout.fillWidth: true
            text: "Uncommitted changes stay in the original folder. The worktree is kept when you archive its chat."
            wrapMode: Text.WordWrap
            font.pixelSize: 12
            color: palette.placeholderText
        }
        Label {
            Layout.fillWidth: true
            visible: !!dialog.state.directory
            text: "Location on this machine: " + (dialog.state.directory || "")
            wrapMode: Text.WrapAnywhere
            font.pixelSize: 11
            color: palette.placeholderText
        }
        Label {
            objectName: "worktreeError"
            Layout.fillWidth: true
            visible: !!dialog.state.error || !!dialog.state.loading
            text: dialog.state.loading ? "Loading branches…" : dialog.state.error || ""
            wrapMode: Text.WrapAnywhere
            font.pixelSize: 12
            color: dialog.state.error ? Theme.danger : palette.placeholderText
        }
        RowLayout {
            Layout.fillWidth: true
            NativeButton {
                text: "Reload branches"
                visible: !!dialog.state.error && !(dialog.state.refs || []).length
                onClicked: dialog.backend.prepareWorktree(dialog.state.project)
            }
            Item { Layout.fillWidth: true }
            NativeButton {
                text: "Cancel"
                enabled: !dialog.state.creating
                onClicked: dialog.close()
            }
            NativeButton {
                id: createButton
                objectName: "createWorktreeChatButton"
                text: dialog.state.creating ? "Creating…" : "Create chat"
                primary: true
                enabled: !dialog.state.loading && !dialog.state.creating && !!branchField.text.trim() && !!baseChoice.currentValue
                onClicked: dialog.backend.createWorktreeChat(branchField.text, baseChoice.currentValue)
            }
        }
    }
}
