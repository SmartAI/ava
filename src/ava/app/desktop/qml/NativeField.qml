import QtQuick
import QtQuick.Controls

TextField {
    id: control
    implicitHeight: 34
    padding: 9
    selectByMouse: true
    ContextMenu.menu: TextMenu { editor: control }
    font.pixelSize: 12
    color: palette.text
    background: Rectangle {
        radius: 9
        color: control.palette.base
        border.color: control.activeFocus ? control.palette.highlight : control.palette.mid
    }
}
