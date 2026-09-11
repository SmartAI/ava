pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

NativeDialog {
    id: dialog
    required property var backend
    readonly property var state: backend.worktreeState
    readonly property bool newWorktree: worktreeChoice.checked
    readonly property string errorText: state.error || (newWorktree ? state.options_error || "" : "")
    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(520, parent.width - 40)
    modal: true
    title: "New conversation"
    closePolicy: state.creating ? Popup.NoAutoClose : Popup.CloseOnEscape
    Connections {
        target: dialog.backend
        function onWorktreeRequested() {
            worktreeChoice.checked = !!dialog.state.new_worktree;
            originalChoice.checked = !dialog.state.new_worktree;
            branchField.text = dialog.state.branch || "";
            dialog.open();
        }
        function onWorktreeCreated() { dialog.close(); }
    }
    onOpened: originalChoice.forceActiveFocus()
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
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 4
            RadioButton {
                id: originalChoice
                objectName: "originalDirectoryChoice"
                text: "Original directory"
                enabled: !dialog.state.creating
            }
            RadioButton {
                id: worktreeChoice
                objectName: "newWorktreeChoice"
                text: "New worktree"
                enabled: !dialog.state.creating
            }
        }
        Label {
            Layout.fillWidth: true
            text: dialog.newWorktree
                ? "A random branch and separate folder are generated automatically using git worktree add. Chats stay grouped under this project."
                : "Work directly in the selected directory, including its uncommitted changes. No worktree is created."
            wrapMode: Text.WordWrap
            font.pixelSize: 13
        }
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 6
            visible: dialog.newWorktree
            Label { text: "Generated branch"; font.pixelSize: 12 }
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
            visible: dialog.newWorktree
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
            visible: dialog.newWorktree
            text: "Uncommitted changes stay in the original folder. The worktree is kept when you archive its chat."
            wrapMode: Text.WordWrap
            font.pixelSize: 12
            color: palette.placeholderText
        }
        Label {
            Layout.fillWidth: true
            visible: dialog.newWorktree && !!dialog.state.directory
            text: "Location on this machine: " + (dialog.state.directory || "")
            wrapMode: Text.WrapAnywhere
            font.pixelSize: 11
            color: palette.placeholderText
        }
        Label {
            objectName: "worktreeError"
            Layout.fillWidth: true
            visible: !!dialog.errorText || (dialog.newWorktree && !!dialog.state.loading)
            text: dialog.newWorktree && dialog.state.loading ? "Loading branches…" : dialog.errorText
            wrapMode: Text.WrapAnywhere
            font.pixelSize: 12
            color: dialog.errorText ? Theme.danger : palette.placeholderText
        }
        RowLayout {
            Layout.fillWidth: true
            NativeButton {
                text: "Reload branches"
                visible: dialog.newWorktree && !!dialog.state.options_error && !dialog.state.loading
                onClicked: dialog.backend.prepareWorktree(dialog.state.project)
            }
            Item { Layout.fillWidth: true }
            NativeButton {
                objectName: "cancelNewChatButton"
                text: "Cancel"
                enabled: !dialog.state.creating
                onClicked: dialog.close()
            }
            NativeButton {
                id: createButton
                objectName: dialog.newWorktree ? "createWorktreeChatButton" : "createOriginalChatButton"
                text: dialog.state.creating ? "Creating…" : "Create chat"
                primary: true
                enabled: !dialog.state.creating && (!dialog.newWorktree || (!dialog.state.loading && !!branchField.text.trim() && !!baseChoice.currentValue))
                onClicked: {
                    if (dialog.newWorktree)
                        dialog.backend.createWorktreeChat(branchField.text, baseChoice.currentValue);
                    else
                        dialog.backend.createOriginalChat();
                }
            }
        }
    }
}
