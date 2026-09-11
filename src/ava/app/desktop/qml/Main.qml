pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs

ApplicationWindow {
    id: window
    objectName: "desktopWindow"
    required property var backend
    required property string codeFont
    width: 1280
    height: 820
    minimumWidth: 800
    minimumHeight: 600
    visible: true
    title: backend.chatId ? backend.chatTitle + " · Ava" : "Ava"
    font.pixelSize: 14
    color: palette.window
    property bool closing: false
    property bool leftOpen: backend.preference("leftSidebar", true)
    property bool rightOpen: backend.preference("rightSidebar", false)
    property bool terminalOpen: false
    property string workspacePage: "chat"
    Connections { target: window.backend; function onSkillsRequested() { window.workspacePage = "skills"; } }
    readonly property bool boardOpen: workspacePage === "board"
    onTerminalOpenChanged: {
        if (!terminalOpen) {
            conversationSplit.forceActiveFocus();
            if (backend.chatId)
                chatComposer.focusInput();
        }
    }
    property bool dark: backend.preference("dark", false)
    property bool reducedMotion: backend.preference("reducedMotion", false)
    Binding { target: Theme; property: "dark"; value: window.dark }
    Binding { target: Theme; property: "reducedMotion"; value: window.reducedMotion }
    palette.window: Theme.workspace
    palette.base: Theme.surface
    palette.alternateBase: Theme.selection
    palette.button: Theme.surface
    palette.mid: Theme.border
    palette.light: Theme.hover
    palette.text: Theme.text
    palette.buttonText: Theme.text
    palette.windowText: Theme.text
    palette.placeholderText: Theme.secondaryText
    palette.highlight: Theme.accent
    palette.highlightedText: Theme.accentText
    palette.link: Theme.accent
    palette.disabled.text: Theme.disabledText
    palette.disabled.buttonText: Theme.disabledText
    onReducedMotionChanged: backend.savePreference("reducedMotion", reducedMotion)
    onLeftOpenChanged: backend.savePreference("leftSidebar", leftOpen)
    onRightOpenChanged: backend.savePreference("rightSidebar", rightOpen)
    onDarkChanged: backend.savePreference("dark", dark)
    onWorkspacePageChanged: backend.conversationVisible = workspacePage === "chat"
    onClosing: function (close) {
        close.accepted = closing;
        if (!closing) {
            closing = true;
            window.backend.shutdown();
        }
    }
    function showPanel(name) {
        workspacePage = "chat";
        window.backend.reviewCurrentChat();
        if (name === "toggleTerminal") {
            terminalOpen = !terminalOpen;
            if (terminalOpen)
                terminalDock.open();
        } else if (name === "model")
            modelDialog.open();
        else if (name === "terminal") {
            terminalOpen = true;
            terminalDock.open();
        } else {
            rightOpen = true;
            inspector.selectKind(name);
        }
    }
    function locationSubtitle() {
        if (!window.backend)
            return ""
        const parts = [window.backend.projectName]
        if (window.backend.workspacePath)
            parts.push(window.backend.workspacePath)
        if (window.backend.workspaceBranch)
            parts.push(window.backend.workspaceBranch)
        if (window.backend.machines.length > 1)
            parts.push(window.backend.machineName)
        return parts.join(" · ")
    }
    AttachmentDialog {
        parent: Overlay.overlay
        preview: window.backend ? window.backend.attachmentPreview : null
    }
    Connections {
        target: window.backend
        function onPanelRequested(name) {
            window.showPanel(name);
        }
        function onFileRequested(root, path) {
            window.rightOpen = true;
            inspector.showFiles(root, path, window.backend.projectId);
        }
        function onBrowserRequested(url) {
            window.rightOpen = true;
            inspector.showBrowser(url);
        }
        function onThemeRequested() {
            window.dark = !window.dark;
        }
        function onDialogRequested(title, note, rows) {
            infoDialog.title = title;
            infoDialog.note = note;
            infoDialog.rows = rows;
            infoDialog.open();
        }
        function onContextRequested(report) {
            contextDialog.report = report;
            contextDialog.open();
        }
        function onLoginRequested(provider) {
            loginProvider.text = provider;
            loginKey.text = "";
            loginDialog.open();
        }
    }
    Shortcut {
        sequences: [StandardKey.New]
        enabled: window.backend.online && !window.backend.busy
        onActivated: {
            window.workspacePage = "chat";
            window.backend.newChat();
        }
    }
    Shortcut {
        sequence: "Ctrl+K"
        onActivated: sidebar.openSearch()
    }
    Shortcut {
        sequence: "Ctrl+,"
        onActivated: settingsDialog.open()
    }
    Shortcut {
        sequences: [StandardKey.Quit]
        onActivated: window.close()
    }
    Shortcut {
        sequence: "Ctrl+J"
        enabled: !!window.backend.projectPath
        onActivated: {
            window.terminalOpen = !window.terminalOpen;
            if (window.terminalOpen)
                terminalDock.open();
        }
    }
    Shortcut {
        sequence: "Ctrl+B"
        onActivated: window.leftOpen = !window.leftOpen
    }
    Shortcut {
        sequence: "Ctrl+Alt+B"
        onActivated: {
            window.rightOpen = !window.rightOpen;
            if (window.rightOpen && !window.backend.fileState.kind)
                window.backend.browseFiles("");
        }
    }
    FolderDialog {
        id: folderDialog
        title: "Choose a project folder"
        onAccepted: window.backend.addProject(selectedFolder.toString())
    }
    FolderDialog {
        id: newChatFolderDialog
        objectName: "newChatFolderDialog"
        title: "Choose a working directory"
        onAccepted: window.backend.newChatInFolder(selectedFolder.toString())
    }
    WorktreeDialog { backend: window.backend }
    MachinesDialog {
        id: machinesDialog
        parent: Overlay.overlay
        anchors.centerIn: parent
        backend: window.backend
    }
    HostKeyDialog { backend: window.backend; codeFont: window.codeFont }
    NativeDialog {
        id: remoteProjectDialog
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(500, parent.width - 40)
        title: "Add project on " + window.backend.machineName
        modal: true
        focus: true
        padding: 22
        property bool createChat: false
        function openForProject() {
            createChat = false;
            title = "Add project on " + window.backend.machineName;
            open();
        }
        function openForChat() {
            createChat = true;
            title = "Choose a working directory on " + window.backend.machineName;
            open();
        }
        onOpened: remoteProjectPath.forceActiveFocus()
        ColumnLayout {
            width: parent.width
            spacing: 16
            NativeField {
                id: remoteProjectPath
                objectName: "remoteProjectPath"
                Layout.fillWidth: true
                placeholderText: "Remote folder path, e.g. ~/projects/ava"
                onAccepted: { if (remoteProjectAdd.enabled) remoteProjectAdd.clicked(); }
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                NativeButton { text: "Cancel"; onClicked: remoteProjectDialog.close() }
                NativeButton {
                    id: remoteProjectAdd
                    objectName: "addRemoteProjectAction"
                    text: remoteProjectDialog.createChat ? "Create chat" : "Add project"
                    primary: true
                    enabled: !!remoteProjectPath.text.trim() && window.backend.online
                    onClicked: {
                        if (remoteProjectDialog.createChat)
                            window.backend.newChatInFolder(remoteProjectPath.text.trim());
                        else
                            window.backend.addProject(remoteProjectPath.text.trim());
                        remoteProjectDialog.close();
                    }
                }
            }
        }
    }
    FileDialog {
        id: fileDialog
        objectName: "attachmentDialog"
        title: "Add files or images"
        fileMode: FileDialog.OpenFiles
        onAccepted: window.backend.addAttachments(selectedFiles.map(url => url.toString()))
    }

    SplitView {
        id: splitView
        objectName: "workspaceSplitView"
        anchors.fill: parent
        orientation: Qt.Horizontal
        enabled: !window.closing
        onResizingChanged: {
            if (!resizing)
                window.backend.savePanelWidths(window.leftOpen ? Math.round(sidebar.width) : -1, window.rightOpen ? Math.round(inspector.width) : -1);
        }
        handle: Rectangle {
            id: divider
            readonly property bool engaged: SplitHandle.hovered || SplitHandle.pressed
            implicitWidth: 5
            color: SplitHandle.pressed ? window.palette.alternateBase : "transparent"
            Rectangle {
                anchors.centerIn: parent
                width: divider.engaged ? 3 : 1
                height: parent.height
                radius: 1
                color: divider.engaged ? Theme.accent : Theme.border
            }
            HoverHandler {
                cursorShape: Qt.SplitHCursor
            }
        }
        SessionSidebar {
            id: sidebar
            objectName: "leftSidebar"
            currentPage: window.workspacePage
            visible: window.leftOpen
            SplitView.preferredWidth: window.backend.panelWidth("left", 248)
            SplitView.minimumWidth: 180
            SplitView.maximumWidth: 380
            backend: window.backend
            onHideRequested: window.leftOpen = false
            onAddProjectRequested: window.backend.remoteMachine ? remoteProjectDialog.openForProject() : folderDialog.open()
            onMachinesRequested: machinesDialog.open()
            onSettingsRequested: settingsDialog.open()
            onBoardRequested: window.workspacePage = "board"
            onAutomationsRequested: window.workspacePage = "automations"
            onSkillsRequested: window.workspacePage = "skills"
            onMcpRequested: window.workspacePage = "mcp"
            onAnalyticsRequested: window.workspacePage = "analytics"
            onConversationRequested: window.workspacePage = "chat"
        }
        Loader {
            id: boardLoader
            visible: window.workspacePage !== "chat"
            active: visible
            SplitView.fillWidth: true
            SplitView.minimumWidth: 330
            sourceComponent: window.boardOpen ? boardComponent : window.workspacePage === "skills" ? skillsComponent : window.workspacePage === "mcp" ? mcpComponent : window.workspacePage === "analytics" ? analyticsComponent : automationsComponent
        }
        Component {
            id: analyticsComponent
            AnalyticsPane {
                backend: window.backend
                sidebarVisible: window.leftOpen
                onCloseRequested: {
                    window.workspacePage = "chat";
                    window.backend.reviewCurrentChat();
                }
                onSidebarRequested: window.leftOpen = true
            }
        }
        Component {
            id: boardComponent
            SessionBoard {
                backend: window.backend
                sidebarVisible: window.leftOpen
                onOpenChat: function (identity) {
                    window.workspacePage = "chat";
                    window.backend.openChat(identity);
                }
                onCloseRequested: {
                    window.workspacePage = "chat";
                    window.backend.reviewCurrentChat();
                }
                onSidebarRequested: window.leftOpen = true
            }
        }
        Component {
            id: mcpComponent
            McpPane {
                backend: window.backend
                sidebarVisible: window.leftOpen
                codeFont: window.codeFont
                onCloseRequested: {
                    window.workspacePage = "chat";
                    window.backend.reviewCurrentChat();
                }
                onSidebarRequested: window.leftOpen = true
            }
        }
        Component {
            id: skillsComponent
            SkillsPane {
                backend: window.backend
                sidebarVisible: window.leftOpen
                onUseRequested: { window.workspacePage = "chat"; Qt.callLater(chatComposer.focusInputAtEnd); }
                onCloseRequested: {
                    window.workspacePage = "chat";
                    window.backend.reviewCurrentChat();
                }
                onSidebarRequested: window.leftOpen = true
            }
        }
        Component {
            id: automationsComponent
            AutomationsPane {
                backend: window.backend
                sidebarVisible: window.leftOpen
                onOpenChat: function (identity) {
                    window.workspacePage = "chat";
                    window.backend.openChat(identity);
                }
                onCloseRequested: {
                    window.workspacePage = "chat";
                    window.backend.reviewCurrentChat();
                }
                onSidebarRequested: window.leftOpen = true
            }
        }
        SplitView {
            id: conversationSplit
            visible: window.workspacePage === "chat"
            orientation: Qt.Vertical
            SplitView.fillWidth: true
            SplitView.minimumWidth: 330
            onResizingChanged: {
                if (!resizing && window.terminalOpen)
                    window.backend.saveTerminalHeight(Math.round(terminalDock.height));
            }
            handle: Rectangle {
                objectName: "terminalDivider"
                implicitHeight: 5
                color: SplitHandle.hovered || SplitHandle.pressed ? Theme.accent : Theme.border
                HoverHandler {
                    cursorShape: Qt.SplitVCursor
                }
            }
            ColumnLayout {
                id: chatColumn
                readonly property real contentWidth: Math.min(760, width - 48)
                SplitView.fillHeight: true
                SplitView.minimumHeight: 300
                spacing: 0
                Pane {
                    Layout.fillWidth: true
                    verticalPadding: 8
                    horizontalPadding: 16
                    background: null
                    RowLayout {
                        width: parent.width
                        spacing: 8
                        NativeButton {
                            objectName: "toggleLeftSidebar"
                            visible: !window.leftOpen
                            icon.source: "icons/left.svg"
                            quiet: true
                            tip: "Show sidebar"
                            onClicked: window.leftOpen = true
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 3
                            Label {
                                Layout.fillWidth: true
                                text: window.backend.chatId ? window.backend.chatTitle : "New conversation"
                                font.pixelSize: 14
                                font.weight: Font.DemiBold
                                elide: Text.ElideRight
                            }
                            Label {
                                objectName: "locationSubtitle"
                                Layout.fillWidth: true
                                text: window.locationSubtitle()
                                font.pixelSize: 11
                                color: palette.placeholderText
                                elide: Text.ElideMiddle
                            }
                        }
                        NativeButton {
                            text: window.backend.status === "paused" ? "Resume" : "Pause"
                            visible: ["running", "paused"].indexOf(window.backend.status) >= 0
                            enabled: window.backend.connected && !window.backend.busy
                            onClicked: window.backend.control(window.backend.status === "paused" ? "resume" : "pause")
                        }
                        NativeButton {
                            objectName: "stopButton"
                            text: "Stop"
                            visible: ["running", "pausing", "paused", "aborting"].indexOf(window.backend.status) >= 0
                            enabled: window.backend.connected && !window.backend.busy && window.backend.status !== "aborting"
                            onClicked: window.backend.control("abort")
                        }
                        NativeButton {
                            objectName: "toggleTerminalButton"
                            icon.source: "icons/terminal.svg"
                            quiet: true
                            selected: window.terminalOpen
                            tip: "Toggle terminal · " + (Qt.platform.os === "osx" ? "⌘ J" : "Ctrl+J")
                            enabled: !!window.backend.projectPath && window.backend.online
                            onClicked: {
                                window.terminalOpen = !window.terminalOpen;
                                if (window.terminalOpen)
                                    terminalDock.open();
                            }
                        }
                        NativeButton {
                            objectName: "reviewChangesButton"
                            icon.source: "icons/changes.svg"
                            quiet: true
                            tip: "Review changes"
                            enabled: !!window.backend.projectPath && window.backend.online
                            onClicked: window.showPanel("changes")
                        }
                        NativeButton {
                            objectName: "toggleRightSidebar"
                            icon.source: "icons/right.svg"
                            quiet: true
                            selected: window.rightOpen
                            tip: window.rightOpen ? "Hide inspector" : "Show files and browser"
                            onClicked: {
                                window.rightOpen = !window.rightOpen;
                                if (window.rightOpen && !window.backend.fileState.kind)
                                    window.backend.browseFiles("");
                            }
                        }
                    }
                }
                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: 1
                    color: window.palette.mid
                    opacity: 0.35
                }
                Pane {
                    Layout.fillWidth: true
                    visible: !!window.backend.error
                    padding: 12
                    background: Rectangle {
                        color: Theme.dangerSurface
                    }
                    RowLayout {
                        width: parent.width
                        Label {
                            Layout.fillWidth: true
                            text: window.backend.error
                            wrapMode: Text.Wrap
                            color: Theme.danger
                            font.pixelSize: Theme.caption
                        }
                        NativeButton {
                            icon.source: "icons/close.svg"
                            quiet: true
                            tip: "Dismiss error"
                            onClicked: window.backend.dismissError()
                        }
                    }
                }
                Item {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    ListView {
                        id: conversation
                        objectName: "transcriptView"
                        anchors.fill: parent
                        anchors.margins: 24
                        spacing: 22
                        clip: true
                        // Pooling unloads the variable-height Loaders, feeding zero/stale
                        // heights back into ListView. Destroy offscreen delegates instead.
                        reuseItems: false
                        // Follow the measured last row, not the estimated footer position.
                        currentIndex: follow ? count - 1 : -1
                        model: window.backend.transcript
                        footer: Item {
                            id: runStatus
                            objectName: "runStatus"
                            width: conversation.width
                            visible: !!runChat && (runState !== "idle" || !window.backend.connected)
                            height: visible ? runStatusContent.implicitHeight + 18 : 0
                            property string runChat: window.backend.chatId
                            property string runState: window.backend.status
                            property string previousState: "idle"
                            property double accumulatedMs: 0
                            property double segmentStartedAt: 0
                            property int elapsedSeconds: 0
                            readonly property string phase: !window.backend.connected ? (!window.backend.online && window.backend.error ? "Disconnected" : "Connecting")
                                                            : runState === "running" ? (window.backend.transcript.activity || "Thinking")
                                                            : runState === "pausing" ? "Finishing the current step"
                                                            : runState === "paused" ? "Paused"
                                                            : runState === "aborting" ? "Stopping"
                                                            : ""
                            function timed(state) {
                                return ["running", "pausing", "aborting"].indexOf(state) >= 0;
                            }
                            function updateElapsed(now) {
                                const activeMs = segmentStartedAt ? now - segmentStartedAt : 0;
                                elapsedSeconds = Math.max(0, Math.floor((accumulatedMs + activeMs) / 1000));
                            }
                            function restart() {
                                accumulatedMs = 0;
                                elapsedSeconds = 0;
                                previousState = runState;
                                segmentStartedAt = timed(runState) ? Date.now() : 0;
                                Qt.callLater(conversation.followLatest);
                            }
                            function syncState() {
                                const now = Date.now();
                                if (timed(previousState) && segmentStartedAt)
                                    accumulatedMs += now - segmentStartedAt;
                                segmentStartedAt = 0;
                                if (runState === "idle") {
                                    accumulatedMs = 0;
                                    elapsedSeconds = 0;
                                } else if (timed(runState)) {
                                    segmentStartedAt = now;
                                }
                                previousState = runState;
                                updateElapsed(now);
                                Qt.callLater(conversation.followLatest);
                            }
                            function formatElapsed(total) {
                                if (total < 60)
                                    return total + "s";
                                const seconds = total % 60;
                                const paddedSeconds = seconds < 10 ? "0" + seconds : seconds;
                                if (total < 3600)
                                    return Math.floor(total / 60) + "m " + paddedSeconds + "s";
                                const minutes = Math.floor(total / 60) % 60;
                                const paddedMinutes = minutes < 10 ? "0" + minutes : minutes;
                                return Math.floor(total / 3600) + "h " + paddedMinutes + "m " + paddedSeconds + "s";
                            }
                            onRunChatChanged: restart()
                            onRunStateChanged: syncState()
                            Component.onCompleted: restart()
                            Timer {
                                interval: 250
                                repeat: true
                                running: runStatus.visible && runStatus.timed(runStatus.runState)
                                onTriggered: runStatus.updateElapsed(Date.now())
                            }
                            RowLayout {
                                id: runStatusContent
                                width: chatColumn.contentWidth
                                anchors.horizontalCenter: parent.horizontalCenter
                                anchors.top: parent.top
                                anchors.topMargin: 9
                                spacing: 8
                                Rectangle {
                                    Layout.preferredWidth: 7
                                    Layout.preferredHeight: 7
                                    radius: 4
                                    color: runStatus.runState === "paused" || runStatus.runState === "pausing"
                                           ? Theme.warning
                                           : runStatus.runState === "aborting" ? Theme.danger
                                           : window.palette.highlight
                                    SequentialAnimation on opacity {
                                        running: runStatus.runState === "running" && !Theme.reducedMotion
                                        loops: Animation.Infinite
                                        NumberAnimation { to: 0.35; duration: 700 }
                                        NumberAnimation { to: 1; duration: 700 }
                                    }
                                }
                                Label {
                                    id: runPhase
                                    objectName: "runPhase"
                                    text: runStatus.phase
                                    elide: Text.ElideRight
                                    font.pixelSize: 13
                                    font.weight: Font.DemiBold
                                    color: window.palette.text
                                }
                                Label {
                                    id: runElapsed
                                    objectName: "runElapsed"
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: runPhase.implicitHeight
                                    verticalAlignment: Text.AlignVCenter
                                    text: !window.backend.connected ? (!window.backend.online && window.backend.error ? "" : window.backend.connectionLabel)
                                          : runStatus.runState === "paused"
                                            ? "(" + runStatus.formatElapsed(runStatus.elapsedSeconds) + " · waiting to resume)"
                                          : runStatus.runState === "running"
                                            ? "(" + runStatus.formatElapsed(runStatus.elapsedSeconds) + " · Esc to pause)"
                                          : runStatus.runState === "pausing"
                                            ? "(" + runStatus.formatElapsed(runStatus.elapsedSeconds) + " · Esc to stop)"
                                            : "(" + runStatus.formatElapsed(runStatus.elapsedSeconds) + ")"
                                    elide: Text.ElideRight
                                    font.family: window.codeFont
                                    font.pixelSize: 11
                                    color: window.palette.placeholderText
                                }
                            }
                        }
                        property bool follow: true
                        function alignTail() {
                            if (!follow || !currentItem)
                                return;
                            contentY = Math.max(originY, currentItem.y + currentItem.height + (footerItem ? footerItem.height : 0) - height);
                        }
                        function followLatest() {
                            if (!follow)
                                return;
                            forceLayout();
                            alignTail();
                        }
                        // Geometry changes during polish must align in the same frame,
                        // without forceLayout() re-entering the layout that emitted them.
                        Connections {
                            target: conversation.currentItem
                            function onHeightChanged() { conversation.alignTail(); }
                            function onYChanged() { conversation.alignTail(); }
                        }
                        Connections {
                            target: conversation.footerItem
                            function onHeightChanged() { conversation.alignTail(); }
                        }
                        onMovementStarted: follow = false
                        onMovementEnded: follow = atYEnd
                        onHeightChanged: Qt.callLater(followLatest)
                        onWidthChanged: Qt.callLater(followLatest)
                        onCurrentItemChanged: Qt.callLater(followLatest)
                        onContentHeightChanged: alignTail()
                        onCountChanged: {
                            if (count === 0)
                                follow = true;
                            Qt.callLater(followLatest);
                        }
                        ScrollBar.vertical: ScrollBar {
                            onPressedChanged: conversation.follow = !pressed && conversation.atYEnd
                        }
                        delegate: Item {
                            id: transcriptRow
                            required property string kind
                            required property string heading
                            required property string body
                            required property string detail
                            required property var attachments
                            required property int sourceRow
                            required property int groupCount
                            required property bool groupExpanded
                            required property int groupRunning
                            required property int groupFailed
                            required property bool outputExpanded
                            width: conversation.width
                            height: messageColumn.implicitHeight
                            Column {
                                id: messageColumn
                                width: chatColumn.contentWidth
                                anchors.horizontalCenter: parent.horizontalCenter
                                spacing: 8
                                NativeButton {
                                    id: groupToggle
                                    objectName: "activityGroupToggle"
                                    width: parent.width
                                    visible: transcriptRow.groupCount > 1
                                    quiet: true
                                    text: transcriptRow.groupCount + " activities"
                                    tip: transcriptRow.groupExpanded ? "Collapse activities" : "Show all activities"
                                    contentItem: RowLayout {
                                        spacing: 8
                                        Image {
                                            source: transcriptRow.groupExpanded ? "icons/down.svg" : "icons/chevron.svg"
                                            sourceSize.width: 14
                                            sourceSize.height: 14
                                            opacity: 0.55
                                        }
                                        Label {
                                            objectName: "activityGroupSummary"
                                            Layout.fillWidth: true
                                            text: groupToggle.text
                                            font.pixelSize: 12
                                            color: window.palette.placeholderText
                                        }
                                        Label {
                                            objectName: "activityGroupRunning"
                                            visible: transcriptRow.groupRunning > 0
                                            text: transcriptRow.groupRunning + " running"
                                            font.pixelSize: 11
                                            color: window.palette.placeholderText
                                        }
                                        Label {
                                            objectName: "activityGroupFailed"
                                            visible: transcriptRow.groupFailed > 0
                                            text: transcriptRow.groupFailed + " failed"
                                            font.pixelSize: 11
                                            color: Theme.danger
                                        }
                                    }
                                    onClicked: {
                                        conversation.follow = false;
                                        window.backend.transcript.toggleGroup(transcriptRow.sourceRow);
                                    }
                                }
                                Loader {
                                    id: messageLoader
                                    width: parent.width
                                    active: transcriptRow.groupCount <= 1 || transcriptRow.groupExpanded
                                    visible: active
                                    sourceComponent: TranscriptMessage {
                                        kind: transcriptRow.kind
                                        heading: transcriptRow.heading
                                        body: transcriptRow.body
                                        detail: transcriptRow.detail
                                        attachments: transcriptRow.attachments
                                        backend: window.backend
                                        codeFont: window.codeFont
                                        readingSize: window.backend.readingSize
                                        expanded: transcriptRow.outputExpanded
                                        onExpansionToggled: {
                                            conversation.follow = false;
                                            window.backend.transcript.toggleOutput(transcriptRow.sourceRow);
                                        }
                                    }
                                }
                            }
                        }
                    }
                    NativeButton {
                        objectName: "jumpToLatest"
                        anchors.horizontalCenter: parent.horizontalCenter
                        anchors.bottom: parent.bottom
                        anchors.bottomMargin: 12
                        visible: conversation.count > 0 && !conversation.atYEnd
                        icon.source: "icons/down.svg"
                        implicitWidth: 32
                        tip: "Jump to latest message"
                        onClicked: {
                            conversation.follow = true;
                            conversation.positionViewAtEnd();
                            Qt.callLater(conversation.followLatest);
                        }
                    }
                    ColumnLayout {
                        anchors.centerIn: parent
                        width: Math.min(420, parent.width - 60)
                        visible: conversation.count === 0
                        spacing: 12
                        Label {
                            Layout.fillWidth: true
                            horizontalAlignment: Text.AlignHCenter
                            wrapMode: Text.WordWrap
                            text: "What would you like to work on?"
                            font.pixelSize: window.rightOpen ? 21 : 26
                            font.weight: Font.Medium
                            color: window.palette.text
                        }
                        Label {
                            Layout.fillWidth: true
                            text: window.backend.projectId ? "New conversations start in this folder." : "Add a project folder to start a conversation."
                            horizontalAlignment: Text.AlignHCenter
                            wrapMode: Text.WordWrap
                            color: palette.placeholderText
                            font.pixelSize: 13
                            lineHeight: 1.4
                        }
                        Label {
                            id: chatDirectory
                            objectName: "chatDirectory"
                            Layout.fillWidth: true
                            visible: !!window.backend.projectId
                            text: window.backend.workspacePath
                            horizontalAlignment: Text.AlignHCenter
                            wrapMode: Text.WrapAnywhere
                            maximumLineCount: 2
                            color: palette.text
                            font.pixelSize: 12
                        }
                        RowLayout {
                            Layout.alignment: Qt.AlignHCenter
                            Layout.topMargin: 8
                            spacing: 8
                            NativeButton {
                                objectName: "emptyStateAction"
                                text: window.backend.projectId ? "New conversation" : "Add project"
                                icon.source: "icons/plus.svg"
                                primary: true
                                visible: !window.backend.chatId
                                enabled: window.backend.online && !window.backend.busy
                                onClicked: window.backend.projectId ? window.backend.newChat() : folderDialog.open()
                            }
                            NativeButton {
                                objectName: "chooseChatFolderButton"
                                text: "Choose folder…"
                                visible: !!window.backend.projectId && (!window.backend.chatId || (!window.backend.draft && window.backend.attachments.length === 0))
                                enabled: window.backend.online && !window.backend.busy
                                onClicked: window.backend.remoteMachine ? remoteProjectDialog.openForChat() : newChatFolderDialog.open()
                            }
                        }
                    }
                }
                ChatComposer {
                    id: chatComposer
                    Layout.fillWidth: true
                    Layout.preferredWidth: chatColumn.contentWidth
                    Layout.maximumWidth: chatColumn.contentWidth
                    Layout.alignment: Qt.AlignHCenter
                    Layout.leftMargin: 24
                    Layout.rightMargin: 24
                    Layout.bottomMargin: 16
                    backend: window.backend
                    onAttach: fileDialog.open()
                    onModels: modelDialog.open()
                }
            }
            TerminalDock {
                id: terminalDock
                visible: window.terminalOpen
                SplitView.preferredHeight: window.backend.terminalHeight
                SplitView.minimumHeight: 140
                SplitView.maximumHeight: Math.max(140, conversationSplit.height - 305)
                backend: window.backend
                codeFont: window.codeFont
                onHideRequested: window.terminalOpen = false
            }
        }
        Inspector {
            id: inspector
            objectName: "rightSidebar"
            visible: window.rightOpen && window.workspacePage === "chat"
            SplitView.preferredWidth: window.backend.panelWidth("right", 520)
            SplitView.minimumWidth: 280
            SplitView.maximumWidth: Math.max(280, window.width - 340 - (window.leftOpen ? sidebar.width + 5 : 0))
            backend: window.backend
            codeFont: window.codeFont
            onClosed: window.rightOpen = false
        }
    }

    SettingsDialog {
        id: settingsDialog
        backend: window.backend
        codeFont: window.codeFont
        dark: window.dark
        reducedMotion: window.reducedMotion
        onReducedMotionRequested: function (value) { window.reducedMotion = value; }
        onDarkRequested: function (value) { window.dark = value; }
        onArchivedRequested: sidebar.openSearch(true)
    }

    ModelDialog {
        id: modelDialog
        backend: window.backend
        onProvidersRequested: { settingsDialog.page = 1; settingsDialog.open(); }
    }
    ContextDialog {
        id: contextDialog
        dark: window.dark
        onClosed: chatComposer.focusInput()
    }
    NativeDialog {
        id: infoDialog
        objectName: "infoDialog"
        property string note: ""
        property var rows: []
        anchors.centerIn: parent
        width: Math.min(540, window.width - 60)
        modal: true
        padding: 22
        ColumnLayout {
            width: parent.width
            spacing: 14
            Label {
                Layout.fillWidth: true
                text: infoDialog.note
                wrapMode: Text.Wrap
                font.pixelSize: 12
                color: palette.placeholderText
            }
            ListView {
                Layout.fillWidth: true
                Layout.preferredHeight: Math.min(380, contentHeight)
                clip: true
                model: infoDialog.rows
                ScrollBar.vertical: ScrollBar {}
                delegate: ItemDelegate {
                    id: row
                    required property var modelData
                    objectName: "dialog_" + modelData.value
                    width: ListView.view.width
                    height: infoRow.implicitHeight + 18
                    background: Rectangle {
                        radius: 10
                        color: row.hovered ? window.palette.light : "transparent"
                    }
                    contentItem: ColumnLayout {
                        id: infoRow
                        spacing: 4
                        Label {
                            text: row.modelData.label
                            font.pixelSize: 13
                            font.weight: Font.Medium
                        }
                        Label {
                            Layout.fillWidth: true
                            text: row.modelData.detail
                            wrapMode: Text.Wrap
                            font.pixelSize: 12
                            color: palette.placeholderText
                        }
                    }
                    onClicked: {
                        if (!modelData.action)
                            return;
                        infoDialog.close();
                        if (modelData.action === "skill")
                            window.backend.chooseCommand(modelData.value, "skill");
                        else
                            window.backend.runCommand("/" + modelData.value);
                    }
                }
            }
            NativeButton {
                Layout.alignment: Qt.AlignRight
                text: "Done"
                onClicked: infoDialog.close()
            }
        }
    }
    NativeDialog {
        id: loginDialog
        anchors.centerIn: parent
        width: 420
        modal: true
        padding: 22
        title: "Provider credentials"
        ColumnLayout {
            width: parent.width
            spacing: 12
            NativeField {
                id: loginProvider
                Layout.fillWidth: true
                placeholderText: "Provider"
            }
            NativeField {
                id: loginKey
                Layout.fillWidth: true
                placeholderText: "API key"
                echoMode: TextInput.Password
            }
            NativeButton {
                Layout.alignment: Qt.AlignRight
                primary: true
                text: "Save key"
                enabled: !!loginProvider.text && !!loginKey.text
                onClicked: {
                    window.backend.login(loginProvider.text, loginKey.text);
                    loginKey.text = "";
                    loginDialog.close();
                }
            }
        }
    }
    Rectangle {
        anchors.fill: parent
        visible: window.closing
        color: window.palette.window
        opacity: 0.95
        Label {
            anchors.centerIn: parent
            text: "Saving and closing…"
            font.pixelSize: 20
        }
    }
}
