pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

NativeDialog {
    id: dialog
    objectName: "settingsDialog"
    required property var backend
    required property string codeFont
    required property bool dark
    property int page: 0
    property bool ready: false
    property bool custom: false
    property var connections: []
    property string selectedProvider: ""
    readonly property var connection: connections.find(item => item.id === selectedProvider) || ({})
    readonly property var state: backend.providerSettingsState
    readonly property var startup: backend.serviceState
    readonly property string provider: custom ? providerName.text.trim() : selectedProvider
    readonly property bool storesKey: custom || ["codex", "llamacpp"].indexOf(provider) < 0
    signal darkRequested(bool value)
    signal archivedRequested
    anchors.centerIn: parent
    width: Math.min(780, parent.width - 48)
    height: Math.min(640, parent.height - 64)
    modal: true
    focus: true
    padding: 0
    closePolicy: state.saving ? Popup.NoAutoClose : Popup.CloseOnEscape | Popup.CloseOnPressOutside
    title: "Settings"
    background: Rectangle {
        radius: 18
        color: dialog.palette.base
        border.color: dialog.palette.mid
    }
    function load(values) {
        connections = (values.providers || []).map(item => Object.assign({}, item, {display: item.label + " · " + item.status}));
        const preferred = values.saved_provider || selectedProvider || backend.selection.provider;
        providerPicker.currentIndex = Math.max(0, connections.findIndex(item => item.id === preferred));
        selectProvider();
        ready = true;
    }
    function selectProvider() {
        const item = connections[providerPicker.currentIndex] || {};
        selectedProvider = item.id || "";
        custom = item.provider_type === "custom";
        providerName.text = custom ? selectedProvider : "";
        familyPicker.currentIndex = item.family === "anthropic" ? 1 : 0;
        baseUrl.text = item.base_url || "";
        apiKey.clear();
    }
    function selectType(value) {
        custom = value;
        apiKey.clear();
        if (custom) {
            selectedProvider = "";
            providerPicker.currentIndex = -1;
            providerName.clear();
            baseUrl.clear();
            familyPicker.currentIndex = 0;
            providerName.forceActiveFocus();
        } else {
            selectProvider();
        }
    }
    function save() {
        backend.saveProviderSettings({
            provider_type: custom ? "custom" : "builtin",
            provider: provider,
            family: custom ? familyPicker.currentValue : null,
            base_url: custom ? baseUrl.text.trim() : null,
            api_key: storesKey ? (apiKey.text || null) : null
        });
    }
    onOpened: {
        ready = false;
        apiKey.clear();
        backend.loadProviderSettings();
        backend.loadServiceState();
    }
    onClosed: apiKey.clear()
    Connections {
        target: dialog.backend
        function onProviderSettingsLoaded(values) {
            if (dialog.visible)
                dialog.load(values);
        }
        function onProviderKeyRemoved() {
            apiKey.clear();
            dialog.backend.loadProviderSettings();
        }
    }
    header: Item {
        implicitHeight: 64
        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 24
            anchors.rightMargin: 16
            Label {
                Layout.fillWidth: true
                text: dialog.title
                font.pixelSize: 18
                font.weight: Font.DemiBold
            }
            NativeButton {
                icon.source: "icons/close.svg"
                quiet: true
                tip: "Close settings"
                enabled: !dialog.state.saving
                onClicked: dialog.close()
            }
        }
    }
    contentItem: RowLayout {
        spacing: 0
        ColumnLayout {
            Layout.preferredWidth: 164
            Layout.minimumWidth: 164
            Layout.maximumWidth: 164
            Layout.fillHeight: true
            Layout.leftMargin: 12
            Layout.rightMargin: 12
            spacing: 5
            Repeater {
                model: [
                    {label: "General", name: "settingsGeneralTab", icon: "settings"},
                    {label: "Providers", name: "settingsProvidersTab", icon: "model"},
                    {label: "Shortcuts", name: "settingsShortcutsTab", icon: "command"}
                ]
                delegate: NativeButton {
                    id: navigation
                    required property int index
                    required property var modelData
                    objectName: modelData.name
                    Layout.fillWidth: true
                    implicitHeight: 36
                    text: modelData.label
                    quiet: dialog.page !== index
                    contentItem: RowLayout {
                        spacing: 9
                        Image {
                            source: "icons/" + navigation.modelData.icon + ".svg"
                            sourceSize.width: 16
                            sourceSize.height: 16
                            opacity: dialog.dark ? 0.9 : 0.65
                        }
                        Label {
                            Layout.fillWidth: true
                            text: navigation.text
                            font.pixelSize: 12
                            font.weight: dialog.page === navigation.index ? Font.DemiBold : Font.Normal
                        }
                    }
                    onClicked: dialog.page = index
                }
            }
            Item { Layout.fillHeight: true }
        }
        Rectangle {
            Layout.fillHeight: true
            implicitWidth: 1
            color: dialog.palette.mid
        }
        StackLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.leftMargin: 24
            Layout.rightMargin: 16
            currentIndex: dialog.page
            Flickable {
                objectName: "settingsGeneralScroll"
                contentHeight: general.implicitHeight + 20
                contentWidth: width
                clip: true
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar {}
                ColumnLayout {
                    id: general
                    width: parent.width - 12
                    spacing: 22
                    Label { text: "Appearance"; font.pixelSize: 16; font.weight: Font.DemiBold }
                    RowLayout {
                        Layout.fillWidth: true
                        Label { Layout.fillWidth: true; text: "Theme"; font.pixelSize: 13 }
                        NativeButton {
                            objectName: "lightThemeButton"
                            text: "Light"
                            primary: !dialog.dark
                            onClicked: dialog.darkRequested(false)
                        }
                        NativeButton {
                            objectName: "darkThemeButton"
                            text: "Dark"
                            primary: dialog.dark
                            onClicked: dialog.darkRequested(true)
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Label { Layout.fillWidth: true; text: "Chat text size"; font.pixelSize: 13 }
                        NativeCombo {
                            objectName: "readingSizePicker"
                            implicitWidth: 136
                            model: ["Small", "Default", "Large"]
                            currentIndex: (dialog.backend.readingSize - 13) / 2
                            onActivated: dialog.backend.saveReadingSize(13 + currentIndex * 2)
                            Accessible.name: "Chat text size"
                        }
                    }
                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: readingPreview.implicitHeight + 28
                        radius: 12
                        color: dialog.palette.window
                        border.color: dialog.palette.mid
                        Label {
                            id: readingPreview
                            anchors.fill: parent
                            anchors.margins: 14
                            text: "A little more room to think."
                            font.pixelSize: dialog.backend.readingSize
                            wrapMode: Text.WordWrap
                            lineHeight: 1.5
                        }
                    }
                    Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: dialog.palette.mid }
                    RowLayout {
                        Layout.fillWidth: true
                        ColumnLayout {
                            Layout.fillWidth: true
                            Label { Layout.fillWidth: true; text: "Archived chats"; font.pixelSize: 13 }
                            Label { Layout.fillWidth: true; text: "Find and restore previous conversations."; wrapMode: Text.WordWrap; font.pixelSize: 11; color: palette.placeholderText }
                        }
                        NativeButton {
                            text: "Manage"
                            onClicked: { dialog.close(); dialog.archivedRequested(); }
                        }
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 8
                        Label { text: "Background work"; font.pixelSize: 16; font.weight: Font.DemiBold }
                        Label {
                            objectName: "backgroundWorkNotice"
                            Layout.fillWidth: true
                            text: "Closing Ava keeps agent tasks running. Pause or stop a task from its conversation."
                            wrapMode: Text.WordWrap
                            font.pixelSize: 12
                            color: palette.placeholderText
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 4
                                Label { text: "Start at login"; font.pixelSize: 13 }
                                Label {
                                    objectName: "backgroundStartupStatus"
                                    Layout.fillWidth: true
                                    text: dialog.startup.loading ? "Checking background service…" : dialog.startup.error ? "Check background service status" : dialog.startup.autostart ? (dialog.startup.active ? "Enabled · running" : "Enabled · stopped") : "Off · starts when you open Ava"
                                    wrapMode: Text.WordWrap
                                    font.pixelSize: 11
                                    color: palette.placeholderText
                                }
                            }
                            NativeButton {
                                objectName: "backgroundStartupButton"
                                text: dialog.startup.busy ? "Updating…" : dialog.startup.autostart ? "Turn off" : "Enable"
                                enabled: dialog.startup.supported && !dialog.startup.loading
                                onClicked: {
                                    dialog.forceActiveFocus();
                                    dialog.backend.configureStartup(!dialog.startup.autostart);
                                }
                            }
                        }
                        Label {
                            Layout.fillWidth: true
                            text: "Starts in the background and restarts after a crash. Uses saved credentials; shell-only API keys are not copied."
                            wrapMode: Text.WordWrap
                            font.pixelSize: 11
                            color: palette.placeholderText
                        }
                        Label {
                            Layout.fillWidth: true
                            visible: dialog.startup.manager === "systemd" && dialog.startup.installed
                            text: dialog.startup.linger === true ? "Runs after sign-out, including while the desktop is closed." : dialog.startup.linger === false ? "Stops at sign-out. Enable linger for this account to run unattended." : "Sign-out behavior could not be verified for this account."
                            wrapMode: Text.WordWrap
                            font.pixelSize: 11
                            color: palette.placeholderText
                        }
                        Label {
                            objectName: "backgroundStartupError"
                            Layout.fillWidth: true
                            visible: !!dialog.startup.error
                            text: dialog.startup.error
                            wrapMode: Text.Wrap
                            font.pixelSize: 11
                            color: dialog.dark ? "#f3a2a2" : "#b42318"
                        }
                        NativeButton {
                            text: "Refresh status"
                            visible: !!dialog.startup.error
                            enabled: !dialog.startup.loading
                            quiet: true
                            onClicked: dialog.backend.loadServiceState()
                        }
                    }
                }
            }
            Flickable {
                id: providersScroll
                objectName: "settingsProviderScroll"
                contentHeight: providers.implicitHeight + 20
                contentWidth: width
                clip: true
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar {}
                ColumnLayout {
                    id: providers
                    width: parent.width - 12
                    spacing: 16
                    Label { text: "Provider connections"; font.pixelSize: 16; font.weight: Font.DemiBold }
                    Label {
                        Layout.fillWidth: true
                        text: "Manage credentials here. Choose a model and reasoning effort from the model name in each conversation."
                        font.pixelSize: 12
                        color: palette.placeholderText
                        wrapMode: Text.WordWrap
                    }
                    Label {
                        visible: !dialog.ready
                        Layout.fillWidth: true
                        text: dialog.state.loading ? "Loading settings…" : "Settings are unavailable."
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                    }
                    NativeButton {
                        objectName: "retryProviderSettings"
                        visible: !dialog.ready && !dialog.state.loading
                        text: "Try again"
                        onClicked: dialog.backend.loadProviderSettings()
                    }
                    ColumnLayout {
                        visible: dialog.ready
                        enabled: !dialog.state.saving && !dialog.state.loading
                        Layout.fillWidth: true
                        spacing: 16
                        NativeCombo {
                            id: providerPicker
                            objectName: "settingsProviderPicker"
                            Layout.fillWidth: true
                            model: dialog.connections
                            textRole: "display"
                            valueRole: "id"
                            onActivated: dialog.selectProvider()
                            Accessible.name: "Provider connections"
                        }
                        RowLayout {
                            NativeButton {
                                objectName: "customProviderType"
                                text: "Add custom provider"
                                onClicked: dialog.selectType(true)
                            }
                            NativeButton {
                                text: "Refresh status"
                                quiet: true
                                onClicked: dialog.backend.loadProviderSettings()
                            }
                        }
                        Label {
                            objectName: "providerConnectionStatus"
                            Layout.fillWidth: true
                            text: dialog.connection.message || "Connect an OpenAI-compatible or Anthropic endpoint."
                            wrapMode: Text.WordWrap
                            font.pixelSize: 12
                            color: palette.placeholderText
                        }
                        Label {
                            Layout.fillWidth: true
                            visible: !!dialog.connection.credential_source
                            text: "Credentials: " + (dialog.connection.credential_source || "")
                            font.pixelSize: 11
                            color: palette.placeholderText
                        }
                        GridLayout {
                            Layout.fillWidth: true
                            columns: width >= 420 ? 2 : 1
                            columnSpacing: 16
                            rowSpacing: 14
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 6
                                visible: dialog.custom
                                Label { text: "Provider name"; font.pixelSize: 12 }
                                NativeField {
                                    id: providerName
                                    objectName: "settingsProviderName"
                                    Layout.fillWidth: true
                                    visible: dialog.custom
                                    placeholderText: "my-gateway"
                                    onTextEdited: apiKey.clear()
                                    Accessible.name: "Provider name"
                                }
                            }
                            ColumnLayout {
                                visible: dialog.custom
                                Layout.fillWidth: true
                                spacing: 6
                                Label { text: "API format"; font.pixelSize: 12 }
                                NativeCombo {
                                    id: familyPicker
                                    objectName: "settingsFamilyPicker"
                                    Layout.fillWidth: true
                                    model: [{label: "OpenAI-compatible", value: "openai"}, {label: "Anthropic", value: "anthropic"}]
                                    textRole: "label"
                                    valueRole: "value"
                                    Accessible.name: "API format"
                                }
                            }
                        }
                        ColumnLayout {
                            visible: dialog.custom
                            Layout.fillWidth: true
                            spacing: 6
                            Label { text: "Base URL"; font.pixelSize: 12 }
                            NativeField {
                                id: baseUrl
                                objectName: "settingsBaseUrl"
                                Layout.fillWidth: true
                                placeholderText: "https://gateway.example.com/v1"
                                Accessible.name: "Base URL"
                            }
                        }
                        ColumnLayout {
                            visible: dialog.storesKey
                            Layout.fillWidth: true
                            spacing: 6
                            RowLayout {
                                Layout.fillWidth: true
                                Label { Layout.fillWidth: true; text: "API key"; font.pixelSize: 12 }
                                NativeButton {
                                    objectName: "removeProviderKey"
                                    text: "Remove stored key"
                                    quiet: true
                                    implicitHeight: 24
                                    enabled: dialog.connection.has_stored_key === true && dialog.provider === dialog.selectedProvider
                                    onClicked: dialog.backend.removeProviderKey(dialog.provider)
                                }
                            }
                            NativeField {
                                id: apiKey
                                objectName: "settingsApiKey"
                                Layout.fillWidth: true
                                placeholderText: "Leave blank to keep the stored key"
                                echoMode: TextInput.Password
                                Accessible.name: "API key"
                            }
                        }
                        Label {
                            visible: !dialog.storesKey
                            Layout.fillWidth: true
                            text: dialog.provider === "codex" ? "Uses your existing Codex CLI login. Run codex login in the terminal to sign in." : "Uses the local llama.cpp server at http://127.0.0.1:8081/v1."
                            font.pixelSize: 12
                            color: palette.placeholderText
                            wrapMode: Text.WordWrap
                        }
                    }
                }
            }
            Flickable {
                contentWidth: width
                contentHeight: shortcuts.implicitHeight + 20
                clip: true
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar {}
                ColumnLayout {
                    id: shortcuts
                    width: parent.width - 12
                    spacing: 20
                    Label { text: "Keyboard shortcuts"; font.pixelSize: 16; font.weight: Font.DemiBold }
                    Repeater {
                        model: [
                            {action: "New chat", key: "N"}, {action: "Search chats", key: "K"},
                            {action: "Toggle sidebar", key: "B"}, {action: "Toggle inspector", key: "Alt B"},
                            {action: "Toggle terminal", key: "J"}, {action: "Settings", key: ","}
                        ]
                        delegate: RowLayout {
                            id: shortcut
                            required property var modelData
                            Layout.fillWidth: true
                            Label { Layout.fillWidth: true; text: shortcut.modelData.action; font.pixelSize: 12 }
                            Label {
                                text: (Qt.platform.os === "osx" ? "⌘ " : "Ctrl ") + shortcut.modelData.key
                                font.pixelSize: 12
                                font.family: dialog.codeFont
                                color: palette.placeholderText
                            }
                        }
                    }
                    Label {
                        Layout.fillWidth: true
                        text: "Enter sends · Shift+Enter adds a line\nWhile running: Enter steers, Alt+Enter queues a follow-up."
                        font.pixelSize: 12
                        color: palette.placeholderText
                        wrapMode: Text.WordWrap
                        lineHeight: 1.5
                    }
                }
            }
        }
    }
    footer: ColumnLayout {
        spacing: 0
        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: dialog.palette.mid }
        RowLayout {
            Layout.fillWidth: true
            Layout.margins: 16
            spacing: 8
            Label {
                objectName: "providerSettingsFeedback"
                Layout.fillWidth: true
                text: dialog.page === 1 ? (dialog.state.error || dialog.state.notice || "Credentials are stored separately from settings. Conversations are unchanged.") : "Appearance changes are saved automatically."
                font.pixelSize: 11
                color: dialog.page === 1 && dialog.state.error ? (dialog.dark ? "#ecc4b4" : "#8b3e2d") : dialog.palette.placeholderText
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
            }
            NativeButton {
                objectName: "closeSettingsButton"
                text: "Done"
                enabled: !dialog.state.saving
                primary: dialog.page !== 1
                onClicked: dialog.close()
            }
            NativeButton {
                objectName: "saveProviderSettings"
                text: dialog.state.saving ? "Checking…" : "Save connection"
                primary: true
                visible: dialog.page === 1
                enabled: dialog.ready && !dialog.state.loading && !dialog.state.saving && !!dialog.provider && (!dialog.custom || !!baseUrl.text.trim())
                onClicked: dialog.save()
            }
        }
    }
}
