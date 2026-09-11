pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Effects

Rectangle {
    id: surface
    // Flat by default: only persistent raised surfaces and overlays need shadows.
    property int elevation: 0
    property bool focused: false
    color: Theme.surface
    radius: Theme.cardRadius
    border.color: Theme.border

    Loader {
        anchors.fill: parent
        z: -1
        active: surface.elevation > 0
        sourceComponent: RectangularShadow {
            radius: surface.radius
            blur: surface.elevation > 1 ? 28 : 12
            offset: Qt.vector2d(0, surface.elevation > 1 ? 8 : 3)
            color: Theme.shadow
        }
    }
    Rectangle {
        anchors.fill: parent
        anchors.margins: -3
        color: "transparent"
        radius: surface.radius + 3
        border.width: 2
        border.color: Theme.accent
        visible: surface.focused
    }
}
