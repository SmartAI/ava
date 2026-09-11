pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Pane {
    id: dock
    objectName: "terminalDock"
    required property var backend
    required property string codeFont
    property int currentTab: 0
    property int nextId: 0
    readonly property var activePane: {
        panes.count;
        return panes.itemAt(currentTab);
    }
    signal hideRequested
    padding: 0
    background: Rectangle { color: Theme.inset }
    ListModel {
        id: tabs
    }
    function addTerminal() {
        if (!backend.workspacePath)
            return;
        tabs.append({
            identity: nextId++,
            root: backend.workspacePath,
            project: backend.projectId
        });
        currentTab = tabs.count - 1;
    }
    function open() {
        let match = tabs.count && tabs.get(currentTab).project === backend.projectId && tabs.get(currentTab).root === backend.workspacePath ? currentTab : -1;
        for (let i = 0; i < tabs.count; ++i)
            if (match < 0 && tabs.get(i).project === backend.projectId && tabs.get(i).root === backend.workspacePath) match = i;
        if (match >= 0)
            currentTab = match;
        else
            addTerminal();
        if (activePane)
            activePane.focusTerminal();
    }
    function closeTab(identity) {
        let index = -1;
        for (let i = 0; i < tabs.count; ++i)
            if (tabs.get(i).identity === identity) { index = i; break; }
        if (index < 0)
            return;
        const wasCurrent = index === currentTab;
        tabs.remove(index);
        currentTab = Math.max(0, Math.min(currentTab - (index < currentTab ? 1 : 0), tabs.count - 1));
        if (!tabs.count)
            hideRequested();
        else if (wasCurrent && visible && activePane)
            Qt.callLater(activePane.focusTerminal);
    }
    ColumnLayout {
        anchors.fill: parent
        spacing: 0
        RowLayout {
            Layout.fillWidth: true
            Layout.leftMargin: 10
            Layout.rightMargin: 8
            spacing: 3
            ListView {
                id: tabStrip
                Layout.fillWidth: true
                implicitHeight: 36
                orientation: ListView.Horizontal
                clip: true
                spacing: 4
                model: tabs
                currentIndex: dock.currentTab
                highlightRangeMode: ListView.ApplyRange
                highlightMoveDuration: 0
                delegate: RowLayout {
                    id: tab
                    required property int index
                    required property int identity
                    required property string root
                    required property string project
                    width: implicitWidth
                    height: 36
                    spacing: 0
                    NativeButton {
                        objectName: "terminalTab_" + tab.identity
                        text: ((dock.backend.machines.length > 1 || tab.root !== dock.backend.projectPath) ? dock.backend.workspaceLabel(tab.project, tab.root) : tab.root.split("/").pop()) + " · " + (tab.identity + 1)
                        icon.source: "icons/terminal.svg"
                        implicitHeight: 30
                        Layout.maximumWidth: (dock.backend.machines.length > 1 || tab.root !== dock.backend.projectPath) ? 260 : 180
                        quiet: true
                        selected: dock.currentTab === tab.index
                        tip: dock.backend.workspaceLabel(tab.project, tab.root) + "\n" + tab.root
                        onClicked: {
                            dock.currentTab = tab.index;
                            if (dock.activePane)
                                dock.activePane.focusTerminal();
                        }
                    }
                    NativeButton {
                        objectName: "closeTerminalTab_" + tab.identity
                        implicitWidth: 22
                        implicitHeight: 28
                        icon.source: "icons/close.svg"
                        icon.width: 12
                        icon.height: 12
                        quiet: true
                        tip: "Close terminal"
                        onClicked: dock.closeTab(tab.identity)
                    }
                }
            }
            NativeButton {
                objectName: "newTerminalButton"
                icon.source: "icons/plus.svg"
                quiet: true
                tip: "New terminal in current project"
                onClicked: dock.addTerminal()
            }
            NativeButton {
                objectName: "hideTerminalButton"
                icon.source: "icons/down.svg"
                quiet: true
                tip: "Hide terminal"
                onClicked: dock.hideRequested()
            }
        }
        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Repeater {
                id: panes
                model: tabs
                delegate: TerminalPane {
                    required property int index
                    required property int identity
                    required property string root
                    required property string project
                    anchors.fill: parent
                    visible: index === dock.currentTab
                    backend: dock.backend
                    codeFont: dock.codeFont
                    rootPath: root
                    projectId: project
                    onExitRequested: Qt.callLater(dock.closeTab, identity)
                }
            }
        }
    }
}
