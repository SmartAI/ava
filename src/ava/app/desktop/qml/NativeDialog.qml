import QtQuick
import QtQuick.Controls

Dialog {
    id: dialog
    readonly property var hostWindow: dialog.ApplicationWindow.window
    padding: Theme.spaceXl
    focus: true
    background: Surface { radius: Theme.dialogRadius; elevation: 2 }
    Overlay.modal: Rectangle { color: Theme.scrim }
    // Dialog focus and onOpened setup are immediate, including rapid keyboard input.
    enter: Transition {}
    // Dismiss immediately so the departing modal never intercepts the next action.
    exit: Transition {}
    Connections {
        target: dialog
        function onOpened() {
            const host = dialog.hostWindow;
            if (dialog.modal && host && host.backend && host.backend.browserControl)
                host.backend.browserControl.pauseForDialog();
        }
    }
    header: Label {
        text: dialog.title
        visible: !!text
        font.pixelSize: Theme.sectionTitle
        font.weight: Font.DemiBold
        leftPadding: dialog.padding
        rightPadding: dialog.padding
        topPadding: dialog.padding
        bottomPadding: Theme.spaceXs
        elide: Text.ElideRight
        background: null
    }
}
