import QtQuick
import QtQuick.Controls

ToolTip {
    id: tip
    delay: 650
    timeout: 7000
    padding: 8
    horizontalPadding: 10
    implicitWidth: Math.min(340, contentItem.implicitWidth + leftPadding + rightPadding)
    contentItem: Text {
        objectName: "nativeToolTipText"
        text: tip.text
        color: tip.palette.text
        font.pixelSize: 11
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
    }
    background: Rectangle {
        objectName: "nativeToolTipBackground"
        radius: 8
        color: tip.palette.base
        border.color: tip.palette.mid
    }
}
