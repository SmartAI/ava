import QtQuick
import QtQuick.Controls

ItemDelegate {
    id: control
    property bool selected: false
    property string accessory: ""
    property string tip: ""
    hoverEnabled: true
    implicitHeight: Theme.controlHeight + 4
    leftPadding: Theme.spaceMd
    rightPadding: accessory ? accessoryLabel.implicitWidth + Theme.spaceXl : Theme.spaceMd
    spacing: Theme.spaceSm
    font.pixelSize: Theme.body
    font.weight: selected ? Font.DemiBold : Font.Normal
    icon.width: Theme.iconSize
    icon.height: Theme.iconSize
    icon.color: selected ? Theme.accent : Theme.secondaryText
    palette.text: Theme.text
    Accessible.name: text
    Accessible.description: selected ? "Current section" : tip
    background: Surface {
        radius: Theme.controlRadius
        color: control.selected ? Theme.selection : control.down ? Theme.border : control.hovered ? Theme.hover : "transparent"
        border.width: 0
        focused: control.visualFocus
    }
    Label {
        id: accessoryLabel
        anchors.right: parent.right
        anchors.rightMargin: Theme.spaceMd
        anchors.verticalCenter: parent.verticalCenter
        text: control.accessory
        font.pixelSize: Theme.captionSmall
        color: Theme.secondaryText
    }
    NativeToolTip { visible: control.hovered && !!control.tip; text: control.tip }
}
