pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Pane {
    id: setup
    objectName: "sessionSetupFooter"
    required property var backend
    signal chooseFolder
    property bool editingRemotePath: false
    readonly property bool canChoose: backend.online && !backend.busy && (!backend.chatId || backend.canConfigureSession)
    padding: Theme.spaceXs
    horizontalPadding: Theme.spaceSm
    background: Rectangle {
        color: Theme.inset
        radius: Theme.cardRadius
    }
    contentItem: ColumnLayout {
        spacing: Theme.spaceXs
        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.spaceXs
            NativeCombo {
                id: projectChoice
                objectName: "sessionProjectChoice"
                property string tip: setup.backend.workspacePath
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                Layout.maximumWidth: 260
                implicitWidth: 200
                leftPadding: Theme.spaceSm + Theme.iconSize + Theme.spaceSm
                rightPadding: Theme.spaceXl
                font.pixelSize: Theme.caption
                model: setup.backend.sessionProjects
                textRole: "name"
                valueRole: "id"
                currentIndex: model.findIndex(project => project.id === setup.backend.projectId)
                enabled: setup.canChoose
                Accessible.name: "Project for this session"
                Accessible.description: tip
                background: Surface {
                    radius: Theme.controlRadius
                    border.width: 0
                    focused: projectChoice.visualFocus
                    color: projectChoice.down ? Theme.selection : projectChoice.hovered ? Theme.hover : "transparent"
                }
                Image {
                    x: Theme.spaceSm
                    anchors.verticalCenter: parent.verticalCenter
                    source: "icons/folder.svg"
                    sourceSize.width: Theme.iconSize
                    sourceSize.height: Theme.iconSize
                }
                NativeToolTip {
                    visible: projectChoice.hovered && !!projectChoice.tip
                    text: projectChoice.tip
                }
                popup.width: Math.max(projectChoice.width, Math.min(320, setup.width))
                delegate: ItemDelegate {
                    id: projectEntry
                    required property int index
                    required property var modelData
                    width: projectChoice.popup.width - 10
                    height: Theme.controlHeight
                    highlighted: projectChoice.highlightedIndex === index
                    contentItem: Text {
                        text: projectEntry.modelData.name
                        color: Theme.text
                        font.pixelSize: Theme.body
                        verticalAlignment: Text.AlignVCenter
                        elide: Text.ElideRight
                    }
                    background: Rectangle {
                        radius: Theme.controlRadius
                        color: projectEntry.highlighted ? Theme.selection : "transparent"
                    }
                }
                onActivated: {
                    if (setup.backend.chatId)
                        setup.backend.changeSessionProject(currentValue);
                    else
                        setup.backend.newChat(currentValue);
                }
            }
            NativeButton {
                id: chooseFolderButton
                objectName: "chooseChatFolderButton"
                icon.source: "icons/plus.svg"
                quiet: true
                tip: "Choose another project folder"
                visible: !setup.backend.chatId || setup.backend.canConfigureSession
                enabled: setup.canChoose
                onClicked: {
                    if (setup.backend.remoteMachine) {
                        setup.editingRemotePath = !setup.editingRemotePath;
                        if (setup.editingRemotePath) remotePath.forceActiveFocus();
                    } else setup.chooseFolder();
                }
            }
            NativeButton {
                objectName: "sessionWorktreeCheck"
                text: checked ? "Worktree ✓" : "Worktree"
                icon.source: "icons/branch.svg"
                quiet: true
                checkable: true
                font.pixelSize: Theme.caption
                visible: !!setup.backend.chatId
                enabled: setup.backend.canConfigureSession
                checked: setup.backend.sessionWorktree
                tip: setup.backend.workspaceBranch
                    ? "Worktree · " + setup.backend.workspaceBranch + "\n" + setup.backend.workspacePath
                    : checked
                        ? "A new Git worktree will be created on first send. Uncommitted changes stay in the project folder."
                        : "Work in the project folder. Turn on to create a separate Git worktree on first send."
                Accessible.name: "Use a Git worktree"
                onClicked: setup.backend.setSessionWorktree(checked)
            }
            Item { Layout.fillWidth: true }
        }
        RowLayout {
            Layout.fillWidth: true
            visible: setup.editingRemotePath && setup.backend.remoteMachine && setup.canChoose
            spacing: Theme.spaceSm
            NativeField {
                id: remotePath
                objectName: "sessionRemotePath"
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                placeholderText: "Remote project folder, e.g. ~/projects/ava"
                Accessible.name: "Remote project folder"
                onAccepted: { if (useFolder.enabled) useFolder.clicked(); }
                Keys.onEscapePressed: {
                    setup.editingRemotePath = false;
                    chooseFolderButton.forceActiveFocus();
                }
            }
            NativeButton {
                id: useFolder
                objectName: "useSessionRemoteFolder"
                text: "Use folder"
                enabled: !!remotePath.text.trim()
                onClicked: {
                    setup.backend.newChatInFolder(remotePath.text.trim());
                    setup.editingRemotePath = false;
                }
            }
        }
    }
}
