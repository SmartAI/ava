import QtQuick
import QtQuick.Controls

Button {
    id: control
    property bool primary: false
    property bool quiet: false
    property bool selected: checked
    property string tip: ""
    implicitHeight: Theme.controlHeight
    implicitWidth: Math.max(Theme.controlHeight, contentItem.implicitWidth + leftPadding + rightPadding)
    padding: Theme.spaceSm
    leftPadding: text ? Theme.spaceMd : Theme.spaceSm
    rightPadding: leftPadding
    font.pixelSize: Theme.body
    font.weight: Font.Medium
    icon.width: Theme.iconSize
    icon.height: Theme.iconSize
    icon.color: enabled ? (primary ? Theme.primaryText : Theme.text) : Theme.disabledText
    hoverEnabled: true
    Accessible.name: text || tip
    NativeToolTip {
        visible: control.hovered && !!control.tip
        text: control.tip
        palette: control.palette
    }
    background: Surface {
        radius: Theme.controlRadius
        focused: control.visualFocus
        color: !control.enabled ? (control.quiet ? "transparent" : Theme.inset)
             : control.primary ? (control.down ? Theme.primaryPressed : control.hovered ? Theme.primaryHover : Theme.primary)
             : control.selected || control.down ? Theme.selection : control.hovered ? Theme.hover
             : control.quiet ? "transparent" : Theme.surface
        border.width: control.primary || control.quiet || control.selected ? 0 : 1
        border.color: Theme.border
        Behavior on color { ColorAnimation { duration: Theme.motionDuration } }
    }
    palette.buttonText: enabled ? (primary ? Theme.primaryText : Theme.text) : Theme.disabledText
}
