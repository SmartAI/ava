pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls

ComboBox {
    id: control
    implicitHeight: 34
    leftPadding: 12
    rightPadding: 28
    font.pixelSize: 12
    background: Rectangle {
        radius: 10
        color: control.hovered ? control.palette.light : control.palette.button
        border.color: control.activeFocus ? control.palette.highlight : control.palette.mid
    }
    contentItem: Text {
        text: control.displayText
        font: control.font
        color: control.enabled ? control.palette.text : control.palette.placeholderText
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }
    indicator: Text {
        x: control.width - width - 12
        y: (control.height - height) / 2
        text: "⌄"
        color: control.palette.text
    }
    popup: Popup {
        y: control.height + 5
        width: control.width
        padding: 5
        implicitHeight: Math.min(320, contentItem.implicitHeight + 10)
        background: Rectangle { radius: 12; color: control.palette.base; border.color: control.palette.mid }
        contentItem: ListView {
            clip: true
            implicitHeight: contentHeight
            model: control.popup.visible ? control.delegateModel : null
            currentIndex: control.highlightedIndex
            ScrollBar.vertical: ScrollBar {}
        }
    }
    delegate: ItemDelegate {
        id: entry
        required property int index
        required property var modelData
        width: control.width - 10
        height: 34
        text: control.textRole ? entry.modelData[control.textRole] : entry.modelData
        highlighted: control.highlightedIndex === entry.index
        contentItem: Text { text: entry.text; color: control.palette.text; elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter; font.pixelSize: 12 }
        background: Rectangle { radius: 7; color: entry.highlighted ? control.palette.alternateBase : "transparent" }
    }
}
