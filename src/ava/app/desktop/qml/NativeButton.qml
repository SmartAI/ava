import QtQuick
import QtQuick.Controls

Button {
    id: control
    property bool primary: false
    property bool quiet: false
    property string tip: ""
    implicitHeight: 32
    implicitWidth: Math.max(34, contentItem.implicitWidth + leftPadding + rightPadding)
    padding: 8
    leftPadding: text ? 12 : 8
    rightPadding: leftPadding
    font.pixelSize: 12
    font.weight: Font.Medium
    icon.width: 16
    icon.height: 16
    icon.color: control.ApplicationWindow.window
                ? (control.primary ? control.ApplicationWindow.window.palette.highlightedText : control.ApplicationWindow.window.palette.text)
                : "#2d352f"
    opacity: enabled ? 1 : 0.42
    hoverEnabled: true
    Accessible.name: text || tip
    NativeToolTip {
        visible: control.hovered && !!control.tip
        text: control.tip
        palette: control.palette
    }
    background: Rectangle {
        radius: height / 2 > 12 ? 10 : height / 2
        color: control.primary ? control.palette.highlight : control.quiet ? "transparent" : control.palette.button
        border.width: control.quiet && !control.hovered && !control.visualFocus ? 0 : 1
        border.color: control.visualFocus ? control.palette.highlight : control.palette.mid
        Rectangle {
            anchors.fill: parent
            radius: parent.radius
            color: control.down ? "#18000000" : control.hovered ? "#0b888888" : "transparent"
            Behavior on color { ColorAnimation { duration: 100 } }
        }
        Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: 1
            height: parent.height / 2
            radius: parent.radius - 1
            visible: !control.quiet && !control.down
            gradient: Gradient {
                GradientStop { position: 0; color: "#12ffffff" }
                GradientStop { position: 1; color: "#00ffffff" }
            }
        }
    }
    palette.buttonText: control.ApplicationWindow.window
                        ? (control.primary ? control.ApplicationWindow.window.palette.highlightedText : control.ApplicationWindow.window.palette.text)
                        : "#2d352f"
}
