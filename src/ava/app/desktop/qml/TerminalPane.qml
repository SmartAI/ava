pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtWebEngine
import QtWebChannel

Pane {
    id: pane
    objectName: "terminalPane"
    required property var backend
    required property string rootPath
    property string projectId: ""
    required property string codeFont
    property var session: null
    property bool loaded: false
    readonly property bool dark: Theme.dark
    readonly property var terminalColors: ({background: Theme.workspace.toString(), foreground: Theme.text.toString(), cursor: Theme.text.toString(), selectionBackground: Theme.selection.toString()})
    property var evaluation: null
    signal evaluated
    padding: 0
    background: null
    function evaluate(script: string) {
        browser.runJavaScript(script, function (value) {
            pane.evaluation = value;
            pane.evaluated();
        });
    }
    function appearance() {
        if (loaded)
            browser.runJavaScript("window.avaTerminal.setAppearance(" + dark + "," + JSON.stringify(codeFont) + "," + JSON.stringify(terminalColors) + "," + Theme.reducedMotion + ")");
    }
    function focusTerminal() {
        browser.forceActiveFocus();
        if (loaded)
            browser.runJavaScript("window.avaTerminal.focus()");
    }
    function copySelection() {
        if (loaded)
            browser.runJavaScript("window.avaTerminal.copy()");
    }
    function pasteClipboard() {
        if (loaded)
            browser.runJavaScript("window.avaTerminal.paste()");
        focusTerminal();
    }
    function clearScreen() {
        if (loaded)
            browser.runJavaScript("window.avaTerminal.clear()");
    }
    function selectAll() {
        if (loaded) browser.runJavaScript("window.avaTerminal.selectAll()");
    }
    Component.onCompleted: {
        session = backend.createTerminal(rootPath, projectId);
        channel.registerObject("terminal", session);
        browser.profile = terminalProfile.instance();
        browser.url = Qt.resolvedUrl("../terminal/index.html");
    }
    Component.onDestruction: {
        if (session)
            session.close();
    }
    onTerminalColorsChanged: appearance()
    Connections { target: Theme; function onReducedMotionChanged() { pane.appearance(); } }
    onCodeFontChanged: appearance()
    onVisibleChanged: {
        if (visible)
            Qt.callLater(focusTerminal);
    }
    WebChannel {
        id: channel
    }
    WebEngineProfilePrototype {
        id: terminalProfile
    }
    ColumnLayout {
        anchors.fill: parent
        spacing: 0
        WebEngineView {
            id: browser
            objectName: "terminalWeb"
            Layout.fillWidth: true
            Layout.fillHeight: true
            backgroundColor: pane.palette.window
            webChannel: channel
            settings.localContentCanAccessRemoteUrls: false
            settings.localContentCanAccessFileUrls: true
            settings.javascriptCanOpenWindows: false
            settings.javascriptCanAccessClipboard: false
            onNavigationRequested: function (request) {
                if (request.url.toString() !== Qt.resolvedUrl("../terminal/index.html").toString())
                    request.action = WebEngineNavigationRequest.IgnoreRequest;
            }
            onContextMenuRequested: function (request) {
                request.accepted = true;
                const point = request.position;
                browser.runJavaScript("window.avaTerminal.hasSelection()", function(selected) {
                    terminalMenu.hasSelection = !!selected;
                    terminalMenu.popup(browser, point);
                });
            }
            onLoadingChanged: function (request) {
                if (request.status === WebEngineView.LoadSucceededStatus) {
                    pane.loaded = true;
                    pane.appearance();
                    if (pane.visible)
                        pane.focusTerminal();
                }
            }
            onRenderProcessTerminated: function () {
                if (pane.session)
                    pane.session.close();
            }
        }
        NativeButton {
            objectName: "reconnectTerminalButton"
            Layout.leftMargin: 12
            Layout.bottomMargin: 6
            visible: !!pane.session && pane.session.canReconnect
            text: "Open new SSH shell"
            tip: "Reconnect with a new shell. Previous commands will not be replayed."
            onClicked: { pane.session.reconnect(); pane.focusTerminal(); }
        }
        Label {
            Layout.fillWidth: true
            Layout.leftMargin: 12
            Layout.bottomMargin: visible ? 8 : 0
            visible: !!pane.session && !!pane.session.status
            text: pane.session ? pane.session.status : ""
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            font.pixelSize: 11
            color: palette.placeholderText
        }
    }
    NativeMenu {
        id: terminalMenu
        palette: pane.palette
        property bool hasSelection: false
        onClosed: pane.focusTerminal()
        NativeMenuItem {
            text: "Copy"
            enabled: terminalMenu.hasSelection
            onTriggered: pane.copySelection()
        }
        NativeMenuItem {
            text: "Paste"
            enabled: pane.loaded && pane.backend.clipboardHasText
            onTriggered: pane.pasteClipboard()
        }
        NativeMenuItem {
            text: "Select all"
            enabled: pane.loaded
            onTriggered: pane.selectAll()
        }
        MenuSeparator {}
        NativeMenuItem {
            text: "Clear scrollback"
            enabled: pane.loaded
            onTriggered: pane.clearScreen()
        }
    }
}
