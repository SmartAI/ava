pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls

ComboBox {
    id: control
    implicitHeight: Theme.controlHeight
    leftPadding: 12
    rightPadding: 28
    font.pixelSize: Theme.body
    background: Surface {
        radius: Theme.controlRadius
        focused: control.visualFocus
        color: !control.enabled ? Theme.inset : control.down ? Theme.selection : control.hovered ? Theme.hover : Theme.surface
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
        background: Surface { elevation: 2 }
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
        height: Theme.controlHeight
        text: control.textRole ? entry.modelData[control.textRole] : entry.modelData
        highlighted: control.highlightedIndex === entry.index
        contentItem: Text { text: entry.text; color: control.palette.text; elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter; font.pixelSize: Theme.body }
        background: Rectangle { radius: Theme.controlRadius; color: entry.highlighted ? Theme.selection : "transparent" }
    }
}
