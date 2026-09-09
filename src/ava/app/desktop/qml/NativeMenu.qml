import QtQuick
import QtQuick.Controls

Menu {
    id: menu
    implicitWidth: 210
    padding: 6
    font.pixelSize: 13
    popupType: Qt.platform.pluginName === "wayland" ? Popup.Item : Popup.Window
    background: Rectangle {
        radius: 12
        color: menu.palette.base
        border.color: menu.palette.mid
    }
}
