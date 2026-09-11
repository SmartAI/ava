import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: state
    property string title: ""
    property string description: ""
    spacing: Theme.spaceSm
    Label {
        Layout.fillWidth: true
        text: state.title
        textFormat: Text.PlainText
        horizontalAlignment: Text.AlignHCenter
        wrapMode: Text.WordWrap
        font.pixelSize: Theme.body
        font.weight: Font.Medium
    }
    Label {
        Layout.fillWidth: true
        visible: !!text
        text: state.description
        textFormat: Text.PlainText
        horizontalAlignment: Text.AlignHCenter
        wrapMode: Text.WordWrap
        font.pixelSize: Theme.caption
        color: Theme.secondaryText
        lineHeight: 1.35
    }
}
