pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

NativeDialog {
    id: dialog
    required property var backend
    title: "Machines"
    modal: true
    focus: true
    width: Math.min(640, parent.width - 40)
    onOpened: host.forceActiveFocus()
    ColumnLayout {
        width: parent.width
        spacing: 14
        Label {
            Layout.fillWidth: true
            text: "Projects and chats stay on the machine where they run."
            wrapMode: Text.WordWrap
            color: palette.placeholderText
            font.pixelSize: 12
        }
        ListView {
            id: machines
            objectName: "machineConnectionsList"
            Layout.fillWidth: true
            Layout.preferredHeight: Math.min(250, dialog.parent.height * 0.3, Math.max(80, contentHeight))
            clip: true
            spacing: 8
            model: dialog.backend.machines
            ScrollBar.vertical: ScrollBar {}
            delegate: Pane {
                id: machine
                required property var modelData
                width: ListView.view.width
                implicitHeight: machineBody.implicitHeight + 24
                padding: 12
                background: Surface {
                    color: machine.modelData.active ? Theme.selection : Theme.inset
                    border.color: machine.modelData.active ? Theme.accent : Theme.border
                }
                ColumnLayout {
                    id: machineBody
                    width: parent.width
                    spacing: 6
                    RowLayout {
                        Layout.fillWidth: true
                        Rectangle {
                            implicitWidth: 6
                            implicitHeight: 6
                            radius: 3
                            objectName: "backendStatus_" + machine.modelData.id
                            color: machine.modelData.state === "online" ? Theme.success : machine.modelData.state === "offline" ? Theme.danger : Theme.warning
                        }
                        Label {
                            Layout.fillWidth: true
                            text: machine.modelData.name
                            font.pixelSize: 13
                            font.weight: Font.Medium
                            elide: Text.ElideRight
                        }
                        NativeButton {
                            objectName: "restartMachine_" + machine.modelData.id
                            icon.source: "icons/reload.svg"
                            text: machine.modelData.restarting ? "Restarting…" : "Restart"
                            implicitHeight: 30
                            enabled: !machine.modelData.busy
                            tip: "Restart backend; active tasks must be stopped first"
                            onClicked: dialog.backend.restartMachine(machine.modelData.id)
                        }
                        NativeButton {
                            objectName: "updateMachine_" + machine.modelData.id
                            text: "Update backend"
                            visible: !!machine.modelData.update_pending
                            enabled: machine.modelData.online && !machine.modelData.busy
                            implicitHeight: 30
                            tip: "Apply the update if no tasks are active"
                            onClicked: dialog.backend.updateMachine(machine.modelData.id)
                        }
                        NativeButton {
                            objectName: "selectMachine_" + machine.modelData.id
                            text: machine.modelData.online ? "Open" : machine.modelData.busy ? "Connecting…" : "Reconnect"
                            enabled: !machine.modelData.busy
                            implicitHeight: 30
                            onClicked: {
                                if (machine.modelData.online) {
                                    dialog.backend.selectMachine(machine.modelData.id);
                                    dialog.close();
                                } else dialog.backend.reconnectMachine(machine.modelData.id);
                            }
                        }
                        NativeButton {
                            objectName: "removeMachine_" + machine.modelData.id
                            visible: !!machine.modelData.host
                            enabled: !machine.modelData.restarting
                            icon.source: "icons/close.svg"
                            quiet: true
                            tip: "Remove connection; remote tasks keep running"
                            implicitHeight: 30
                            implicitWidth: 30
                            onClicked: dialog.backend.removeMachine(machine.modelData.id)
                        }
                    }
                    Label {
                        Layout.fillWidth: true
                        text: (machine.modelData.host ? "SSH · " + machine.modelData.host : "Local") + " · " + machine.modelData.status
                        font.pixelSize: 11
                        color: palette.placeholderText
                        elide: Text.ElideMiddle
                    }
                    Label {
                        Layout.fillWidth: true
                        visible: !!machine.modelData.update_pending
                        text: "Connected to the previous backend. Update when your tasks are finished."
                        textFormat: Text.PlainText
                        wrapMode: Text.WordWrap
                        font.pixelSize: 11
                        color: palette.placeholderText
                    }
                    Label {
                        Layout.fillWidth: true
                        visible: !!machine.modelData.error
                        text: machine.modelData.error
                        textFormat: Text.PlainText
                        wrapMode: Text.WrapAnywhere
                        font.pixelSize: 11
                    }
                }
            }
        }
        Label {
            text: "Add an SSH machine"
            font.pixelSize: 14
            font.weight: Font.DemiBold
        }
        NativeField {
            id: host
            objectName: "machineHostField"
            Layout.fillWidth: true
            placeholderText: "SSH alias or user@hostname"
            maximumLength: 255
            onAccepted: { if (add.enabled) add.clicked(); }
        }
        NativeField {
            id: name
            objectName: "machineNameField"
            Layout.fillWidth: true
            placeholderText: "Display name (optional)"
            maximumLength: 80
        }
        Label {
            Layout.fillWidth: true
            text: "Uses your SSH configuration; new hosts ask you to verify their fingerprint. Ava installs its backend in your remote user directory. Python 3.12+ and a user service manager are required."
            wrapMode: Text.WordWrap
            font.pixelSize: 11
            color: palette.placeholderText
        }
        RowLayout {
            Layout.fillWidth: true
            Item { Layout.fillWidth: true }
            NativeButton {
                text: "Done"
                onClicked: dialog.close()
            }
            NativeButton {
                id: add
                objectName: "addMachineAction"
                text: "Connect"
                primary: true
                enabled: /^[A-Za-z0-9_][A-Za-z0-9_.@:\[\]-]*$/.test(host.text.trim())
                onClicked: {
                    dialog.forceActiveFocus();
                    dialog.backend.addMachine(host.text, name.text);
                    host.text = "";
                    name.text = "";
                }
            }
        }
    }
}
