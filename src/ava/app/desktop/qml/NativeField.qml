import QtQuick
import QtQuick.Controls

TextField {
    id: control
    implicitHeight: Theme.controlHeight
    padding: Theme.spaceSm
    leftPadding: Theme.spaceMd
    rightPadding: Theme.spaceMd
    selectByMouse: true
    ContextMenu.menu: TextMenu { editor: control }
    font.pixelSize: Theme.body
    color: palette.text
    background: Surface {
        radius: Theme.controlRadius
        focused: control.activeFocus
        color: control.enabled ? Theme.surface : Theme.inset
    }
}
