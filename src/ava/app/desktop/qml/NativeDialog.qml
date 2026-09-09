import QtQuick
import QtQuick.Controls

Dialog {
    id: dialog
    readonly property var hostWindow: dialog.ApplicationWindow.window
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
        font.pixelSize: 17
        font.weight: Font.DemiBold
        leftPadding: dialog.padding
        rightPadding: dialog.padding
        topPadding: dialog.padding
        bottomPadding: 2
        elide: Text.ElideRight
        background: null
    }
}
