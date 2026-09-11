import QtQuick
import QtQuick.Controls

Menu {
    id: menu
    implicitWidth: 210
    padding: 6
    font.pixelSize: Theme.body
    // Headless renderers have no native popup activation; keep menus in the scene.
    popupType: ["wayland", "offscreen"].includes(Qt.platform.pluginName) ? Popup.Item : Popup.Window
    background: Surface { elevation: 2 }
}
