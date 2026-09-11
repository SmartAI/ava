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
        font.pixelSize: Theme.caption
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
    }
    background: Surface {
        objectName: "nativeToolTipBackground"
        elevation: 1
        radius: Theme.controlRadius
        color: tip.palette.base
        border.color: tip.palette.mid
    }
}
