pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Window
import QtQuick.Controls
import QtQuick.Layouts
import QtWebEngine

Item {
    id: pane
    readonly property var hostWindow: Window.window
    objectName: "browserPane"
    property string mediaProblem: ""
    property bool mediaDismissed: false
    signal titleUpdated(string title)
    signal newTabRequested(string url)
    required property var session
    required property var backend
    required property int tabId
    readonly property var control: backend ? backend.browserControl : null
    readonly property var controlState: control ? (control.tabs[String(tabId)] || ({})) : ({})
    property alias webView: browser
    property string requestedUrl: ""
    onRequestedUrlChanged: { if (requestedUrl) browser.url = requestedUrl }
    Component.onCompleted: {
        if (requestedUrl) browser.url = requestedUrl;
        if (control) control.register(tabId, browser);
    }
    Component.onDestruction: { if (control) control.unregister(tabId); }
    onVisibleChanged: { if (!visible && control) control.release(tabId); }
    BrowserAgent { control: pane.control; browser: pane.webView; tabId: pane.tabId }
    ColumnLayout {
        anchors.fill: parent
        spacing: 8
        RowLayout {
            Layout.fillWidth: true
            spacing: 4
            NativeButton { objectName: "browserBack"; quiet: true; icon.source: "icons/back.svg"; tip: "Back"; enabled: browser.canGoBack; onClicked: browser.goBack() }
            NativeButton { objectName: "browserForward"; quiet: true; icon.source: "icons/forward.svg"; tip: "Forward"; enabled: browser.canGoForward; onClicked: browser.goForward() }
            NativeButton { objectName: "browserReload"; quiet: true; icon.source: browser.loading ? "icons/close.svg" : "icons/reload.svg"; tip: browser.loading ? "Stop loading" : "Reload"; onClicked: browser.loading ? browser.stop() : browser.reload() }
            NativeField {
                id: address
                objectName: "browserAddress"
                Layout.fillWidth: true
                placeholderText: "Search or enter a URL"
                text: browser.url.toString() === "about:blank" ? "" : browser.url.toString()
                Accessible.name: "Browser address"
                onAccepted: {
                    let value = text.trim()
                    if (!value) return
                    if (!value.includes("://")) value = value.includes(" ") || !value.includes(".") && !value.startsWith("localhost")
                        ? "https://www.google.com/search?q=" + encodeURIComponent(value) : "https://" + value
                    if (/^https?:\/\//i.test(value)) browser.url = value
                    else loadError.text = "Enter an http or https address."
                }
            }
            NativeButton { quiet: true; icon.source: "icons/external.svg"; tip: "Open in default browser"; enabled: /^https?:/.test(browser.url.toString()); onClicked: Qt.openUrlExternally(browser.url) }
        }
        RowLayout {
            Layout.fillWidth: true
            NativeButton { objectName: "bookmarksButton"; text: "Bookmarks"; quiet: true; onClicked: { libraryDialog.mode = "bookmarks"; libraryDialog.open() } }
            NativeButton { objectName: "historyButton"; text: "History"; quiet: true; onClicked: { libraryDialog.mode = "history"; libraryDialog.open() } }
            Item { Layout.fillWidth: true }
            NativeButton { objectName: "browserDataButton"; icon.source: "icons/reload.svg"; quiet: true; tip: pane.session.importing ? "Importing browser data…" : "Browser data"; onClicked: dataDialog.open() }
        }
        RowLayout {
            Layout.fillWidth: true
            spacing: 8
            Rectangle {
                visible: !!pane.controlState.active
                implicitWidth: 6; implicitHeight: 6; radius: 3
                color: browserControlLabel.palette.highlight
            }
            Label {
                id: browserControlLabel
                Layout.fillWidth: true
                text: pane.controlState.active ? ((pane.controlState.working ? "Ava is using · " : "Shared with · ")
                      + (pane.backend && pane.backend.chatId === pane.controlState.chat ? pane.backend.chatTitle : pane.controlState.label))
                                              : pane.controlState.connecting ? "Connecting…" : pane.controlState.error || "Agent browser"
                font.pixelSize: 11
                color: palette.placeholderText
                elide: Text.ElideRight
                HoverHandler { id: browserStatusHover }
                NativeToolTip {
                    visible: browserStatusHover.hovered && !!pane.controlState.error
                    text: pane.controlState.error || ""
                }
            }
            NativeButton {
                objectName: "browserHandoff"
                visible: !pane.controlState.active
                text: "Use in this chat"
                enabled: !!pane.backend && pane.backend.connected && !!pane.backend.chatId && !pane.controlState.connecting
                tip: "Let this chat inspect and operate the visible tab. You can take control at any time."
                onClicked: pane.backend.handoffBrowser(pane.tabId)
            }
            NativeButton {
                objectName: "browserTakeOver"
                visible: !!pane.controlState.active
                text: "Take control"
                onClicked: pane.control.release(pane.tabId)
            }
        }
        Label { Layout.fillWidth: true; visible: pane.session.importing; text: "Importing browser data…"; font.pixelSize: 11; color: palette.placeholderText }
        ProgressBar { Layout.fillWidth: true; implicitHeight: 3; visible: browser.loading; from: 0; to: 100; value: browser.loadProgress }
        Label { id: loadError; Layout.fillWidth: true; visible: !!text; wrapMode: Text.Wrap; color: Theme.danger; font.pixelSize: 12 }
        Pane {
            objectName: "browserMediaNotice"
            Layout.fillWidth: true
            visible: !!pane.mediaProblem && !pane.mediaDismissed
            padding: 10
            background: Surface { color: Theme.warningSurface; border.width: 0 }
            ColumnLayout {
                width: parent.width
                Label { Layout.fillWidth: true; text: pane.mediaProblem; wrapMode: Text.Wrap; font.pixelSize: 12 }
                RowLayout {
                    NativeButton { objectName: "openMediaExternally"; text: "Open in browser"; icon.source: "icons/external.svg"; onClicked: Qt.openUrlExternally(browser.url) }
                    NativeButton { text: "Dismiss"; quiet: true; onClicked: pane.mediaDismissed = true }
                }
            }
        }
        WebEngineView {
            id: browser
            objectName: "webBrowser"
            Layout.fillWidth: true
            Layout.fillHeight: true
            url: "about:blank"
            profile: pane.session.profile
            settings.localContentCanAccessRemoteUrls: false
            settings.localContentCanAccessFileUrls: false
            Component.onCompleted: userScripts.insert(pane.session.mediaScript)
            settings.javascriptCanOpenWindows: true
            onContextMenuRequested: function(request) {
                if ((!request.isContentEditable && !request.selectedText) || request.mediaType !== ContextMenuRequest.MediaTypeNone || request.misspelledWord)
                    return;
                request.accepted = true;
                textMenu.editable = request.isContentEditable;
                textMenu.editFlags = request.editFlags;
                textMenu.linkUrl = request.linkUrl.toString();
                textMenu.popup(browser, request.position);
            }
            onJavaScriptConsoleMessage: function(level, message, line, source) {
                if (message.startsWith("__AVA_MEDIA__:") || message.includes("MEDIA_ERR_SRC_NOT_SUPPORTED")) {
                    browser.runJavaScript("!!document.createElement('video').canPlayType('video/mp4; codecs=\"avc1.42E01E\"')", function(supported) {
                        pane.mediaProblem = supported ? "This video could not be played. Try reloading the page or opening it in your default browser." : "This video could not be played. MP4/H.264 playback is unavailable in this embedded browser."
                    })
                }
            }
            onNavigationRequested: function(request) {
                if (!/^https?:\/\//i.test(request.url.toString()) && request.url.toString() !== "about:blank") request.action = WebEngineNavigationRequest.IgnoreRequest
            }
            onNewWindowRequested: function(request) { if (/^https?:\/\//i.test(request.requestedUrl.toString())) pane.newTabRequested(request.requestedUrl.toString()) }
            onTitleChanged: pane.titleUpdated(title || "Browser")
            onLoadingChanged: function(request) {
                if (request.status === WebEngineView.LoadStartedStatus) { pane.mediaProblem = ""; pane.mediaDismissed = false }
                loadError.text = request.status === WebEngineView.LoadFailedStatus ? "This page could not be loaded. " + request.errorString : ""
            }
        }
    }
    NativeMenu {
        id: textMenu
        palette: address.palette
        property bool editable: false
        property int editFlags: 0
        property string linkUrl: ""
        onClosed: Qt.callLater(function() { pane.hostWindow.requestActivate(); browser.forceActiveFocus(); })
        NativeMenuItem { text: "Undo"; visible: textMenu.editable; enabled: !!(textMenu.editFlags & ContextMenuRequest.CanUndo); onTriggered: browser.triggerWebAction(WebEngineView.Undo) }
        NativeMenuItem { text: "Redo"; visible: textMenu.editable; enabled: !!(textMenu.editFlags & ContextMenuRequest.CanRedo); onTriggered: browser.triggerWebAction(WebEngineView.Redo) }
        MenuSeparator { visible: textMenu.editable }
        NativeMenuItem { text: "Cut"; visible: textMenu.editable; enabled: !!(textMenu.editFlags & ContextMenuRequest.CanCut); onTriggered: browser.triggerWebAction(WebEngineView.Cut) }
        NativeMenuItem { text: "Copy"; enabled: !!(textMenu.editFlags & ContextMenuRequest.CanCopy); onTriggered: browser.triggerWebAction(WebEngineView.Copy) }
        NativeMenuItem { text: "Paste"; visible: textMenu.editable; enabled: !!(textMenu.editFlags & ContextMenuRequest.CanPaste); onTriggered: browser.triggerWebAction(WebEngineView.Paste) }
        MenuSeparator { visible: textMenu.editable }
        NativeMenuItem { text: "Select all"; enabled: !!(textMenu.editFlags & ContextMenuRequest.CanSelectAll); onTriggered: browser.triggerWebAction(WebEngineView.SelectAll) }
        MenuSeparator { visible: !!textMenu.linkUrl }
        NativeMenuItem { text: "Open link in new tab"; visible: !!textMenu.linkUrl; enabled: /^https?:\/\//i.test(textMenu.linkUrl); onTriggered: pane.newTabRequested(textMenu.linkUrl) }
        NativeMenuItem { text: "Copy link address"; visible: !!textMenu.linkUrl; onTriggered: browser.triggerWebAction(WebEngineView.CopyLinkToClipboard) }
    }
    NativeDialog {
        id: dataDialog
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(460, parent.width - 40)
        title: "Browser data"
        modal: true
        padding: 22
        ColumnLayout {
            width: parent.width
            spacing: 14
            Label { Layout.fillWidth: true; text: pane.session.importStatus || "Browser data is saved on this device."; wrapMode: Text.Wrap; font.pixelSize: 13 }
            Label { Layout.fillWidth: true; text: "Imports cookies, bookmarks and history from your default browser's selected profile. Some websites may ask you to sign in again."; wrapMode: Text.Wrap; font.pixelSize: 12; color: palette.placeholderText }
            Label { Layout.fillWidth: true; text: "Passwords, extensions and existing website storage aren't imported."; wrapMode: Text.Wrap; font.pixelSize: 12; color: palette.placeholderText }
            RowLayout {
                Layout.fillWidth: true
                NativeButton { objectName: "importBrowserButton"; text: "Import again"; enabled: !pane.session.importing; onClicked: pane.session.importDefaultBrowser() }
                Item { Layout.fillWidth: true }
                NativeButton { text: "Done"; primary: true; onClicked: dataDialog.close() }
            }
        }
    }
    NativeDialog {
        id: libraryDialog
        objectName: "browserLibraryDialog"
        property string mode: "bookmarks"
        property var entries: []
        function refresh() { entries = pane.session.search(mode, librarySearch.text) }
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(540, parent.width - 40)
        title: mode === "bookmarks" ? "Bookmarks" : "History"
        modal: true
        padding: 22
        onOpened: refresh()
        Connections { target: pane.session; function onChanged() { libraryDialog.refresh() } }
        ColumnLayout {
            width: parent.width
            spacing: 12
            NativeField { id: librarySearch; Layout.fillWidth: true; placeholderText: "Search by title or URL"; onTextChanged: libraryDialog.refresh() }
            ListView {
                Layout.fillWidth: true
                Layout.preferredHeight: Math.min(360, contentHeight)
                clip: true
                model: libraryDialog.entries
                ScrollBar.vertical: ScrollBar {}
                delegate: ItemDelegate {
                    id: entry
                    objectName: "browserEntry_" + modelData.title
                    required property var modelData
                    width: ListView.view.width
                    height: 52
                    background: Rectangle { radius: 9; color: entry.hovered ? entry.palette.alternateBase : "transparent" }
                    contentItem: ColumnLayout {
                        spacing: 3
                        Label { Layout.fillWidth: true; text: entry.modelData.title; elide: Text.ElideRight; font.pixelSize: 13 }
                        Label { Layout.fillWidth: true; text: entry.modelData.url; elide: Text.ElideMiddle; font.pixelSize: 11; color: palette.placeholderText }
                    }
                    onClicked: { browser.url = modelData.url; libraryDialog.close() }
                }
            }
            Label { visible: libraryDialog.entries.length === 0; text: pane.session.importing ? "Import in progress…" : "No matching entries"; font.pixelSize: 12; color: palette.placeholderText }
            NativeButton { objectName: "closeBrowserLibrary"; Layout.alignment: Qt.AlignRight; text: "Done"; onClicked: libraryDialog.close() }
        }
    }
}
