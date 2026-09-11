pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs

ApplicationWindow {
    id: panel
    objectName: "quickChatWindow"
    required property var backend
    required property string codeFont
    required property var appPalette
    palette: appPalette
    signal openMain()
    signal models()
    signal attach()
    width: 680
    height: 560
    visible: false
    title: "Quick Chat · Ava"
    color: "transparent"
    flags: Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
    property string sessionId: ""
    property bool starting: false
    property bool needsFocus: false
    function prepare() {
        needsFocus = true;
        if (!backend.online || backend.busy) return;
        if (sessionId && !backend.projects.some(project => project.chats.some(chat => chat.id === sessionId)))
            sessionId = "";
        if (sessionId) {
            if (backend.chatId !== sessionId) backend.openChat(sessionId);
        } else if (!starting) {
            starting = true;
            backend.newChat();
        }
        composer.focusInput();
    }
    onVisibleChanged: { if (visible) prepare(); }
    onActiveChanged: { if (active) composer.focusInput(); }
    onClosing: function(event) { event.accepted = false; hide(); }
    Connections {
        target: panel.backend
        function onChanged() {
            if (!panel.visible) return;
            if (panel.starting && !panel.backend.busy) {
                panel.starting = false;
                if (!panel.backend.error) panel.sessionId = panel.backend.chatId;
            }
            if (!panel.sessionId && !panel.starting && panel.backend.online && !panel.backend.busy && !panel.backend.error)
                panel.prepare();
            if (!panel.backend.busy && !panel.backend.error && panel.backend.chatId) {
                panel.sessionId = panel.backend.chatId;
                if (panel.needsFocus) {
                    panel.needsFocus = false;
                    composer.focusInput();
                }
            }
        }
    }
    Shortcut { sequence: "Escape"; onActivated: panel.hide() }
    FolderDialog {
        id: folder
        title: "Choose a project folder"
        onAccepted: panel.backend.newChatInFolder(selectedFolder.toString())
    }
    Surface {
        objectName: "quickChatSurface"
        elevation: 2
        anchors.fill: parent
        anchors.margins: 24
        radius: Theme.dialogRadius + 6
        color: Qt.rgba(Theme.surface.r, Theme.surface.g, Theme.surface.b, 0.94)
        border.color: Theme.border
        ColumnLayout {
            anchors.fill: parent
            anchors.margins: Theme.spaceXl
            spacing: Theme.spaceMd
            RowLayout {
                Layout.fillWidth: true
                Label { text: "Quick Chat"; color: Theme.text; font.pixelSize: Theme.sectionTitle; font.weight: Font.DemiBold }
                Item { Layout.fillWidth: true }
                NativeButton {
                    icon.source: "icons/plus.svg"; quiet: true; tip: "New quick chat"
                    enabled: panel.backend.online && !panel.backend.busy
                    onClicked: { panel.sessionId = ""; panel.prepare(); }
                }
                NativeButton {
                    text: "Open in Ava"; quiet: true
                    onClicked: { panel.hide(); panel.openMain(); }
                }
                NativeButton { icon.source: "icons/close.svg"; quiet: true; tip: "Dismiss · Esc"; onClicked: panel.hide() }
            }
            Label {
                Layout.fillWidth: true
                text: panel.backend.modelName || "Default model"
                color: Theme.secondaryText; font.pixelSize: Theme.caption
                elide: Text.ElideRight
            }
            ListView {
                id: messages
                objectName: "quickChatTranscript"
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                spacing: Theme.spaceSm
                model: panel.backend.transcript
                property bool following: true
                onMovementStarted: following = false
                onMovementEnded: following = atYEnd
                onContentHeightChanged: { if (following) Qt.callLater(positionViewAtEnd); }
                onCountChanged: { if (following) Qt.callLater(positionViewAtEnd); }
                ScrollBar.vertical: ScrollBar {}
                delegate: TranscriptMessage {
                    required property int index
                    width: messages.width
                    backend: panel.backend
                    codeFont: panel.codeFont
                }
                Label {
                    anchors.centerIn: parent
                    visible: messages.count === 0
                    text: panel.backend.online ? "What can I help you with?" : "Connecting to Ava…"
                    color: Theme.secondaryText
                    font.pixelSize: Theme.sectionTitle
                }
            }
            Label {
                Layout.fillWidth: true
                visible: !!panel.backend.error
                text: panel.backend.error
                color: Theme.danger
                wrapMode: Text.Wrap
            }
            ChatComposer {
                id: composer
                Layout.fillWidth: true
                backend: panel.backend
                onChooseFolder: folder.open()
                onModels: { panel.hide(); panel.openMain(); panel.models(); }
                onAttach: { panel.hide(); panel.openMain(); panel.attach(); }
            }
        }
    }
}
