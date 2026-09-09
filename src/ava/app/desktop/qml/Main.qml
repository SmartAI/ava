pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs

ApplicationWindow {
    id: window
    objectName: "desktopWindow"
    required property var backend
    width: 1160
    height: 780
    minimumWidth: 800
    minimumHeight: 560
    visible: true
    title: window.backend.chatId ? window.backend.chatTitle + " · Ava" : "Ava"
    color: "#fcfcfa"
    font.pixelSize: 14
    palette.highlight: "#386a59"
    palette.highlightedText: "white"
    palette.text: "#252a27"
    palette.buttonText: "#252a27"
    palette.windowText: "#252a27"
    palette.base: "#ffffff"
    palette.button: "#eeefeb"
    property bool closing: false

    onClosing: function(close) {
        close.accepted = false
        if (!closing) {
            closing = true
            window.backend.shutdown()
        }
    }

    Shortcut { sequences: [StandardKey.New]; enabled: window.backend.online && !window.backend.busy; onActivated: window.backend.newChat() }
    Shortcut { sequences: [StandardKey.Quit]; onActivated: window.close() }

    FolderDialog {
        id: folderDialog
        title: "Choose a project folder"
        onAccepted: window.backend.addProject(selectedFolder.toString())
    }

    RowLayout {
        anchors.fill: parent
        spacing: 0
        enabled: !window.closing

        Rectangle {
            Layout.preferredWidth: 250
            Layout.fillHeight: true
            color: "#f0f1ed"
            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 18
                spacing: 14
                RowLayout {
                    Layout.fillWidth: true
                    Label { text: "ava"; font.pixelSize: 30; font.weight: Font.DemiBold; color: "#315a4b" }
                    Item { Layout.fillWidth: true }
                    ToolButton {
                        text: "+"
                        objectName: "addProjectButton"
                        font.pixelSize: 24
                        enabled: window.backend.online
                        Accessible.name: "Add project"
                        ToolTip.visible: hovered
                        ToolTip.text: "Add project folder"
                        onClicked: folderDialog.open()
                    }
                }
                Label { text: "WORKSPACE"; font.pixelSize: 10; font.letterSpacing: 1.5; color: "#70796f" }
                ComboBox {
                    id: projects
                    objectName: "projectPicker"
                    Layout.fillWidth: true
                    model: window.backend.projects
                    textRole: "name"
                    valueRole: "id"
                    displayText: window.backend.projectName
                    enabled: window.backend.online
                    Accessible.name: "Project"
                    onActivated: window.backend.selectProject(currentValue)
                    ToolTip.visible: hovered
                    ToolTip.text: window.backend.projectPath
                }
                Button {
                    objectName: "newChatButton"
                    Layout.fillWidth: true
                    text: "+  New conversation"
                    enabled: window.backend.online && !!window.backend.projectId && !window.backend.busy
                    onClicked: window.backend.newChat()
                }
                Label { text: "CONVERSATIONS"; font.pixelSize: 10; font.letterSpacing: 1.5; color: "#70796f" }
                ListView {
                    id: chats
                    objectName: "chatList"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    spacing: 5
                    model: window.backend.chats
                    ScrollBar.vertical: ScrollBar {}
                    delegate: ItemDelegate {
                        id: chatItem
                        required property var modelData
                        width: ListView.view.width
                        text: chatItem.modelData.title || "New conversation"
                        highlighted: chatItem.modelData.id === window.backend.chatId
                        enabled: window.backend.online
                        Accessible.name: text
                        contentItem: Label {
                            text: chatItem.text
                            elide: Text.ElideRight
                            verticalAlignment: Text.AlignVCenter
                            color: chatItem.highlighted ? "#234b3e" : "#4c544d"
                        }
                        background: Rectangle {
                            radius: 7
                            color: chatItem.highlighted ? "#dce6dd" : chatItem.hovered ? "#e7e9e3" : "transparent"
                        }
                        onClicked: window.backend.openChat(chatItem.modelData.id)
                    }
                    Label {
                        anchors.top: parent.top
                        width: parent.width
                        visible: chats.count === 0
                        text: "Your conversations will appear here."
                        wrapMode: Text.WordWrap
                        color: "#7a8278"
                        font.pixelSize: 12
                    }
                }
                Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: "#dfe2da" }
                Label {
                    Layout.fillWidth: true
                    text: window.backend.connectionLabel
                    wrapMode: Text.WordWrap
                    font.pixelSize: 11
                    color: window.backend.online ? "#496754" : "#8a5940"
                    Accessible.name: text
                }
                Button {
                    text: "Restart Ava"
                    visible: !window.backend.online && window.backend.connectionLabel !== "Starting Ava…"
                    onClicked: window.backend.start()
                }
            }
        }
        Rectangle { Layout.fillHeight: true; Layout.preferredWidth: 1; color: "#e4e6df" }
        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 0
            Pane {
                Layout.fillWidth: true
                padding: 22
                background: Rectangle { color: "#fcfcfa" }
                RowLayout {
                    width: parent.width
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 4
                        Label {
                            Layout.fillWidth: true
                            text: window.backend.chatId ? window.backend.chatTitle : window.backend.projectName
                            font.pixelSize: 17
                            font.weight: Font.DemiBold
                            elide: Text.ElideRight
                        }
                        Label {
                            Layout.fillWidth: true
                            text: window.backend.modelName || window.backend.projectPath
                            elide: Text.ElideMiddle
                            color: "#778075"
                            font.pixelSize: 11
                        }
                    }
                    Label {
                        objectName: "runStatus"
                        text: window.backend.chatId ? (window.backend.connected ? window.backend.status : "connecting…") : ""
                        color: "#386a59"
                        font.pixelSize: 12
                    }
                    Button {
                        text: window.backend.status === "paused" ? "Resume" : "Pause"
                        visible: ["running", "paused"].indexOf(window.backend.status) >= 0
                        enabled: window.backend.connected && !window.backend.busy
                        onClicked: window.backend.control(window.backend.status === "paused" ? "resume" : "pause")
                    }
                    Button {
                        objectName: "stopButton"
                        text: "Stop"
                        visible: ["running", "pausing", "paused", "aborting"].indexOf(window.backend.status) >= 0
                        enabled: window.backend.connected && !window.backend.busy && window.backend.status !== "aborting"
                        onClicked: window.backend.control("abort")
                    }
                }
            }
            Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: "#eaede5" }
            Rectangle {
                Layout.fillWidth: true
                implicitHeight: errorLayout.implicitHeight + 24
                visible: !!window.backend.error
                color: "#fbede6"
                RowLayout {
                    id: errorLayout
                    anchors.fill: parent
                    anchors.margins: 12
                    Label { Layout.fillWidth: true; text: window.backend.error; wrapMode: Text.Wrap; color: "#8b3e2d" }
                    ToolButton { text: "×"; Accessible.name: "Dismiss error"; onClicked: window.backend.dismissError() }
                }
            }
            Item {
                Layout.fillWidth: true
                Layout.fillHeight: true
                ListView {
                    id: conversation
                    objectName: "transcriptView"
                    anchors.fill: parent
                    anchors.margins: 22
                    spacing: 22
                    clip: true
                    reuseItems: true
                    model: window.backend.transcript
                    property bool follow: true
                    onMovementEnded: follow = atYEnd
                    onContentHeightChanged: { if (follow) Qt.callLater(positionViewAtEnd) }
                    onCountChanged: { if (count === 0) follow = true }
                    ScrollBar.vertical: ScrollBar { onPressedChanged: { if (!pressed) conversation.follow = conversation.atYEnd } }
                    delegate: ColumnLayout {
                        id: message
                        required property string kind
                        required property string heading
                        required property string body
                        required property string detail
                        property bool expanded: false
                        property string fontFamily: window.font.family
                        readonly property bool compact: kind === "tool" || kind === "reasoning"
                        width: Math.min(820, ListView.view.width - 20)
                        x: (ListView.view.width - width) / 2
                        spacing: 6
                        ListView.onReused: expanded = false
                        RowLayout {
                            Layout.fillWidth: true
                            Label {
                                text: message.heading
                                color: message.kind === "error" ? "#a74c35" : message.kind === "user" ? "#747b71" : "#386a59"
                                font.pixelSize: 12
                                font.weight: Font.DemiBold
                            }
                            Item { Layout.fillWidth: true }
                            ToolButton {
                                text: message.expanded ? "Collapse" : "Expand"
                                visible: message.compact
                                font.pixelSize: 11
                                onClicked: message.expanded = !message.expanded
                            }
                        }
                        TextArea {
                            Layout.fillWidth: true
                            visible: message.expanded && !!message.detail
                            text: message.detail
                            readOnly: true
                            selectByMouse: true
                            wrapMode: TextEdit.WrapAnywhere
                            textFormat: TextEdit.PlainText
                            font.family: "monospace"
                            font.pixelSize: 12
                            color: "#657060"
                            background: null
                            padding: 0
                        }
                        TextArea {
                            Layout.fillWidth: true
                            text: message.compact && !message.expanded ? message.body.slice(0, 180) : message.body
                            readOnly: true
                            selectByMouse: true
                            wrapMode: TextEdit.Wrap
                            textFormat: TextEdit.PlainText
                            font.family: message.kind === "tool" ? "monospace" : message.fontFamily
                            font.pixelSize: message.kind === "tool" ? 12 : 15
                            color: message.kind === "error" ? "#a74c35" : "#30372f"
                            background: null
                            padding: 0
                            Accessible.name: message.heading + ": " + text
                        }
                    }
                }
                ColumnLayout {
                    anchors.centerIn: parent
                    width: Math.min(420, parent.width - 60)
                    visible: conversation.count === 0
                    spacing: 14
                    Label { Layout.alignment: Qt.AlignHCenter; text: "What shall we build?"; font.pixelSize: 28; color: "#354d3d" }
                    Label {
                        Layout.fillWidth: true
                        text: window.backend.chatId ? "Ask Ava about your project, or describe a change."
                                             : "Start a conversation in this project to work with Ava."
                        horizontalAlignment: Text.AlignHCenter
                        wrapMode: Text.WordWrap
                        color: "#7a8278"
                    }
                    Button {
                        Layout.alignment: Qt.AlignHCenter
                        text: "New conversation"
                        visible: !window.backend.chatId
                        enabled: window.backend.online && !!window.backend.projectId && !window.backend.busy
                        onClicked: window.backend.newChat()
                    }
                }
            }
            ColumnLayout {
                Layout.fillWidth: true
                Layout.leftMargin: 24
                Layout.rightMargin: 24
                Layout.bottomMargin: 18
                spacing: 8
                Label {
                    Layout.fillWidth: true
                    visible: !!window.backend.pendingText
                    text: "Queued: " + window.backend.pendingText
                    maximumLineCount: 3
                    elide: Text.ElideRight
                    wrapMode: Text.Wrap
                    color: "#6b775f"
                    font.pixelSize: 12
                }
                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: inputLayout.implicitHeight + 20
                    color: "#ffffff"
                    border.color: input.activeFocus ? "#88a58d" : "#dce1d7"
                    radius: 12
                    RowLayout {
                        id: inputLayout
                        anchors.fill: parent
                        anchors.margins: 10
                        spacing: 10
                        ScrollView {
                            Layout.fillWidth: true
                            Layout.preferredHeight: Math.min(140, Math.max(60, input.implicitHeight))
                            clip: true
                            TextArea {
                                id: input
                                objectName: "composer"
                                text: window.backend.draft
                                placeholderText: window.backend.chatId ? "Message Ava…" : "Create a conversation to begin"
                                enabled: !!window.backend.chatId
                                wrapMode: TextEdit.Wrap
                                selectByMouse: true
                                background: null
                                Accessible.name: "Message Ava"
                                onTextChanged: { if (window.backend.draft !== text) window.backend.draft = text }
                                Keys.onPressed: function(event) {
                                    if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter)
                                            && !(event.modifiers & Qt.ShiftModifier)) {
                                        event.accepted = true
                                        if (!input.inputMethodComposing) window.backend.send()
                                    }
                                }
                            }
                        }
                        Button {
                            objectName: "sendButton"
                            text: window.backend.status === "running" ? "Queue" : "Send"
                            Layout.alignment: Qt.AlignBottom
                            enabled: window.backend.connected && !window.backend.busy && !!input.text.trim()
                                     && ["paused", "pausing", "aborting"].indexOf(window.backend.status) < 0
                            onClicked: window.backend.send()
                        }
                    }
                }
                Label {
                    text: "Enter to send · Shift+Enter for a new line"
                    color: "#92988d"
                    font.pixelSize: 10
                }
            }
        }
    }
    Rectangle {
        anchors.fill: parent
        visible: window.closing
        color: "#e6fcfcfa"
        Label { anchors.centerIn: parent; text: "Saving and closing…"; font.pixelSize: 20 }
    }
}
