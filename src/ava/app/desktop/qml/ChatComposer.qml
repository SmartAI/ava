pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: composer
    objectName: "chatComposerArea"
    function focusInput() { input.forceActiveFocus(); }
    function focusInputAtEnd() { input.forceActiveFocus(); input.cursorPosition = input.length; }
    required property var backend
    signal attach()
    signal models()
    property bool slashDismissed: false
    readonly property bool slashOpen: !slashDismissed && input.activeFocus && backend.commandCandidates.length > 0
    spacing: 8

    Connections {
        target: composer.backend
        function onDraftChanged() { composer.slashDismissed = false; commands.currentIndex = 0 }
    }
    Label {
        Layout.fillWidth: true
        visible: !!composer.backend.pendingText
        text: "Queued  ·  " + composer.backend.pendingText
        maximumLineCount: 3
        elide: Text.ElideRight
        wrapMode: Text.Wrap
        color: palette.placeholderText
        font.pixelSize: 12
    }
    Surface {
        id: card
        objectName: "composerCard"
        Layout.fillWidth: true
        implicitHeight: content.implicitHeight + 24
        elevation: 1
        focused: drop.containsDrag || input.activeFocus
        radius: Theme.dialogRadius
        ColumnLayout {
            id: content
            anchors.fill: parent
            anchors.margins: 12
            spacing: 8
            Rectangle {
                Layout.fillWidth: true
                implicitHeight: Math.min(270, commands.contentHeight) + 10
                visible: composer.slashOpen
                color: input.palette.window
                radius: 12
                border.color: input.palette.mid
                ListView {
                    id: commands
                    objectName: "commandList"
                    anchors.fill: parent
                    anchors.margins: 5
                    clip: true
                    model: composer.backend.commandCandidates
                    currentIndex: 0
                    ScrollBar.vertical: ScrollBar {}
                    delegate: ItemDelegate {
                        id: command
                        required property var modelData
                        required property int index
                        objectName: "command_" + modelData.name
                        width: ListView.view.width
                        height: 50
                        highlighted: commands.currentIndex === index
                        background: Rectangle { radius: 8; color: command.highlighted || command.hovered ? input.palette.alternateBase : "transparent" }
                        contentItem: ColumnLayout {
                            spacing: 2
                            Label { text: (command.modelData.kind === "skill" ? "$" : "/") + command.modelData.name; font.pixelSize: 13; font.weight: Font.Medium }
                            Label { Layout.fillWidth: true; text: command.modelData.description; font.pixelSize: 11; color: palette.placeholderText; elide: Text.ElideRight }
                        }
                        onClicked: {
                            composer.backend.chooseCommand(modelData.name, modelData.kind)
                            input.forceActiveFocus()
                            input.cursorPosition = input.length
                        }
                    }
                }
            }
            ScrollView {
                Layout.fillWidth: true
                Layout.preferredHeight: 58
                visible: composer.backend.attachments.length > 0
                clip: true
                contentWidth: chips.implicitWidth
                Row {
                    id: chips
                    spacing: 8
                    Repeater {
                        model: composer.backend.attachments
                        delegate: Rectangle {
                            id: chip
                            required property var modelData
                            objectName: "attachment_" + modelData.name
                            width: Math.min(245, chipRow.implicitWidth + 18)
                            height: 52
                            radius: 12
                            color: input.palette.window
                            border.color: input.palette.mid
                            RowLayout {
                                id: chipRow
                                anchors.fill: parent
                                anchors.margins: 8
                                Image { source: chip.modelData.preview || "icons/file.svg"; Layout.preferredWidth: 32; Layout.preferredHeight: 32; fillMode: Image.PreserveAspectFit; sourceSize.width: 64; sourceSize.height: 64 }
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 2
                                    Label { Layout.fillWidth: true; text: chip.modelData.name; elide: Text.ElideMiddle; font.pixelSize: 12 }
                                    Label { text: Math.max(1, Math.round(chip.modelData.size / 1024)) + " KB"; font.pixelSize: 10; color: palette.placeholderText }
                                }
                                NativeButton { objectName: "remove_" + chip.modelData.name; icon.source: "icons/close.svg"; quiet: true; tip: "Remove attachment"; onClicked: composer.backend.removeAttachment(chip.modelData.id) }
                            }
                        }
                    }
                }
            }
            ScrollView {
                Layout.fillWidth: true
                Layout.preferredHeight: Math.min(150, Math.max(42, input.implicitHeight))
                clip: true
                TextArea {
                    id: input
                    objectName: "composer"
                    text: composer.backend.draft
                    placeholderText: !composer.backend.chatId ? "Create a conversation to begin"
                                     : composer.backend.status === "running" ? "Steer Ava, or queue a follow-up…" : "Ask Ava anything, / for commands…"
                    enabled: !!composer.backend.chatId
                    wrapMode: TextEdit.Wrap
                    selectByMouse: true
                    ContextMenu.menu: TextMenu {
                        editor: input
                        canPaste: input.canPaste || composer.backend.clipboardHasAttachments
                        pasteHandler: () => composer.backend.pasteAttachments()
                    }
                    font.pixelSize: 15
                    padding: 5
                    background: null
                    Accessible.name: "Message Ava"
                    onTextChanged: { if (composer.backend.draft !== text) composer.backend.draft = text }
                    Keys.onPressed: function(event) {
                        if (input.inputMethodComposing) {
                            if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) event.accepted = true
                            return
                        }
                        if (event.matches(StandardKey.Paste) && composer.backend.pasteAttachments()) { event.accepted = true; return }
                        if (composer.slashOpen) {
                            if (event.key === Qt.Key_Down || event.key === Qt.Key_Up) {
                                commands.currentIndex = (commands.currentIndex + (event.key === Qt.Key_Down ? 1 : commands.count - 1)) % commands.count
                                commands.positionViewAtIndex(commands.currentIndex, ListView.Contain)
                                event.accepted = true
                                return
                            }
                            if (event.key === Qt.Key_Escape) { composer.slashDismissed = true; event.accepted = true; return }
                            if (event.key === Qt.Key_Tab || event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                                const candidate = composer.backend.commandCandidates[commands.currentIndex]
                                composer.backend.chooseCommand(candidate.name, candidate.kind)
                                if (event.key !== Qt.Key_Tab && candidate.kind === "command") composer.backend.submit(false)
                                input.cursorPosition = input.length
                                event.accepted = true
                                return
                            }
                        }
                        if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter) && !(event.modifiers & Qt.ShiftModifier)) {
                            event.accepted = true
                            composer.backend.submit(!!(event.modifiers & Qt.AltModifier))
                        } else if (event.key === Qt.Key_Escape && ["running", "pausing", "paused"].indexOf(composer.backend.status) >= 0) {
                            event.accepted = true
                            composer.backend.control(composer.backend.status === "running" ? "pause" : "abort")
                        }
                    }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: 6
                NativeButton { objectName: "attachButton"; icon.source: "icons/plus.svg"; quiet: true; tip: "Attach files or images"; enabled: !!composer.backend.chatId; onClicked: composer.attach() }
                NativeButton { objectName: "commandsButton"; icon.source: "icons/command.svg"; quiet: true; tip: "Commands and skills"; enabled: !!composer.backend.chatId; onClicked: { composer.backend.draft = "/"; input.forceActiveFocus(); input.cursorPosition = 1 } }
                Item { Layout.fillWidth: true }
                NativeButton {
                    objectName: "modelButton"
                    text: (composer.backend.selection.model || "Choose model") + (composer.backend.selection.effort ? " · " + composer.backend.selection.effort : "") + "  ⌄"
                    quiet: true
                    Layout.minimumWidth: 0
                    Layout.maximumWidth: Math.max(0, Math.min(280, content.width - 126))
                    enabled: composer.backend.connected
                    tip: "Model and reasoning effort"
                    onClicked: composer.models()
                }
                NativeButton {
                    objectName: "sendButton"
                    icon.source: "icons/arrow.svg"
                    primary: true
                    implicitWidth: 34
                    tip: composer.backend.status === "paused" ? "Resume" : composer.backend.status === "running" ? "Send steering message" : "Send message"
                    enabled: composer.backend.connected && !composer.backend.busy && composer.backend.status !== "aborting"
                             && (!!input.text.trim() || composer.backend.attachments.length > 0 || composer.backend.status === "paused")
                    onClicked: composer.backend.send()
                }
            }
        }
        DropArea {
            id: drop
            anchors.fill: parent
            enabled: !!composer.backend.chatId
            onDropped: function(event) {
                if (event.hasUrls) {
                    composer.backend.addAttachments(event.urls.map(url => url.toString()))
                    event.acceptProposedAction()
                }
            }
        }
    }
    Label {
        Layout.alignment: Qt.AlignHCenter
        text: ["running", "paused", "pausing"].indexOf(composer.backend.status) >= 0
              ? "Enter to steer · Alt+Enter to queue · Esc to pause or stop"
              : "Enter to send · Shift+Enter for a new line"
        color: palette.placeholderText
        font.pixelSize: 10
    }
}
