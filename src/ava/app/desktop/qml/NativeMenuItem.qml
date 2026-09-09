import QtQuick
import QtQuick.Controls

MenuItem {
    id: control
    implicitHeight: 30
    leftPadding: 12
    rightPadding: 12
    contentItem: Text {
        text: control.text
        font: control.font
        color: control.highlighted ? control.palette.highlightedText : control.palette.text
        opacity: control.enabled ? 1 : 0.38
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }
    background: Rectangle {
        radius: 6
        color: control.highlighted ? control.palette.highlight : "transparent"
    }
}
