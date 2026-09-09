pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Pane {
    id: sidebar
    required property var backend
    signal hideRequested
    signal addProjectRequested
    signal settingsRequested
    signal machinesRequested
    signal boardRequested
    signal automationsRequested
    signal skillsRequested
    signal mcpRequested
    signal analyticsRequested
    signal conversationRequested
    property var selectedChat: ({})
    property var selectedProject: ({})
    padding: 12
    background: Rectangle {
        color: sidebar.palette.window.hslLightness < 0.5 ? "#191919" : "#f7f7f8"
    }
    function openSearch(archived = false) {
        searchDialog.archivedOnly = archived;
        searchField.text = "";
        searchDialog.open();
    }
    function manageChat(chat, anchor, x = anchor.width, y = anchor.height) {
        selectedChat = chat;
        const position = anchor.mapToItem(Overlay.overlay, x, y);
        chatMenu.popup(Overlay.overlay, position.x, position.y);
    }
    function manageProject(project, anchor, x = anchor.width, y = anchor.height) {
        selectedProject = project;
        const position = anchor.mapToItem(Overlay.overlay, x, y);
        projectMenu.popup(Overlay.overlay, position.x, position.y);
    }
    function openConversation(identity) {
        sidebar.conversationRequested();
        sidebar.backend.openChat(identity);
    }
    function newConversation(project = "") {
        sidebar.conversationRequested();
        if (project) sidebar.backend.newChat(project);
        else sidebar.backend.newChat();
    }
    ColumnLayout {
        anchors.fill: parent
        spacing: 4
        RowLayout {
            Layout.fillWidth: true
            Layout.bottomMargin: 10
            Label {
                Layout.fillWidth: true
                Layout.leftMargin: 10
                text: "Ava"
                font.pixelSize: 15
                font.weight: Font.DemiBold
            }
            NativeButton {
                objectName: "hideSidebarButton"
                icon.source: "icons/left.svg"
                quiet: true
                tip: "Hide sidebar"
                onClicked: sidebar.hideRequested()
            }
        }
        RowLayout {
            Layout.fillWidth: true
            spacing: 0
        NativeButton {
            objectName: "newChatButton"
            Layout.fillWidth: true
            text: "New chat"
            icon.source: "icons/new.svg"
            quiet: true
            display: AbstractButton.TextBesideIcon
            enabled: sidebar.backend.online && !!sidebar.backend.projectId && !sidebar.backend.busy
            onClicked: sidebar.newConversation()
            contentItem: RowLayout {
                spacing: 9
                Image {
                    source: "icons/new.svg"
                    sourceSize.width: 16
                    sourceSize.height: 16
                }
                Label {
                    Layout.fillWidth: true
                    text: "New chat"
                    font.pixelSize: 13
                }
            }
        }
            NativeButton {
                objectName: "newChatOptionsButton"
                icon.source: "icons/chevron.svg"
                icon.width: 12
                icon.height: 12
                quiet: true
                tip: "Choose where to work"
                enabled: sidebar.backend.online && !!sidebar.backend.projectId && !sidebar.backend.busy
                onClicked: newChatMenu.popup()
                NativeMenu {
                    id: newChatMenu
                    popupType: Popup.Item
                    NativeMenuItem {
                        text: "New chat in current folder"
                        onTriggered: sidebar.newConversation()
                    }
                    NativeMenuItem {
                        objectName: "newWorktreeAction"
                        text: "New chat in a worktree…"
                        onTriggered: {
                            sidebar.conversationRequested();
                            sidebar.backend.prepareWorktree();
                        }
                    }
                }
            }
        }
        NativeButton {
            objectName: "searchChatsButton"
            text: "Search chats"
            Layout.fillWidth: true
            quiet: true
            tip: "Search conversations"
            enabled: sidebar.backend.projects.length > 0
            onClicked: sidebar.openSearch()
            contentItem: RowLayout {
                spacing: 9
                Image {
                    source: "icons/search.svg"
                    sourceSize.width: 16
                    sourceSize.height: 16
                }
                Label {
                    Layout.fillWidth: true
                    text: "Search chats"
                    font.pixelSize: 13
                }
                Label {
                    text: Qt.platform.os === "osx" ? "⌘ K" : "Ctrl K"
                    font.pixelSize: 10
                    color: palette.placeholderText
                }
            }
        }
        NativeButton {
            objectName: "sessionBoardButton"
            text: "Session board"
            icon.source: "icons/board.svg"
            quiet: true
            Layout.fillWidth: true
            onClicked: sidebar.boardRequested()
            contentItem: RowLayout {
                spacing: 9
                Image { source: "icons/board.svg"; sourceSize.width: 16; sourceSize.height: 16 }
                Label { Layout.fillWidth: true; text: "Session board"; font.pixelSize: 13 }
                Label { visible: sidebar.backend.board.totals[1] > 0; text: sidebar.backend.board.totals[1]; color: palette.placeholderText; font.pixelSize: 11 }
            }
        }
        NativeButton {
            objectName: "analyticsButton"
            Layout.fillWidth: true
            text: "Analytics"
            icon.source: "icons/analytics.svg"
            quiet: true
            onClicked: sidebar.analyticsRequested()
            contentItem: RowLayout {
                spacing: 9
                Image { source: "icons/analytics.svg"; sourceSize.width: 16; sourceSize.height: 16 }
                Label { Layout.fillWidth: true; text: "Analytics"; font.pixelSize: 13 }
            }
        }
        NativeButton {
            objectName: "automationsButton"
            text: "Automations"
            quiet: true
            Layout.fillWidth: true
            onClicked: sidebar.automationsRequested()
            contentItem: RowLayout {
                spacing: 9
                Image { source: "icons/clock.svg"; sourceSize.width: 16; sourceSize.height: 16 }
                Label { Layout.fillWidth: true; text: "Automations"; font.pixelSize: 13 }
            }
        }
        NativeButton {
            objectName: "skillsButton"
            text: "Skills"
            quiet: true
            Layout.fillWidth: true
            onClicked: sidebar.skillsRequested()
            contentItem: RowLayout {
                spacing: 9
                Label { text: "✦"; font.pixelSize: 17; Layout.preferredWidth: 16 }
                Label { Layout.fillWidth: true; text: "Skills"; font.pixelSize: 13 }
            }
        }
        NativeButton {
            objectName: "mcpButton"
            text: "MCP servers"
            quiet: true
            Layout.fillWidth: true
            onClicked: sidebar.mcpRequested()
            contentItem: RowLayout {
                spacing: 9
                Image { source: "icons/plug.svg"; sourceSize.width: 16; sourceSize.height: 16 }
                Label { Layout.fillWidth: true; text: "MCP servers"; font.pixelSize: 13 }
            }
        }
        ListView {
            id: sessions
            objectName: "sessionList"
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.topMargin: 16
            clip: true
            spacing: 2
            model: sidebar.backend.sessionRows
            reuseItems: true
            ScrollBar.vertical: ScrollBar {}
            function revealChat(identity) {
                for (let i = 0; i < model.length; ++i) {
                    if (model[i].kind === "chat" && model[i].id === identity) {
                        positionViewAtIndex(i, ListView.Contain);
                        return;
                    }
                }
            }
            delegate: Item {
                id: row
                required property var modelData
                readonly property bool project: modelData.kind === "project"
                readonly property bool machine: modelData.kind === "machine"
                readonly property bool section: modelData.kind === "section"
                readonly property bool pinned: modelData.kind === "chat" && !!modelData.pinned
                width: ListView.view.width
                height: section ? 36 : machine ? 44 : project ? 34 : pinned ? 48 : 32
                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 10
                    visible: row.section
                    Label {
                        objectName: row.section ? row.modelData.id + "Section" : ""
                        Layout.fillWidth: true
                        text: row.modelData.title
                        font.pixelSize: 11
                        color: sidebar.palette.placeholderText
                    }
                    NativeButton {
                        objectName: row.section && row.modelData.id === "projects" ? (sidebar.backend.machines.length > 1 ? "addMachineButton" : "addProjectButton") : ""
                        visible: row.section && row.modelData.id === "projects"
                        implicitWidth: 28
                        implicitHeight: 28
                        icon.source: "icons/plus.svg"
                        quiet: true
                        tip: sidebar.backend.machines.length > 1 ? "Add machine" : "Add project"
                        enabled: sidebar.backend.machines.length > 1 || sidebar.backend.online
                        onClicked: sidebar.backend.machines.length > 1 ? sidebar.machinesRequested() : sidebar.addProjectRequested()
                    }
                }
                ItemDelegate {
                    id: rowButton
                    objectName: row.section ? "" : (row.machine ? "machineGroup_" : row.project ? "projectGroup_" : "session_") + row.modelData.id
                    visible: !row.section
                    anchors.fill: parent
                    enabled: row.machine || row.project || !!row.modelData.online
                    highlighted: !row.project && !row.machine && row.modelData.id === sidebar.backend.chatId
                    leftPadding: row.machine || row.pinned ? 8 : (row.project ? 8 : 26) + (sidebar.backend.machines.length > 1 ? 12 : 0)
                    rightPadding: row.project ? 56 : 30
                    Accessible.name: row.modelData.title || "New chat"
                    Accessible.description: row.machine ? (row.modelData.expanded ? "Collapse machine" : "Expand machine") : row.project ? (row.modelData.expanded ? "Collapse project" : "Expand project") : row.pinned ? "Pinned conversation in " + row.modelData.project : "Open conversation"
                    onClicked: row.machine ? sidebar.backend.toggleMachineGroup(row.modelData.id) : row.project ? sidebar.backend.toggleProjectGroup(row.modelData.id) : sidebar.openConversation(row.modelData.id)
                    NativeToolTip {
                        visible: rowButton.hovered
                        palette: sidebar.palette
                        text: row.machine ? row.modelData.status : row.project ? row.modelData.path : (row.modelData.title || "New chat") + (row.pinned ? "\n" + row.modelData.project : "")
                    }
                    contentItem: RowLayout {
                        spacing: 7
                        Image {
                            visible: row.project || row.machine
                            source: "icons/chevron.svg"
                            sourceSize.width: 11
                            sourceSize.height: 11
                            rotation: row.modelData.expanded ? 90 : 0
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 3
                            Label {
                                objectName: row.modelData.kind === "chat" ? "sessionTitle_" + row.modelData.id : ""
                                Layout.fillWidth: true
                                text: row.modelData.title || "New chat"
                                elide: Text.ElideRight
                                maximumLineCount: 1
                                font.pixelSize: 12
                                font.weight: row.project || row.machine ? Font.Medium : Font.Normal
                                color: row.project ? sidebar.palette.placeholderText : sidebar.palette.text
                            }
                            Label {
                                objectName: row.pinned ? "pinnedProject_" + row.modelData.id : ""
                                Layout.fillWidth: true
                                visible: row.pinned || row.machine
                                text: row.machine ? (row.modelData.online ? "Connected" : "Offline") : row.modelData.project || ""
                                elide: Text.ElideRight
                                font.pixelSize: 10
                                color: sidebar.palette.placeholderText
                            }
                        }
                        Image {
                            visible: row.pinned
                            source: "icons/pin.svg"
                            sourceSize.width: 12
                            sourceSize.height: 12
                        }
                        Rectangle {
                            visible: !row.project && ["running", "pausing", "paused"].includes(row.modelData.id === sidebar.backend.chatId ? sidebar.backend.status : row.modelData.status || "")
                            implicitWidth: 5
                            implicitHeight: 5
                            radius: 3
                            color: "#4b9b70"
                        }
                    }
                    background: Rectangle {
                        radius: 8
                        color: rowButton.highlighted ? sidebar.palette.alternateBase : rowButton.hovered ? sidebar.palette.light : "transparent"
                    }
                    TapHandler {
                        acceptedButtons: Qt.RightButton
                        onTapped: function (point) {
                            if (row.machine)
                                sidebar.machinesRequested();
                            else if (row.project)
                                sidebar.manageProject(row.modelData, rowButton, point.position.x, point.position.y);
                            else
                                sidebar.manageChat(row.modelData, rowButton, point.position.x, point.position.y);
                        }
                    }
                }
                NativeButton {
                    objectName: row.section ? "" : row.project ? "projectMenu_" + row.modelData.id : "sessionMenu_" + row.modelData.id
                    visible: !row.section && !row.machine
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    implicitWidth: 26
                    implicitHeight: 26
                    icon.source: "icons/more.svg"
                    icon.width: 14
                    icon.height: 14
                    quiet: true
                    opacity: row.project || rowButton.highlighted || rowButton.hovered || hovered ? 1 : 0
                    enabled: !!row.modelData.online && !sidebar.backend.busy
                    tip: row.project ? "Project options" : "Conversation options"
                    onClicked: row.project ? sidebar.manageProject(row.modelData, rowButton) : sidebar.manageChat(row.modelData, rowButton)
                }
                NativeButton {
                    objectName: row.machine ? "addProject_" + row.modelData.id : row.project ? "newChat_" + row.modelData.id : ""
                    visible: row.project || row.machine
                    anchors.right: parent.right
                    anchors.rightMargin: row.machine ? 0 : 28
                    anchors.verticalCenter: parent.verticalCenter
                    implicitWidth: 26
                    implicitHeight: 26
                    icon.source: "icons/plus.svg"
                    icon.width: 14
                    icon.height: 14
                    quiet: true
                    enabled: !!row.modelData.online && !sidebar.backend.busy
                    tip: row.machine ? "Add project on " + row.modelData.title : "New chat in " + row.modelData.title
                    onClicked: {
                        if (row.machine) {
                            sidebar.backend.selectMachine(row.modelData.id);
                            sidebar.addProjectRequested();
                        } else sidebar.newConversation(row.modelData.id);
                    }
                }
            }
            Label {
                width: parent.width
                visible: sidebar.backend.projects.length === 0
                y: 40
                padding: 10
                text: "Add a project to get started."
                wrapMode: Text.WordWrap
                color: palette.placeholderText
                font.pixelSize: 12
            }
        }
        NativeButton {
            objectName: "machinesButton"
            text: "Machines"
            Layout.fillWidth: true
            quiet: true
            icon.source: "icons/machine.svg"
            onClicked: sidebar.machinesRequested()
        }
        NativeButton {
            objectName: "settingsButton"
            text: "Settings"
            Layout.fillWidth: true
            quiet: true
            onClicked: sidebar.settingsRequested()
            contentItem: RowLayout {
                spacing: 9
                Image {
                    source: "icons/settings.svg"
                    sourceSize.width: 16
                    sourceSize.height: 16
                }
                Label {
                    Layout.fillWidth: true
                    text: "Settings"
                    font.pixelSize: 13
                }
                Rectangle {
                    implicitWidth: 5
                    implicitHeight: 5
                    radius: 3
                    color: sidebar.backend.online ? "#4b9b70" : "#be8546"
                }
            }
            tip: sidebar.backend.connectionLabel
        }
        NativeButton {
            objectName: "reconnectBackendButton"
            text: "Reconnect"
            visible: !sidebar.backend.online && sidebar.backend.connectionLabel !== "Starting Ava…"
            onClicked: sidebar.backend.start()
        }
    }
    NativeMenu {
        id: projectMenu
        popupType: Popup.Item
        padding: 5
        width: 210
        NativeMenuItem {
            text: "New chat"
            enabled: !!sidebar.selectedProject.online && !sidebar.backend.busy
            onTriggered: sidebar.newConversation(sidebar.selectedProject.id)
        }
        NativeMenuItem {
            text: "New chat in a worktree…"
            enabled: !!sidebar.selectedProject.online && !sidebar.backend.busy
            onTriggered: {
                sidebar.conversationRequested();
                sidebar.backend.prepareWorktree(sidebar.selectedProject.id);
            }
        }
        MenuSeparator {}
        NativeMenuItem {
            objectName: "removeProjectAction"
            text: "Remove from Ava"
            enabled: !!sidebar.selectedProject.online
            onTriggered: sidebar.backend.removeProject(sidebar.selectedProject.id)
        }
    }
    Popup {
        id: removedNotice
        parent: Overlay.overlay
        property string projectName: ""
        property string projectPath: ""
        property string machineId: "local"
        width: Math.min(430, parent.width - 40)
        x: Math.round((parent.width - width) / 2)
        y: 76
        padding: 16
        closePolicy: Popup.CloseOnEscape
        background: Rectangle {
            radius: 12
            color: removedNotice.palette.base
            border.color: removedNotice.palette.mid
        }
        contentItem: RowLayout {
            spacing: 16
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 4
                Label {
                    objectName: "projectRemovedNotice"
                    Layout.fillWidth: true
                    text: removedNotice.projectName + " removed from Ava"
                    elide: Text.ElideRight
                    font.pixelSize: 13
                    font.weight: Font.Medium
                }
                Label {
                    Layout.fillWidth: true
                    text: "Files and chats are kept. Running tasks continue."
                    wrapMode: Text.WordWrap
                    font.pixelSize: 11
                    color: palette.placeholderText
                }
            }
            NativeButton {
                objectName: "undoRemoveProject"
                text: "Undo"
                enabled: sidebar.backend.machines.some(m => m.id === removedNotice.machineId && m.online)
                onClicked: {
                    sidebar.backend.selectMachine(removedNotice.machineId);
                    sidebar.backend.addProject(removedNotice.projectPath);
                    removedNotice.close();
                }
            }
        }
        onOpened: noticeTimer.restart()
        onClosed: noticeTimer.stop()
        Timer {
            id: noticeTimer
            interval: 8000
            onTriggered: removedNotice.close()
        }
    }
    Connections {
        target: sidebar.backend
        function onNavigationChanged() {
            if (removedNotice.visible && sidebar.backend.projects.some(p => p.machine === removedNotice.machineId && p.path === removedNotice.projectPath))
                removedNotice.close();
        }
        function onProjectRemoved(name, path, machine) {
            removedNotice.projectName = name;
            removedNotice.projectPath = path;
            removedNotice.machineId = machine;
            removedNotice.open();
            noticeTimer.restart();
        }
    }
    NativeMenu {
        id: chatMenu
        popupType: Popup.Item
        padding: 5
        width: 190
        NativeMenuItem {
            objectName: "pinChatAction"
            text: sidebar.selectedChat.pinned ? "Unpin chat" : "Pin chat"
            onTriggered: {
                const identity = sidebar.selectedChat.id;
                sidebar.backend.toggleChatPin(identity);
                Qt.callLater(sessions.revealChat, identity);
            }
        }
        NativeMenuItem {
            objectName: "renameChatAction"
            text: "Rename"
            onTriggered: {
                nameField.text = sidebar.selectedChat.title || "";
                renameDialog.open();
            }
        }
        MenuSeparator {}
        NativeMenuItem {
            objectName: "archiveChatAction"
            text: "Archive"
            onTriggered: sidebar.backend.archiveChat(sidebar.selectedChat.id, true)
        }
    }
    NativeDialog {
        id: renameDialog
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(430, parent.width - 40)
        modal: true
        title: "Rename chat"
        padding: 22
        onOpened: {
            nameField.forceActiveFocus();
            nameField.selectAll();
        }
        background: Rectangle {
            radius: 16
            color: renameDialog.palette.base
            border.color: renameDialog.palette.mid
        }
        ColumnLayout {
            width: parent.width
            spacing: 16
            NativeField {
                id: nameField
                objectName: "chatNameField"
                Layout.fillWidth: true
                maximumLength: 200
                placeholderText: "Conversation title"
                onAccepted: {
                    if (text.trim())
                        saveName.clicked();
                }
            }
            RowLayout {
                Layout.fillWidth: true
                Item {
                    Layout.fillWidth: true
                }
                NativeButton {
                    text: "Cancel"
                    onClicked: renameDialog.close()
                }
                NativeButton {
                    id: saveName
                    objectName: "saveChatName"
                    text: "Save"
                    primary: true
                    enabled: !!nameField.text.trim()
                    onClicked: {
                        sidebar.backend.renameChat(sidebar.selectedChat.id, nameField.text);
                        renameDialog.close();
                    }
                }
            }
        }
    }
    NativeDialog {
        id: searchDialog
        property bool archivedOnly: false
        property var results: []
        function refresh() {
            results = sidebar.backend.searchChats(searchField.text, archivedOnly);
            resultsView.currentIndex = 0;
        }
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(610, parent.width - 50)
        modal: true
        title: archivedOnly ? "Archived chats" : "Search chats"
        padding: 20
        onOpened: {
            refresh();
            searchField.forceActiveFocus();
        }
        onArchivedOnlyChanged: refresh()
        background: Rectangle {
            radius: 18
            color: searchDialog.palette.base
            border.color: searchDialog.palette.mid
        }
        Connections {
            target: sidebar.backend
            function onNavigationChanged() {
                if (searchDialog.visible)
                    searchDialog.refresh();
            }
        }
        ColumnLayout {
            width: parent.width
            spacing: 12
            NativeField {
                id: searchField
                objectName: "chatSearchField"
                Layout.fillWidth: true
                placeholderText: "Search conversations or projects"
                onTextChanged: searchDialog.refresh()
                Keys.onPressed: function (event) {
                    if (event.key === Qt.Key_Down || event.key === Qt.Key_Up) {
                        resultsView.currentIndex = Math.max(0, Math.min(resultsView.count - 1, resultsView.currentIndex + (event.key === Qt.Key_Down ? 1 : -1)));
                        resultsView.positionViewAtIndex(resultsView.currentIndex, ListView.Contain);
                        event.accepted = true;
                    } else if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter) && resultsView.count && !searchDialog.archivedOnly) {
                        sidebar.openConversation(searchDialog.results[resultsView.currentIndex].id);
                        searchDialog.close();
                        event.accepted = true;
                    }
                }
            }
            ListView {
                id: resultsView
                objectName: "chatSearchResults"
                Layout.fillWidth: true
                Layout.preferredHeight: 300
                model: searchDialog.results
                clip: true
                reuseItems: true
                currentIndex: 0
                ScrollBar.vertical: ScrollBar {}
                delegate: ItemDelegate {
                    id: result
                    required property var modelData
                    required property int index
                    objectName: "searchChat_" + modelData.id
                    width: ListView.view.width
                    height: 54
                    highlighted: index === resultsView.currentIndex
                    background: Rectangle {
                        radius: 9
                        color: result.highlighted || result.hovered ? searchDialog.palette.light : "transparent"
                    }
                    contentItem: RowLayout {
                        spacing: 12
                        Image {
                            source: "icons/chat.svg"
                            sourceSize.width: 17
                            sourceSize.height: 17
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 3
                            Label {
                                Layout.fillWidth: true
                                text: result.modelData.title || "New chat"
                                elide: Text.ElideRight
                                font.pixelSize: 13
                            }
                            Label {
                                Layout.fillWidth: true
                                text: result.modelData.project
                                font.pixelSize: 11
                                color: palette.placeholderText
                                elide: Text.ElideRight
                            }
                        }
                        NativeButton {
                            objectName: "restoreChat_" + result.modelData.id
                            visible: searchDialog.archivedOnly
                            text: "Restore"
                            implicitHeight: 28
                            onClicked: sidebar.backend.archiveChat(result.modelData.id, false)
                        }
                    }
                    onClicked: {
                        if (!searchDialog.archivedOnly) {
                            sidebar.openConversation(modelData.id);
                            searchDialog.close();
                        }
                    }
                }
                Label {
                    anchors.centerIn: parent
                    visible: resultsView.count === 0
                    text: searchField.text ? "No matching chats" : searchDialog.archivedOnly ? "No archived chats" : "No conversations yet"
                    font.pixelSize: 13
                    color: palette.placeholderText
                }
            }
            RowLayout {
                Layout.fillWidth: true
                NativeButton {
                    objectName: "searchArchivedToggle"
                    text: searchDialog.archivedOnly ? "Show active chats" : "Archived chats"
                    quiet: true
                    onClicked: searchDialog.archivedOnly = !searchDialog.archivedOnly
                }
                Item {
                    Layout.fillWidth: true
                }
                NativeButton {
                    objectName: "closeChatSearch"
                    text: "Done"
                    onClicked: searchDialog.close()
                }
            }
        }
    }
}
