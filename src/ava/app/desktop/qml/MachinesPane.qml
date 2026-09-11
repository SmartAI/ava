pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Flickable {
    id: pane
    required property var backend
    signal machineOpened()
    contentWidth: width
    contentHeight: body.implicitHeight + 20
    clip: true
    boundsBehavior: Flickable.StopAtBounds
    ScrollBar.vertical: ScrollBar {}
    FontMetrics {
        id: actionMetrics
        font.pixelSize: Theme.body
        font.weight: Font.Medium
    }
    readonly property real restartWidth: Math.ceil(actionMetrics.advanceWidth("Restarting…")) + Theme.iconSize + Theme.spaceSm + Theme.spaceMd * 2
    readonly property real openWidth: Math.ceil(actionMetrics.advanceWidth("Connecting…")) + Theme.spaceMd * 2
    ColumnLayout {
        id: body
        width: parent.width - 12
        spacing: 14
        Label {
            text: "Machines"
            font.pixelSize: Theme.sectionTitle
            font.weight: Font.DemiBold
        }
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
            Layout.preferredHeight: Math.min(250, Math.max(80, contentHeight))
            clip: true
            spacing: 8
            model: pane.backend.machines
            ScrollBar.vertical: ScrollBar {}
            delegate: Pane {
                id: machine
                required property var modelData
                width: ListView.view.width
                implicitHeight: machineBody.implicitHeight + 24
                padding: 12
                background: Surface {
                    color: machine.modelData.active ? Theme.selection : Theme.inset
                    border.width: 0
                    radius: Theme.controlRadius
                }
                ColumnLayout {
                    id: machineBody
                    width: parent.width
                    spacing: 6
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spaceSm
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
                            quiet: true
                            Layout.preferredWidth: pane.restartWidth
                            enabled: !machine.modelData.busy && !pane.backend.serviceState.loading
                            tip: "Restart backend; active tasks must be stopped first"
                            onClicked: pane.backend.restartMachine(machine.modelData.id)
                        }
                        NativeButton {
                            objectName: "selectMachine_" + machine.modelData.id
                            text: machine.modelData.online ? "Open" : machine.modelData.busy ? "Connecting…" : "Reconnect"
                            primary: machine.modelData.online
                            tip: machine.modelData.online ? "Open projects on " + machine.modelData.name : "Reconnect to " + machine.modelData.name
                            enabled: !machine.modelData.busy
                            Layout.preferredWidth: pane.openWidth
                            onClicked: {
                                if (machine.modelData.online) {
                                    pane.backend.selectMachine(machine.modelData.id);
                                    pane.machineOpened();
                                } else pane.backend.reconnectMachine(machine.modelData.id);
                            }
                        }
                        // Reserve the same action column for local and SSH machines.
                        Item {
                            Layout.preferredWidth: Theme.controlHeight
                            Layout.preferredHeight: Theme.controlHeight
                            NativeButton {
                                anchors.fill: parent
                                objectName: "removeMachine_" + machine.modelData.id
                                visible: !!machine.modelData.host
                                enabled: !machine.modelData.restarting
                                icon.source: "icons/close.svg"
                                quiet: true
                                tip: "Remove connection; remote tasks keep running"
                                onClicked: pane.backend.removeMachine(machine.modelData.id)
                            }
                        }
                    }
                    Label {
                        Layout.fillWidth: true
                        text: (machine.modelData.host ? "SSH · " + machine.modelData.host : "Local") + " · " + machine.modelData.status
                        font.pixelSize: 11
                        color: palette.placeholderText
                        elide: Text.ElideMiddle
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        visible: !!machine.modelData.update_pending
                        spacing: Theme.spaceSm
                        Label {
                            Layout.fillWidth: true
                            text: "Connected to the previous backend. Update when your tasks are finished."
                            textFormat: Text.PlainText
                            wrapMode: Text.WordWrap
                            font.pixelSize: Theme.captionSmall
                            color: palette.placeholderText
                        }
                        NativeButton {
                            objectName: "updateMachine_" + machine.modelData.id
                            text: "Update backend"
                            enabled: machine.modelData.online && !machine.modelData.busy
                            tip: "Apply the update if no tasks are active"
                            onClicked: pane.backend.updateMachine(machine.modelData.id)
                        }
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
                id: add
                objectName: "addMachineAction"
                text: "Connect"
                primary: true
                enabled: /^[A-Za-z0-9_][A-Za-z0-9_.@:\[\]-]*$/.test(host.text.trim())
                onClicked: {
                    pane.forceActiveFocus();
                    pane.backend.addMachine(host.text, name.text);
                    host.text = "";
                    name.text = "";
                }
            }
        }
    }
}
