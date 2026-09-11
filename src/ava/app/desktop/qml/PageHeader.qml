import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Control {
    id: header
    objectName: "pageHeader"
    default property alias actions: actions.data
    property string title: ""
    property string description: ""
    property bool sidebarVisible: true
    signal sidebarRequested
    padding: 0
    contentItem: GridLayout {
        columns: header.width < 540 ? 1 : 2
        columnSpacing: Theme.spaceLg
        rowSpacing: Theme.spaceMd
        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.spaceMd
            NativeButton {
                visible: !header.sidebarVisible
                icon.source: "icons/left.svg"
                quiet: true
                tip: "Show sidebar"
                onClicked: header.sidebarRequested()
            }
            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spaceXs
                Label {
                    Layout.fillWidth: true
                    text: header.title
                    textFormat: Text.PlainText
                    font.pixelSize: Theme.pageTitle
                    font.weight: Font.DemiBold
                    wrapMode: Text.WordWrap
                }
                Label {
                    Layout.fillWidth: true
                    visible: !!text
                    text: header.description
                    textFormat: Text.PlainText
                    color: Theme.secondaryText
                    font.pixelSize: Theme.caption
                    wrapMode: Text.WordWrap
                }
            }
        }
        RowLayout {
            id: actions
            Layout.alignment: Qt.AlignRight
            spacing: Theme.spaceSm
        }
    }
}
