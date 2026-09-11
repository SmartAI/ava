import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

NativeDialog {
    id: dialog
    required property var preview
    readonly property var state: preview ? preview.state : ({})
    readonly property bool imageReady: image.status === Image.Ready
    objectName: "toolImageDialog"
    title: state.name || "Image"
    modal: true
    focus: true
    padding: Theme.spaceXl
    topPadding: 0
    bottomPadding: 8
    anchors.centerIn: parent
    width: Math.min(980, parent.width - 40)
    height: Math.min(760, parent.height - 40)
    header: Item {
        implicitHeight: 60
        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 20
            anchors.rightMargin: 12
            Label {
                Layout.fillWidth: true
                text: dialog.title
                textFormat: Text.PlainText
                elide: Text.ElideMiddle
                font.pixelSize: Theme.sectionTitle
                font.weight: Font.DemiBold
            }
            NativeButton {
                objectName: "closeToolImage"
                quiet: true
                icon.source: "icons/close.svg"
                tip: "Close image preview"
                onClicked: dialog.close()
            }
        }
    }
    footer: Item {
        implicitHeight: 58
        NativeButton {
            anchors.right: parent.right
            anchors.rightMargin: 20
            anchors.verticalCenter: parent.verticalCenter
            text: "Close"
            onClicked: dialog.close()
        }
    }
    onClosed: if (preview) preview.close()
    Connections {
        target: dialog.preview
        function onOpened() { dialog.open(); }
    }
    contentItem: Item {
        Rectangle {
            anchors.fill: parent
            radius: Theme.controlRadius
            color: Theme.inset
        }
        Image {
            id: image
            objectName: "toolImagePreview"
            anchors.fill: parent
            source: dialog.visible ? (dialog.state.url || "") : ""
            asynchronous: true
            cache: false
            sourceSize.width: 1600
            sourceSize.height: 1200
            fillMode: Image.PreserveAspectFit
        }
        BusyIndicator {
            anchors.centerIn: parent
            visible: dialog.state.loading || image.status === Image.Loading
            running: visible
        }
        ColumnLayout {
            anchors.centerIn: parent
            width: Math.min(400, parent.width)
            visible: !!dialog.state.error || image.status === Image.Error
            spacing: 12
            Label {
                Layout.fillWidth: true
                text: dialog.state.error || "This image could not be displayed."
                wrapMode: Text.Wrap
                horizontalAlignment: Text.AlignHCenter
            }
            NativeButton {
                objectName: "retryToolImage"
                Layout.alignment: Qt.AlignHCenter
                text: "Retry"
                onClicked: dialog.preview.retry()
            }
        }
    }
}
