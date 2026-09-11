pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

NativeDialog {
    id: dialog
    required property var servers
    property var draft: ({})
    readonly property bool google: address.text.trim().indexOf("https://gmailmcp.googleapis.com/") === 0
    property bool secretChanged: false
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
            authChoice.currentIndex = data.oauth ? 1 : 0;
            clientId.text = data.oauth ? data.oauth.client_id : "";
            clientSecret.text = "";
            issuer.text = data.oauth ? data.oauth.issuer : "";
            scope.text = data.oauth ? data.oauth.scope : "";
            redirect.text = data.oauth ? data.oauth.redirect_uri : "http://127.0.0.1:8766/oauth/callback";
            dialog.secretChanged = false;
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
        const oauth = transport.currentIndex && authChoice.currentIndex ? {
            client_id: clientId.text.trim(),
            client_secret: clientSecret.text || dialog.secretChanged ? clientSecret.text : (draft.oauth && draft.oauth.has_client_secret ? null : ""),
            issuer: issuer.text.trim(), scope: scope.text.trim(), redirect_uri: redirect.text.trim()
        } : null;
        servers.save({id: draft.id || "", version: draft.version || 0, name: name.text.trim(), transport: transport.currentIndex ? "http" : "stdio", command_line: command.text.trim(), url: address.text.trim(), credentials: values, oauth: oauth});
    }
    contentItem: ColumnLayout {
        spacing: Theme.spaceLg
        ScrollView {
            id: scroll
            Layout.fillWidth: true
            Layout.fillHeight: true
            contentWidth: availableWidth
            contentHeight: form.implicitHeight + Theme.spaceSm
            clip: true
            ColumnLayout {
                id: form
                x: Theme.spaceSm
                y: Theme.spaceXs
                width: scroll.availableWidth - Theme.spaceSm * 2
                spacing: 8
                Label { text: "Name"; font.weight: Font.DemiBold }
                NativeField { id: name; objectName: "mcpNameField"; Layout.fillWidth: true; placeholderText: "Workspace tools"; maximumLength: 80; enabled: !dialog.servers.saving }
                Label { text: "Connection"; font.weight: Font.DemiBold }
                NativeCombo { id: transport; objectName: "mcpTransportChoice"; Layout.fillWidth: true; model: ["Local command · stdio", "HTTP endpoint"]; enabled: !dialog.servers.saving; onActivated: credentials.clear() }
                Label { text: "Command"; visible: !transport.currentIndex; font.weight: Font.DemiBold }
                NativeField { id: command; objectName: "mcpCommandField"; Layout.fillWidth: true; visible: !transport.currentIndex; placeholderText: "uvx your-mcp-server"; enabled: !dialog.servers.saving }
                Label { text: "Program and arguments, with quotes around paths containing spaces. Runs on the selected machine, in the selected workspace."; visible: !transport.currentIndex; Layout.fillWidth: true; wrapMode: Text.Wrap; font.pixelSize: 11; color: palette.placeholderText }
                Label { text: "MCP URL"; visible: !!transport.currentIndex; font.weight: Font.DemiBold }
                NativeField { id: address; objectName: "mcpUrlField"; Layout.fillWidth: true; visible: !!transport.currentIndex; placeholderText: "https://example.com/mcp"; enabled: !dialog.servers.saving; onEditingFinished: { if (dialog.google && dialog.servers.oauthAvailable) authChoice.currentIndex = 1; } }
                Label { text: "Authentication"; visible: !!transport.currentIndex; font.weight: Font.DemiBold; Layout.topMargin: 8 }
                NativeCombo {
                    id: authChoice
                    objectName: "mcpAuthChoice"
                    Layout.fillWidth: true
                    visible: !!transport.currentIndex
                    model: ["None / manual headers", "OAuth · sign in with your browser"]
                    enabled: !dialog.servers.saving && dialog.servers.oauthAvailable
                    onCurrentIndexChanged: {
                        if (currentIndex && dialog.google) {
                            if (!issuer.text) issuer.text = "https://accounts.google.com";
                            if (!scope.text) scope.text = "https://www.googleapis.com/auth/gmail.readonly";
                        }
                    }
                }
                Label { visible: !!transport.currentIndex && !dialog.servers.oauthAvailable; Layout.fillWidth: true; text: "Update this machine's backend to configure OAuth."; wrapMode: Text.Wrap; color: palette.placeholderText; font.pixelSize: 11 }
                ColumnLayout {
                    visible: !!transport.currentIndex && !!authChoice.currentIndex
                    Layout.fillWidth: true
                    spacing: 8
                    Label { Layout.fillWidth: true; text: dialog.google ? "Google requires your own OAuth client. Enable the Gmail and Gmail MCP APIs in your Google Cloud project first." : "Use your existing OAuth client, or leave the client fields blank if the server supports automatic registration."; wrapMode: Text.Wrap; color: palette.placeholderText; font.pixelSize: 12 }
                    NativeButton { visible: dialog.google; text: "Google setup guide ↗"; quiet: true; onClicked: Qt.openUrlExternally("https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server") }
                    Label { text: "Client ID"; font.weight: Font.DemiBold }
                    NativeField { id: clientId; objectName: "mcpOAuthClientId"; Layout.fillWidth: true; placeholderText: "OAuth client ID"; enabled: !dialog.servers.saving }
                    Label { text: "Client secret"; font.weight: Font.DemiBold }
                    NativeField { id: clientSecret; objectName: "mcpOAuthClientSecret"; Layout.fillWidth: true; echoMode: TextInput.Password; placeholderText: dialog.draft.oauth && dialog.draft.oauth.has_client_secret ? "Saved secret · leave unchanged to keep" : "Optional for public clients"; enabled: !dialog.servers.saving; onTextEdited: dialog.secretChanged = true }
                    Label { text: "Authorization server (issuer)"; font.weight: Font.DemiBold }
                    NativeField { id: issuer; objectName: "mcpOAuthIssuer"; Layout.fillWidth: true; placeholderText: "https://accounts.google.com"; enabled: !dialog.servers.saving }
                    Label { text: "Permissions (scopes)"; font.weight: Font.DemiBold }
                    NativeField { id: scope; objectName: "mcpOAuthScope"; Layout.fillWidth: true; placeholderText: "Space-separated scopes"; enabled: !dialog.servers.saving }
                    Label { Layout.fillWidth: true; text: "Request only the permissions you need. Blank uses the server's advertised scopes; Gmail defaults to read-only."; wrapMode: Text.Wrap; color: palette.placeholderText; font.pixelSize: 11 }
                    Label { text: "Callback URL"; font.weight: Font.DemiBold }
                    NativeField { id: redirect; objectName: "mcpOAuthRedirect"; Layout.fillWidth: true; text: "http://127.0.0.1:8766/oauth/callback"; enabled: !dialog.servers.saving }
                    Label { Layout.fillWidth: true; text: "Register this exact URL with your OAuth provider. The callback runs on this desktop, including when Ava uses an SSH machine. Tokens stay on the execution machine."; wrapMode: Text.Wrap; color: palette.placeholderText; font.pixelSize: 11 }
                }
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
                Label { Layout.fillWidth: true; text: authChoice.currentIndex && transport.currentIndex ? "Save the server, then choose Sign in. Your browser opens only when you request it. Ava refreshes saved OAuth tokens automatically." : "Adding an enabled server connects it now. Ava also connects enabled servers when an agent starts work in another workspace."; wrapMode: Text.Wrap; color: palette.placeholderText; font.pixelSize: 12 }
            }
        }
        Label { Layout.fillWidth: true; visible: !!text; text: dialog.servers.editorError; wrapMode: Text.Wrap; color: palette.link }
        RowLayout {
            Layout.fillWidth: true
            Item { Layout.fillWidth: true }
            NativeButton { text: "Cancel"; enabled: !dialog.servers.saving; onClicked: dialog.close() }
            NativeButton { objectName: "saveMcpButton"; text: dialog.servers.saving ? "Saving…" : dialog.draft.id ? "Save" : "Add and connect"; enabled: !dialog.servers.saving && !!name.text.trim() && (transport.currentIndex ? !!address.text.trim() : !!command.text.trim()); onClicked: dialog.save() }
        }
    }
}
