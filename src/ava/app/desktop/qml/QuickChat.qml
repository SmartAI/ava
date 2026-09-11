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
    minimumWidth: Math.min(520, Screen.desktopAvailableWidth)
    minimumHeight: Math.min(440, Screen.desktopAvailableHeight)
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
    // The card is inset from the transparent window bounds. Put resize targets
    // on its visible edges; manual resizing also works on macOS, where Qt's
    // startSystemResize is not supported.
    Repeater {
        model: [Qt.LeftEdge, Qt.RightEdge, Qt.TopEdge, Qt.BottomEdge,
                Qt.LeftEdge | Qt.TopEdge, Qt.RightEdge | Qt.TopEdge,
                Qt.LeftEdge | Qt.BottomEdge, Qt.RightEdge | Qt.BottomEdge]
        MouseArea {
            required property int modelData
            readonly property bool resizeLeft: (modelData & Qt.LeftEdge) !== 0
            readonly property bool resizeRight: (modelData & Qt.RightEdge) !== 0
            readonly property bool resizeTop: (modelData & Qt.TopEdge) !== 0
            readonly property bool resizeBottom: (modelData & Qt.BottomEdge) !== 0
            readonly property bool corner: (resizeLeft || resizeRight) && (resizeTop || resizeBottom)
            x: resizeLeft ? surface.x - 6 : resizeRight ? surface.x + surface.width - 6 : surface.x + 6
            y: resizeTop ? surface.y - 6 : resizeBottom ? surface.y + surface.height - 6 : surface.y + 6
            width: resizeLeft || resizeRight ? 12 : surface.width - 12
            height: resizeTop || resizeBottom ? 12 : surface.height - 12
            z: 10
            cursorShape: corner ? (resizeLeft === resizeTop ? Qt.SizeFDiagCursor : Qt.SizeBDiagCursor)
                                : (resizeLeft || resizeRight ? Qt.SizeHorCursor : Qt.SizeVerCursor)
            acceptedButtons: Qt.LeftButton
            property point origin
            property rect initial
            onPressed: function(mouse) {
                origin = mapToGlobal(mouse.x, mouse.y);
                initial = Qt.rect(panel.x, panel.y, panel.width, panel.height);
            }
            onPositionChanged: function(mouse) {
                if (!pressed) return;
                const point = mapToGlobal(mouse.x, mouse.y);
                const dx = point.x - origin.x;
                const dy = point.y - origin.y;
                const w = Math.max(panel.minimumWidth, initial.width + (resizeLeft ? -dx : resizeRight ? dx : 0));
                const h = Math.max(panel.minimumHeight, initial.height + (resizeTop ? -dy : resizeBottom ? dy : 0));
                if (resizeLeft) panel.x = initial.x + initial.width - w;
                if (resizeTop) panel.y = initial.y + initial.height - h;
                panel.width = w;
                panel.height = h;
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
        id: surface
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
