pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

NativeDialog {
    id: dialog
    required property var servers
    property var draft: ({})
    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(660, parent.width - 40)
    height: Math.min(710, parent.height - 40)
    modal: true
    title: draft.id ? "Edit MCP server" : "Add MCP server"
    closePolicy: servers.saving ? Popup.NoAutoClose : Popup.CloseOnEscape
    ListModel { id: credentials }
    Connections {
        target: dialog.servers
        function onEditRequested(data) {
            dialog.draft = data;
            name.text = data.name || "";
            transport.currentIndex = data.transport === "http" ? 1 : 0;
            command.text = data.command_line || "";
            address.text = data.url || "";
            credentials.clear();
            const keys = transport.currentIndex ? data.header_names : data.env_names;
            if (keys) for (let key of keys) credentials.append({key: key, value: "", dirty: false});
            dialog.open();
        }
        function onSaved() { dialog.close(); }
    }
    onOpened: name.forceActiveFocus()
    function save() {
        let values = [];
        for (let i = 0; i < credentials.count; i++) {
            const entry = credentials.get(i);
            values.push({name: entry.key.trim(), value: entry.dirty ? entry.value : null});
        }
        servers.save({id: draft.id || "", version: draft.version || 0, name: name.text.trim(), transport: transport.currentIndex ? "http" : "stdio", command_line: command.text.trim(), url: address.text.trim(), credentials: values});
    }
    contentItem: ColumnLayout {
        spacing: 14
        ScrollView {
            id: scroll
            Layout.fillWidth: true
            Layout.fillHeight: true
            contentWidth: availableWidth
            clip: true
            ColumnLayout {
                width: scroll.availableWidth
                spacing: 8
                Label { text: "Name"; font.weight: Font.DemiBold }
                NativeField { id: name; objectName: "mcpNameField"; Layout.fillWidth: true; placeholderText: "Workspace tools"; maximumLength: 80; enabled: !dialog.servers.saving }
                Label { text: "Connection"; font.weight: Font.DemiBold }
                NativeCombo { id: transport; objectName: "mcpTransportChoice"; Layout.fillWidth: true; model: ["Local command · stdio", "HTTP endpoint"]; enabled: !dialog.servers.saving; onActivated: credentials.clear() }
                Label { text: "Command"; visible: !transport.currentIndex; font.weight: Font.DemiBold }
                NativeField { id: command; objectName: "mcpCommandField"; Layout.fillWidth: true; visible: !transport.currentIndex; placeholderText: "uvx your-mcp-server"; enabled: !dialog.servers.saving }
                Label { text: "Program and arguments, with quotes around paths containing spaces. Runs on the selected machine, in the selected workspace."; visible: !transport.currentIndex; Layout.fillWidth: true; wrapMode: Text.Wrap; font.pixelSize: 11; color: palette.placeholderText }
                Label { text: "MCP URL"; visible: !!transport.currentIndex; font.weight: Font.DemiBold }
                NativeField { id: address; objectName: "mcpUrlField"; Layout.fillWidth: true; visible: !!transport.currentIndex; placeholderText: "https://example.com/mcp"; enabled: !dialog.servers.saving }
                Label { text: transport.currentIndex ? "Headers" : "Environment variables"; font.weight: Font.DemiBold; Layout.topMargin: 12 }
                Label { text: "Optional. Saved values stay hidden; leave them unchanged to keep them."; Layout.fillWidth: true; wrapMode: Text.Wrap; font.pixelSize: 11; color: palette.placeholderText }
                Repeater {
                    model: credentials
                    delegate: RowLayout {
                        id: credential
                        required property int index
                        required property string key
                        required property string value
                        required property bool dirty
                        Layout.fillWidth: true
                        NativeField { objectName: "mcpCredentialName_" + credential.index; Layout.fillWidth: true; text: credential.key; placeholderText: transport.currentIndex ? "Authorization" : "API_KEY"; enabled: !dialog.servers.saving; onTextEdited: credentials.setProperty(credential.index, "key", text) }
                        NativeField { objectName: "mcpCredentialValue_" + credential.index; Layout.fillWidth: true; text: credential.value; placeholderText: credential.dirty ? "Value" : "Saved value"; echoMode: TextInput.Password; enabled: !dialog.servers.saving; onTextEdited: { credentials.setProperty(credential.index, "value", text); credentials.setProperty(credential.index, "dirty", true); } }
                        NativeButton { icon.source: "icons/close.svg"; quiet: true; tip: "Remove credential"; enabled: !dialog.servers.saving; onClicked: credentials.remove(credential.index) }
                    }
                }
                NativeButton { objectName: "addMcpCredentialButton"; text: transport.currentIndex ? "+ Add header" : "+ Add variable"; quiet: true; enabled: !dialog.servers.saving; onClicked: credentials.append({key: "", value: "", dirty: true}) }
                Item { Layout.fillHeight: true; Layout.minimumHeight: 12 }
                Label { Layout.fillWidth: true; text: "Adding an enabled server connects it now. Ava also connects enabled servers when an agent starts work in another workspace."; wrapMode: Text.Wrap; color: palette.placeholderText; font.pixelSize: 12 }
            }
        }
        Label { Layout.fillWidth: true; visible: !!text; text: dialog.servers.editorError; wrapMode: Text.Wrap; color: palette.link }
        RowLayout {
            Item { Layout.fillWidth: true }
            NativeButton { text: "Cancel"; enabled: !dialog.servers.saving; onClicked: dialog.close() }
            NativeButton { objectName: "saveMcpButton"; text: dialog.servers.saving ? "Saving…" : dialog.draft.id ? "Save" : "Add and connect"; enabled: !dialog.servers.saving && !!name.text.trim() && (transport.currentIndex ? !!address.text.trim() : !!command.text.trim()); onClicked: dialog.save() }
        }
    }
}
