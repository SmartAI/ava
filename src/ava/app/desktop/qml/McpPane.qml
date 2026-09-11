pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Pane {
    id: page
    required property var backend
    required property bool sidebarVisible
    required property string codeFont
    readonly property var servers: backend.mcpView
    readonly property var detail: servers.detail
    readonly property bool narrow: width < 740
    signal closeRequested
    signal sidebarRequested
    padding: narrow ? Theme.spaceLg : Theme.spaceXl
    background: Rectangle { color: page.palette.window }
    Component.onCompleted: servers.activate(true)
    Component.onDestruction: { if (servers) servers.activate(false); }
    function status(value) { return ({idle: "Ready to connect", connecting: "Connecting…", connected: "Connected", disconnected: "Disconnected", disabled: "Disabled", error: "Connection failed"})[value] || ""; }
    ColumnLayout {
        anchors.fill: parent
        spacing: 16
        PageHeader {
            Layout.fillWidth: true
            title: "MCP servers"
            description: "Connect Ava to the tools you use."
            sidebarVisible: page.sidebarVisible
            onSidebarRequested: page.sidebarRequested()
            NativeButton { objectName: "refreshMcpButton"; icon.source: "icons/reload.svg"; quiet: true; tip: "Refresh servers"; enabled: page.servers.available && !page.servers.loading; onClicked: page.servers.refresh() }
            NativeButton { objectName: "addMcpButton"; text: "Add server"; primary: true; enabled: page.servers.available && !page.servers.saving; onClicked: page.servers.edit(false) }
            NativeButton { objectName: "closeMcpButton"; text: "Done"; quiet: true; onClicked: page.closeRequested() }
        }
        NativeCombo {
            objectName: "mcpProjectChoice"
            Layout.fillWidth: true
            model: page.servers.projects
            textRole: "name"
            valueRole: "id"
            currentIndex: model.findIndex(item => item.id === page.servers.project)
            enabled: !page.servers.saving
            onActivated: page.servers.chooseProject(currentValue)
        }
        Label { Layout.fillWidth: true; visible: !!text; text: page.servers.error || (!page.servers.available ? "Connect or update this machine to manage MCP servers." : ""); wrapMode: Text.Wrap; color: palette.link }
        SplitView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            handle: Rectangle { implicitWidth: 9; color: "transparent"; Rectangle { width: 1; height: parent.height; anchors.centerIn: parent; color: page.palette.mid } }
            ColumnLayout {
                visible: !page.narrow || !page.servers.selected
                SplitView.preferredWidth: 290
                SplitView.minimumWidth: 220
                SplitView.fillWidth: page.narrow
                spacing: 12
                NativeField { objectName: "mcpSearch"; Layout.fillWidth: true; placeholderText: "Search servers"; onTextEdited: page.servers.filter(text) }
                ListView {
                    id: list
                    objectName: "mcpServerList"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    reuseItems: true
                    cacheBuffer: 100
                    spacing: 6
                    model: page.servers.rows
                    ScrollBar.vertical: ScrollBar {}
                    EmptyState { anchors.centerIn: parent; width: parent.width - 24; visible: !list.count; title: page.servers.loading ? "Loading servers…" : "Connect Ava to your tools"; description: page.servers.loading ? "" : "Add an MCP server to get started." }
                    delegate: ItemDelegate {
                        id: row
                        required property var entry
                        objectName: "mcpServer_" + entry.id
                        width: list.width
                        height: 88
                        padding: 12
                        onClicked: page.servers.select(entry.id)
                        background: Surface { border.width: 0; focused: row.visualFocus; color: page.servers.selected === row.entry.id ? Theme.selection : row.hovered ? Theme.hover : "transparent" }
                        contentItem: ColumnLayout {
                            spacing: 6
                            Label { text: row.entry.name; font.weight: Font.DemiBold; Layout.fillWidth: true; elide: Text.ElideRight }
                            Label { text: page.status(row.entry.status); color: row.entry.status === "connected" ? Theme.success : palette.placeholderText; font.pixelSize: 12 }
                            Label { text: (row.entry.transport === "stdio" ? "Local process" : "HTTP") + " · " + row.entry.tool_count + " tools"; color: palette.placeholderText; font.pixelSize: 11 }
                        }
                    }
                }
            }
            ColumnLayout {
                visible: !page.narrow || !!page.servers.selected
                SplitView.fillWidth: true
                SplitView.minimumWidth: 300
                spacing: 12
                NativeButton { visible: page.narrow; text: "‹ All servers"; quiet: true; onClicked: page.servers.select("") }
                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.leftMargin: page.narrow ? 0 : 16
                    spacing: 10
                    Label { text: page.detail.name || "Choose a server"; Layout.fillWidth: true; font.pixelSize: 21; font.weight: Font.DemiBold; wrapMode: Text.Wrap }
                    Label { text: page.detail.id ? page.status(page.detail.status) + (page.detail.active_calls ? " · " + page.detail.active_calls + " active calls" : "") : "MCP servers give Ava additional tools for your work."; Layout.fillWidth: true; wrapMode: Text.Wrap; color: palette.placeholderText }
                    Label { visible: !!page.detail.id; text: page.detail.command_line || page.detail.url || ""; Layout.fillWidth: true; font.pixelSize: 12; maximumLineCount: 3; wrapMode: Text.WrapAnywhere; elide: Text.ElideRight; color: palette.placeholderText }
                    RowLayout {
                        visible: !!page.detail.error
                        Layout.fillWidth: true
                        Label { text: page.detail.error || ""; Layout.fillWidth: true; maximumLineCount: 2; elide: Text.ElideRight; wrapMode: Text.WrapAnywhere; color: palette.link }
                        NativeButton { objectName: "mcpErrorDetailsButton"; text: "Details…"; quiet: true; onClicked: { schemaDialog.title = "Connection error"; schemaDialog.body = page.detail.error; schemaDialog.open(); } }
                    }
                    Flow {
                        visible: !!page.detail.id
                        Layout.fillWidth: true
                        spacing: 8
                        NativeButton { objectName: "connectMcpButton"; text: page.detail.status === "error" ? "Retry connection" : "Connect"; visible: !!page.detail.enabled && page.detail.status !== "connected"; enabled: !page.servers.saving && page.servers.available; onClicked: page.servers.action("connect") }
                        NativeButton { objectName: "refreshMcpToolsButton"; text: "Refresh tools"; visible: page.detail.status === "connected"; enabled: !page.servers.saving && page.servers.available; onClicked: page.servers.action("refresh") }
                        NativeButton { objectName: "toggleMcpButton"; text: page.detail.enabled ? "Disable" : "Enable"; enabled: !page.servers.saving && page.servers.available; onClicked: page.servers.action("toggle") }
                        NativeButton { objectName: "editMcpButton"; text: "Edit…"; enabled: !page.servers.saving && page.servers.available; onClicked: page.servers.edit(true) }
                        NativeButton { objectName: "removeMcpButton"; text: "Remove…"; quiet: true; enabled: !page.servers.saving && page.servers.available; onClicked: removeDialog.open() }
                    }
                    Label { visible: !!page.detail.id; Layout.fillWidth: true; text: "Enabled servers are available to projects on this machine. Local processes run in the selected workspace. Disabling stops new calls; calls already running may finish."; wrapMode: Text.Wrap; font.pixelSize: 11; color: palette.placeholderText }
                    NativeField { visible: !!page.detail.id; objectName: "mcpToolSearch"; Layout.fillWidth: true; placeholderText: "Search tools"; onTextEdited: page.servers.filterTools(text) }
                }
                ListView {
                    id: tools
                    objectName: "mcpToolList"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.leftMargin: page.narrow ? 0 : 16
                    clip: true
                    reuseItems: true
                    cacheBuffer: 100
                    spacing: 8
                    model: page.servers.toolRows
                    ScrollBar.vertical: ScrollBar {}
                    delegate: ItemDelegate {
                        id: tool
                        required property var entry
                        objectName: "mcpTool_" + entry.name
                        width: tools.width
                        height: Math.max(90, contentItem.implicitHeight + 24)
                        padding: 12
                        onClicked: { page.servers.showSchema(entry.name); schemaDialog.title = entry.name; schemaDialog.body = page.servers.schema; schemaDialog.open(); }
                        background: Rectangle { radius: 10; color: tool.hovered ? page.palette.alternateBase : "transparent"; border.color: page.palette.mid }
                        contentItem: ColumnLayout {
                            spacing: 6
                            Label { text: tool.entry.title || tool.entry.name; font.weight: Font.DemiBold; Layout.fillWidth: true; wrapMode: Text.Wrap }
                            Label { text: tool.entry.description || "No description provided."; Layout.fillWidth: true; wrapMode: Text.Wrap; maximumLineCount: 3; elide: Text.ElideRight; color: palette.placeholderText; font.pixelSize: 12 }
                            Label { text: "View parameters ›"; color: palette.link; font.pixelSize: 11 }
                        }
                    }
                }
            }
        }
    }
    McpDialog { id: editor; servers: page.servers }
    NativeDialog {
        id: removeDialog
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(450, parent.width - 40)
        modal: true
        padding: 22
        title: "Remove this MCP server?"
        contentItem: ColumnLayout {
            spacing: 16
            Label { Layout.fillWidth: true; text: "This removes its configuration and stored credentials from Ava on this machine. Its installed program and files remain. Calls already running may finish."; wrapMode: Text.Wrap }
            RowLayout { Item { Layout.fillWidth: true } NativeButton { text: "Cancel"; onClicked: removeDialog.close() } NativeButton { objectName: "confirmRemoveMcpButton"; text: "Remove"; onClicked: { page.servers.action("remove"); removeDialog.close(); } } }
        }
    }
    NativeDialog {
        id: schemaDialog
        property string body: ""
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(640, parent.width - 40)
        height: Math.min(600, parent.height - 40)
        modal: true
        padding: 22
        contentItem: ColumnLayout {
            ScrollView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                TextArea {
                    id: schemaText
                    objectName: "mcpSchemaPreview"
                    readOnly: true
                    selectByMouse: true
                    text: schemaDialog.body
                    wrapMode: TextEdit.WrapAnywhere
                    font.family: page.codeFont
                    font.pixelSize: 12
                    ContextMenu.menu: TextMenu { editor: schemaText }
                    background: null
                }
            }
            NativeButton { text: "Done"; Layout.alignment: Qt.AlignRight; onClicked: schemaDialog.close() }
        }
    }
}
