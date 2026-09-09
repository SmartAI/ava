pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

NativeDialog {
    id: dialog
    required property var backend
    required property string codeFont
    property var challenge: ({})
    parent: Overlay.overlay
    anchors.centerIn: parent
    title: "Verify SSH host"
    modal: true
    focus: true
    padding: 24
    width: Math.min(560, parent.width - 40)
    closePolicy: Popup.CloseOnEscape
    onOpened: cancel.forceActiveFocus()
    background: Rectangle {
        radius: 18
        color: dialog.palette.base
        border.color: dialog.palette.mid
    }

    function synchronize() {
        const next = backend.hostKeyRequest;
        if (!next.request) {
            challenge = ({});
            close();
        } else {
            if (visible && challenge.request !== next.request) {
                challenge = ({});
                close();
                return;
            }
            challenge = next;
            if (!visible) open();
        }
    }
    function answer(accept) {
        const previous = challenge;
        challenge = ({});
        backend.answerHostKey(previous.machine, previous.request, accept);
        close();
    }
    onClosed: {
        if (challenge.request) answer(false);
        Qt.callLater(dialog.synchronize);
    }
    Connections {
        target: dialog.backend
        function onHostKeyChanged() { Qt.callLater(dialog.synchronize); }
    }

    ColumnLayout {
        width: parent.width
        spacing: 18
        Label {
            Layout.fillWidth: true
            text: "First connection to " + (dialog.challenge.name || "")
            textFormat: Text.PlainText
            font.pixelSize: 14
            font.weight: Font.Medium
            wrapMode: Text.WrapAnywhere
        }
        Label {
            Layout.fillWidth: true
            text: "Check this fingerprint against the server or a trusted administrator before connecting."
            font.pixelSize: 13
            wrapMode: Text.WordWrap
            color: palette.placeholderText
        }
        Pane {
            Layout.fillWidth: true
            padding: 16
            background: Rectangle {
                radius: 12
                color: dialog.palette.window
                border.color: dialog.palette.mid
            }
            ColumnLayout {
                width: parent.width
                spacing: 10
                Label {
                    Layout.fillWidth: true
                    text: dialog.challenge.host || ""
                    textFormat: Text.PlainText
                    wrapMode: Text.WrapAnywhere
                    font.pixelSize: 13
                    font.weight: Font.Medium
                }
                Label {
                    text: (dialog.challenge.algorithm || "") + " · SHA-256"
                    font.pixelSize: 11
                    color: palette.placeholderText
                }
                TextArea {
                    id: fingerprint
                    objectName: "hostKeyFingerprint"
                    ContextMenu.menu: TextMenu { editor: fingerprint }
                    padding: 0
                    background: null
                    Layout.fillWidth: true
                    text: dialog.challenge.fingerprint || ""
                    textFormat: TextEdit.PlainText
                    readOnly: true
                    selectByMouse: true
                    wrapMode: TextEdit.WrapAnywhere
                    color: dialog.palette.text
                    selectionColor: dialog.palette.highlight
                    selectedTextColor: dialog.palette.highlightedText
                    font.pixelSize: 14
                    font.family: dialog.codeFont
                }
                NativeButton {
                    objectName: "copyHostKeyButton"
                    text: "Copy fingerprint"
                    implicitHeight: 30
                    quiet: true
                    onClicked: dialog.backend.copyText(dialog.challenge.fingerprint)
                }
            }
        }
        Label {
            Layout.fillWidth: true
            text: "SSH will remember this host for future connections."
            wrapMode: Text.WordWrap
            font.pixelSize: 12
            color: palette.placeholderText
        }
        RowLayout {
            Layout.fillWidth: true
            Item { Layout.fillWidth: true }
            NativeButton {
                id: cancel
                objectName: "cancelHostKeyButton"
                text: "Cancel"
                onClicked: dialog.answer(false)
            }
            NativeButton {
                property string pressedRequest: ""
                objectName: "trustHostKeyButton"
                text: "Trust and connect"
                primary: true
                enabled: !!dialog.challenge.request
                onPressed: pressedRequest = dialog.challenge.request
                onClicked: {
                    if (pressedRequest === dialog.challenge.request) dialog.answer(true);
                }
            }
        }
    }
}
