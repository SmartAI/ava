pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Pane {
    id: inspector
    required property var backend
    required property string codeFont
    property int currentTab: 0
    property int nextId: 2
    property string lastProject: ""
    readonly property Loader currentLoader: {
        contents.count;
        return contents.itemAt(currentTab) as Loader;
    }
    readonly property var activePane: currentLoader ? currentLoader.item : null
    signal closed
    padding: 10
    enabled: !!backend
    background: Rectangle {
        color: Theme.inset
    }
    ListModel {
        id: tabs
        ListElement {
            identity: 0
            kind: "files"
            title: "Files"
            root: ""
            project: ""
            path: ""
            started: false
        }
        ListElement {
            identity: 1
            kind: "browser"
            title: "Browser"
            root: ""
            project: ""
            path: ""
            started: false
        }
    }
    function selectTab(index) {
        if (index < 0 || index >= tabs.count)
            return;
        tabs.setProperty(index, "started", true);
        currentTab = index;
    }
    function addTab(kind) {
        tabs.append({
            identity: nextId++,
            kind: kind,
            title: kind === "files" ? "Files" : kind === "changes" ? "Changes" : "Browser",
            root: kind !== "browser" ? backend.workspacePath : "",
            project: kind !== "browser" ? backend.projectId : "",
            path: "",
            started: true
        });
        selectTab(tabs.count - 1);
    }
    function closeTab(index) {
        tabs.remove(index);
        currentTab = Math.max(0, Math.min(currentTab - (index < currentTab ? 1 : 0), tabs.count - 1));
        if (tabs.count)
            selectTab(currentTab);
    }
    function selectKind(kind) {
        if (kind === "files") {
            showFiles(backend.workspacePath, "", backend.projectId);
            return;
        }
        if (kind === "changes") {
            for (let i = 0; i < tabs.count; ++i)
                if (tabs.get(i).kind === kind && tabs.get(i).project === backend.projectId && tabs.get(i).root === backend.workspacePath) {
                    selectTab(i);
                    return;
                }
            addTab(kind);
            return;
        }
        if (tabs.count && tabs.get(currentTab).kind === kind) {
            selectTab(currentTab);
            return;
        }
        for (let i = 0; i < tabs.count; ++i)
            if (tabs.get(i).kind === kind) {
                selectTab(i);
                return;
            }
        addTab(kind);
    }
    function showBrowser(url) {
        selectKind("browser");
        tabs.setProperty(currentTab, "path", url);
        if (activePane)
            activePane.requestedUrl = url;
    }
    function showFiles(root, path, project) {
        let index = -1;
        if (tabs.count && tabs.get(currentTab).kind === "files" && tabs.get(currentTab).project === project && tabs.get(currentTab).root === root)
            index = currentTab;
        if (index < 0)
            for (let i = 0; i < tabs.count; ++i)
                if (tabs.get(i).kind === "files" && (!tabs.get(i).root || (tabs.get(i).project === project && tabs.get(i).root === root))) {
                    index = i;
                    break;
                }
        if (index < 0) {
            addTab("files");
            index = currentTab;
        }
        tabs.setProperty(index, "root", root);
        tabs.setProperty(index, "project", project);
        tabs.setProperty(index, "path", path);
        selectTab(index);
        if (activePane && path)
            activePane.openPath(path);
    }
    function followProject() {
        if (!visible || !backend || backend.busy || !backend.workspacePath || backend.projectId + "\n" + backend.workspacePath === lastProject)
            return;
        lastProject = backend.projectId + "\n" + backend.workspacePath;
        if (tabs.count && tabs.get(currentTab).kind === "files")
            showFiles(backend.workspacePath, "", backend.projectId);
    }
    Component.onCompleted: followProject()
    onVisibleChanged: {
        if (visible)
            followProject();
    }
    Connections {
        target: inspector.backend
        function onChanged() {
            inspector.followProject();
        }
    }
    ColumnLayout {
        anchors.fill: parent
        spacing: 10
        RowLayout {
            Layout.fillWidth: true
            spacing: 4
            ListView {
                id: tabStrip
                objectName: "inspectorTabs"
                Layout.fillWidth: true
                implicitHeight: 34
                orientation: ListView.Horizontal
                clip: true
                spacing: 3
                model: tabs
                currentIndex: inspector.currentTab
                highlightMoveDuration: 0
                highlightRangeMode: ListView.ApplyRange
                delegate: RowLayout {
                    id: tabItem
                    width: implicitWidth
                    height: 34
                    required property int index
                    required property int identity
                    required property string title
                    required property string kind
                    required property string project
                    required property string root
                    spacing: 0
                    NativeButton {
                        objectName: tabItem.identity === 0 ? "filesTab" : tabItem.identity === 1 ? "browserTab" : "inspectorTab_" + tabItem.identity
                        text: tabItem.title
                        Layout.maximumWidth: 155
                        icon.source: tabItem.kind === "files" ? "icons/folder.svg" : tabItem.kind === "changes" ? "icons/changes.svg" : "icons/globe.svg"
                        quiet: true
                        selected: inspector.currentTab === tabItem.index
                        onClicked: inspector.selectTab(tabItem.index)
                        tip: tabItem.title + (tabItem.project ? "\n" + inspector.backend.workspaceLabel(tabItem.project, tabItem.root) : "")
                    }
                    NativeButton {
                        objectName: "closeInspectorTab_" + tabItem.identity
                        implicitWidth: 24
                        implicitHeight: 28
                        icon.width: 12
                        icon.height: 12
                        icon.source: "icons/close.svg"
                        quiet: true
                        tip: "Close tab"
                        onClicked: inspector.closeTab(tabItem.index)
                    }
                }
            }
            NativeButton {
                objectName: "addInspectorTab"
                icon.source: "icons/plus.svg"
                quiet: true
                tip: "New tab"
                onClicked: addMenu.open()
                NativeMenu {
                    id: addMenu
                    popupType: Popup.Item
                    y: parent.height
                    NativeMenuItem {
                        objectName: "newFilesTab"
                        text: "File explorer"
                        enabled: !!inspector.backend && !!inspector.backend.workspacePath
                        onTriggered: inspector.addTab("files")
                    }
                    NativeMenuItem {
                        objectName: "newChangesTab"
                        text: "Changes"
                        enabled: !!inspector.backend && !!inspector.backend.workspacePath
                        onTriggered: inspector.addTab("changes")
                    }
                    NativeMenuItem {
                        objectName: "newBrowserTab"
                        text: "Browser"
                        onTriggered: inspector.addTab("browser")
                    }
                }
            }
            NativeButton {
                objectName: "closeInspectorButton"
                icon.source: "icons/close.svg"
                quiet: true
                tip: "Hide inspector"
                onClicked: inspector.closed()
            }
        }
        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Repeater {
                id: contents
                model: tabs
                delegate: Loader {
                    id: tabContent
                    required property int index
                    required property int identity
                    required property string kind
                    required property string root
                    required property string project
                    required property string path
                    required property bool started
                    anchors.fill: parent
                    visible: index === inspector.currentTab
                    active: started && !!inspector.backend && (kind === "browser" || !!root)
                    sourceComponent: kind === "files" ? fileComponent : kind === "changes" ? reviewComponent : browserComponent
                    Component {
                        id: reviewComponent
                        ReviewPane {
                            backend: inspector.backend
                            codeFont: inspector.codeFont
                            rootPath: tabContent.root
                            projectId: tabContent.project
                        }
                    }
                    Component {
                        id: fileComponent
                        FilePane {
                            backend: inspector.backend
                            codeFont: inspector.codeFont
                            rootPath: tabContent.root
                            projectId: tabContent.project
                            requestedPath: tabContent.path
                            onTitleUpdated: function (title) {
                                tabs.setProperty(tabContent.index, "title", title);
                            }
                        }
                    }
                    Component {
                        id: browserComponent
                        BrowserPane {
                            backend: inspector.backend
                            tabId: tabContent.identity
                            session: inspector.backend ? inspector.backend.browserSession : null
                            requestedUrl: tabContent.path
                            onTitleUpdated: function (title) {
                                tabs.setProperty(tabContent.index, "title", title);
                            }
                            onNewTabRequested: function (url) {
                                inspector.addTab("browser");
                                tabs.setProperty(inspector.currentTab, "path", url);
                            }
                        }
                    }
                }
            }
            Label {
                anchors.centerIn: parent
                visible: tabs.count === 0
                text: "Open a file explorer or browser with +"
                font.pixelSize: 12
                color: palette.placeholderText
            }
        }
    }
}
