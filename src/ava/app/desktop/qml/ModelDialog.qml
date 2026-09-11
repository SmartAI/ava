pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

NativeDialog {
    id: dialog
    objectName: "modelDialog"
    required property var backend
    signal providersRequested
    property string provider: ""
    property string modelId: ""
    property string effort: ""
    readonly property var choices: backend.modelChoices.providers || []
    readonly property var connection: choices.find(item => item.id === provider) || ({})
    readonly property var models: connection.models || []
    readonly property var profile: models.find(item => item.id === modelId) || ({})
    readonly property var efforts: profile.effort_values || []
    readonly property bool busy: backend.status !== "idle"
    anchors.centerIn: parent
    width: Math.min(480, parent.width - 48)
    modal: true
    focus: true
    title: "Conversation model"
    closePolicy: backend.selecting ? Popup.NoAutoClose : Popup.CloseOnEscape | Popup.CloseOnPressOutside
    onOpened: {
        provider = backend.selection.provider || "";
        modelId = backend.selection.model || "";
        effort = backend.selection.effort || "";
        backend.loadModels();
    }
    Connections {
        target: dialog.backend
        function onModelSelectionSaved() { dialog.close(); }
    }
    ColumnLayout {
        width: parent.width
        spacing: 14
        Label {
            Layout.fillWidth: true
            text: "Only this conversation changes. Other conversations and provider credentials stay the same."
            wrapMode: Text.WordWrap
            color: palette.placeholderText
            font.pixelSize: 12
        }
        Label {
            Layout.fillWidth: true
            visible: dialog.backend.modelChoices.loading || !dialog.choices.length
            text: dialog.backend.modelChoices.loading ? "Checking provider connections…" : "No connected providers. Add credentials in Settings, then try again."
            wrapMode: Text.WordWrap
            font.pixelSize: 12
        }
        ColumnLayout {
            Layout.fillWidth: true
            visible: dialog.choices.length > 0
            enabled: !dialog.backend.selecting && !dialog.backend.modelChoices.loading
            spacing: 8
            Label { text: "Provider"; font.pixelSize: 12 }
            NativeCombo {
                objectName: "conversationProviderPicker"
                Layout.fillWidth: true
                model: dialog.choices
                textRole: "label"
                valueRole: "id"
                currentIndex: dialog.choices.findIndex(item => item.id === dialog.provider)
                displayText: currentIndex < 0 ? "Choose a connected provider" : currentText
                enabled: !dialog.busy
                onActivated: {
                    dialog.provider = currentValue;
                    dialog.modelId = dialog.models.length ? dialog.models[0].id : "";
                    dialog.effort = "";
                }
                Accessible.name: "Provider"
            }
            Label { text: "Model"; font.pixelSize: 12 }
            NativeCombo {
                objectName: "modelPicker"
                Layout.fillWidth: true
                model: dialog.models
                textRole: "id"
                valueRole: "id"
                currentIndex: dialog.models.findIndex(item => item.id === dialog.modelId)
                displayText: currentIndex < 0 ? "Choose a model" : currentText
                onActivated: { dialog.modelId = currentValue; dialog.effort = ""; }
                Accessible.name: "Model"
            }
            Label { text: "Reasoning effort"; font.pixelSize: 12 }
            NativeCombo {
                objectName: "effortPicker"
                Layout.fillWidth: true
                model: [{label: "Provider default", value: ""}].concat(dialog.efforts.map(value => ({label: value, value: value})))
                textRole: "label"
                valueRole: "value"
                currentIndex: dialog.effort ? dialog.efforts.indexOf(dialog.effort) + 1 : 0
                enabled: dialog.efforts.length > 0
                onActivated: dialog.effort = currentValue
                Accessible.name: "Reasoning effort"
            }
            Label {
                Layout.fillWidth: true
                text: dialog.efforts.length ? "Higher effort can take longer and use more tokens." : "This model does not advertise reasoning effort."
                color: palette.placeholderText
                wrapMode: Text.WordWrap
                font.pixelSize: 11
            }
        }
        Label {
            Layout.fillWidth: true
            visible: dialog.busy
            text: "Model and effort changes apply at the next step. Change providers after the conversation finishes."
            wrapMode: Text.WordWrap
            color: palette.placeholderText
            font.pixelSize: 11
        }
        Label {
            objectName: "modelSelectionError"
            Layout.fillWidth: true
            text: dialog.backend.error
            visible: !!text
            color: Theme.danger
            wrapMode: Text.Wrap
            font.pixelSize: 12
        }
        RowLayout {
            Layout.fillWidth: true
            NativeButton {
                text: "Manage providers"
                quiet: true
                enabled: !dialog.backend.selecting
                onClicked: { dialog.close(); dialog.providersRequested(); }
            }
            NativeButton {
                text: "Refresh"
                quiet: true
                enabled: !dialog.backend.selecting && !dialog.backend.modelChoices.loading
                onClicked: dialog.backend.loadModels()
            }
            Item { Layout.fillWidth: true }
        }
        RowLayout {
            Layout.alignment: Qt.AlignRight
            NativeButton {
                objectName: "closeModelButton"
                text: "Cancel"
                enabled: !dialog.backend.selecting
                onClicked: dialog.close()
            }
            NativeButton {
                objectName: "applyConversationModel"
                text: dialog.backend.selecting ? "Applying…" : "Apply"
                primary: true
                enabled: !dialog.backend.selecting && !dialog.backend.modelChoices.loading && !!dialog.profile.id && (!dialog.effort || dialog.efforts.indexOf(dialog.effort) >= 0)
                onClicked: dialog.backend.selectConversationModel(dialog.provider, dialog.modelId, dialog.effort)
            }
        }
    }
}
