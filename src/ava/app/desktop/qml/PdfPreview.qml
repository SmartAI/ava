pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Pdf

Pane {
    id: preview
    objectName: "pdfPreview"
    required property url source
    required property var backend
    property string rasterId: ""
    readonly property PdfPages view: viewer.item as PdfPages
    readonly property PdfDocument pdfDocument: documentLoader.item as PdfDocument
    readonly property int pageCount: pdfDocument ? pdfDocument.pageCount : 0
    readonly property bool ready: opened && !!pdfDocument && pdfDocument.status === PdfDocument.Ready && pageCount > 0
    readonly property bool empty: !!pdfDocument && pdfDocument.status === PdfDocument.Ready && pageCount === 0
    property bool opened: false
    property bool passwordNeeded: false
    property bool passwordTried: false
    property bool canceled: false
    property string fitMode: "width"
    property url loadedSource: ""
    signal linkRequested(url link)
    Timer { id: sourceTimer; interval: 0; onTriggered: preview.loadSource() }
    Timer { id: fitTimer; interval: 0; onTriggered: preview.fit() }
    padding: 0
    background: null

    function reload() {
        loadedSource = source;
        opened = false;
        if (rasterId) backend.releasePdf(rasterId);
        rasterId = "";
        documentLoader.active = false;
        password.clear();
        passwordNeeded = false;
        passwordTried = false;
        canceled = false;
        fitMode = "width";
        documentLoader.active = true;
        opened = true;
    }
    function fit() {
        if (!ready || !view || view.width <= 24 || view.height <= 24 || !fitMode)
            return;
        const scale = fitMode === "page"
            ? Math.min((view.width - 24) / pdfDocument.maxPageWidth, (view.height - 16) / pdfDocument.maxPageHeight)
            : (view.width - 24) / pdfDocument.maxPageWidth;
        view.setScale(scale);
    }
    function zoom(factor) {
        fitMode = "";
        view.setScale(Math.max(0.1, Math.min(4, view.renderScale * factor)));
    }
    function unlock() {
        passwordTried = true;
        pdfDocument.password = password.text;
        password.clear();
    }
    function loadSource() {
        if (source.toString() && source.toString() !== loadedSource.toString())
            reload();
    }
    onSourceChanged: sourceTimer.restart()
    Component.onCompleted: sourceTimer.restart()
    Component.onDestruction: { if (backend && rasterId) backend.releasePdf(rasterId); }
    onVisibleChanged: {
        if (!visible) password.clear();
        else if (passwordNeeded) password.forceActiveFocus();
    }
    Loader {
        id: documentLoader
        active: false
        sourceComponent: PdfDocument {
            id: pdfSourceDocument
            objectName: "pdfDocument"
            Component.onCompleted: source = preview.source
            onPasswordRequired: {
                preview.passwordNeeded = true;
                if (preview.visible)
                    password.forceActiveFocus();
            }
            onStatusChanged: function(status) {
                if (status === PdfDocument.Ready) {
                    preview.rasterId = preview.backend.registerPdf(pdfSourceDocument.source.toString(), pdfSourceDocument.password);
                    preview.passwordNeeded = false;
                    password.clear();
                }
            }
        }
    }
    ColumnLayout {
        anchors.fill: parent
        spacing: 6
        RowLayout {
            Layout.fillWidth: true
            visible: preview.ready
            spacing: 3
            NativeButton {
                objectName: "pdfPreviousPage"
                implicitWidth: 26
                implicitHeight: 28
                icon.source: "icons/back.svg"
                quiet: true
                tip: "Previous page"
                enabled: !!preview.view && preview.view.currentPage > 0
                onClicked: preview.view.goToPage(preview.view.currentPage - 1)
            }
            NativeField {
                id: page
                objectName: "pdfPageNumber"
                implicitHeight: 28
                Layout.preferredWidth: 44
                padding: 4
                horizontalAlignment: Text.AlignHCenter
                Accessible.name: "PDF page number"
                inputMethodHints: Qt.ImhDigitsOnly
                validator: IntValidator { bottom: 1; top: Math.max(1, preview.pageCount) }
                Binding {
                    page.text: preview.view ? String(preview.view.currentPage + 1) : "1"
                    when: !page.activeFocus
                }
                onAccepted: {
                    if (preview.view && acceptableInput)
                        preview.view.goToPage(Number(text) - 1);
                    focus = false;
                }
            }
            Label {
                objectName: "pdfPageCount"
                text: "/ " + preview.pageCount
                font.pixelSize: 11
                color: palette.placeholderText
            }
            NativeButton {
                objectName: "pdfNextPage"
                implicitWidth: 26
                implicitHeight: 28
                icon.source: "icons/forward.svg"
                quiet: true
                tip: "Next page"
                enabled: !!preview.view && preview.view.currentPage < preview.pageCount - 1
                onClicked: preview.view.goToPage(preview.view.currentPage + 1)
            }
            Item { Layout.fillWidth: true }
        }
        RowLayout {
            Layout.fillWidth: true
            visible: preview.ready
            spacing: 3
            NativeButton {
                objectName: "pdfZoomOut"
                implicitWidth: 26
                implicitHeight: 28
                text: "−"
                quiet: true
                tip: "Zoom out"
                enabled: !!preview.view && preview.view.renderScale > 0.1
                onClicked: preview.zoom(0.8)
            }
            NativeButton {
                objectName: "pdfZoomIn"
                implicitWidth: 26
                implicitHeight: 28
                icon.source: "icons/plus.svg"
                quiet: true
                tip: "Zoom in"
                enabled: !!preview.view && preview.view.renderScale < 4
                onClicked: preview.zoom(1.25)
            }
            NativeButton {
                objectName: "pdfFitButton"
                implicitHeight: 28
                text: "Fit"
                tip: "Page sizing"
                onClicked: fitMenu.open()
                NativeMenu {
                    id: fitMenu
                    popupType: Popup.Item
                    y: parent.height
                    NativeMenuItem {
                        objectName: "pdfFitWidth"
                        text: "Fit width"
                        onTriggered: { preview.fitMode = "width"; preview.fit(); }
                    }
                    NativeMenuItem {
                        objectName: "pdfFitPage"
                        text: "Fit page"
                        onTriggered: { preview.fitMode = "page"; preview.fit(); }
                    }
                    NativeMenuItem {
                        objectName: "pdfActualSize"
                        text: "Actual size"
                        onTriggered: { preview.fitMode = ""; preview.view.setScale(1); }
                    }
                }
            }
            Label {
                objectName: "pdfZoomValue"
                Layout.fillWidth: true
                horizontalAlignment: Text.AlignRight
                text: preview.view ? Math.round(preview.view.renderScale * 100) + "%" : ""
                font.pixelSize: 11
                color: palette.placeholderText
            }
        }
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            radius: 10
            color: preview.palette.light
            border.color: preview.palette.mid
            clip: true
            Loader {
                id: viewer
                anchors.fill: parent
                anchors.margins: 4
                active: preview.ready
                visible: status === Loader.Ready
                asynchronous: true
                onLoaded: fitTimer.restart()
                sourceComponent: PdfPages {
                    objectName: "pdfView"
                    document: preview.pdfDocument
                    rasterId: preview.rasterId
                    onWidthChanged: fitTimer.restart()
                    onHeightChanged: fitTimer.restart()
                    onLinkRequested: function(link) { preview.linkRequested(link); }
                }
            }
            BusyIndicator {
                objectName: "pdfBusy"
                anchors.centerIn: parent
                running: !preview.canceled && !preview.passwordNeeded && (preview.pdfDocument && preview.pdfDocument.status === PdfDocument.Loading || viewer.status === Loader.Loading || preview.view && preview.view.currentPageRenderingStatus === Image.Loading)
                visible: running
            }
            ColumnLayout {
                anchors.centerIn: parent
                width: Math.max(0, parent.width - 24)
                visible: !preview.ready && (preview.passwordNeeded || preview.canceled || preview.empty || preview.pdfDocument && preview.pdfDocument.status === PdfDocument.Error)
                spacing: 10
                Label {
                    objectName: "pdfNotice"
                    Layout.fillWidth: true
                    text: preview.canceled ? "Preview canceled" : preview.passwordNeeded ? (preview.passwordTried ? "Incorrect password. Try again." : "This PDF is password protected.") : preview.empty ? "This PDF contains no pages." : "Cannot open this PDF.\n" + (preview.pdfDocument ? preview.pdfDocument.error : "")
                    wrapMode: Text.Wrap
                    horizontalAlignment: Text.AlignHCenter
                    font.pixelSize: 12
                    color: palette.placeholderText
                }
                NativeField {
                    id: password
                    objectName: "pdfPassword"
                    Layout.fillWidth: true
                    visible: preview.passwordNeeded && !preview.canceled
                    placeholderText: "Password"
                    Accessible.name: "PDF password"
                    echoMode: TextInput.Password
                    inputMethodHints: Qt.ImhSensitiveData | Qt.ImhNoPredictiveText
                    onAccepted: { if (text) preview.unlock(); }
                }
                GridLayout {
                    Layout.fillWidth: true
                    columns: width < 180 ? 1 : 2
                    visible: preview.passwordNeeded && !preview.canceled
                    NativeButton {
                        Layout.fillWidth: true
                        objectName: "pdfCancelUnlock"
                        text: "Cancel"
                        onClicked: { preview.canceled = true; preview.opened = false; password.clear(); documentLoader.active = false; }
                    }
                    NativeButton {
                        Layout.fillWidth: true
                        objectName: "pdfUnlock"
                        text: "Unlock"
                        primary: true
                        enabled: !!password.text
                        onClicked: preview.unlock()
                    }
                }
                NativeButton {
                    objectName: "pdfRetry"
                    Layout.alignment: Qt.AlignHCenter
                    visible: preview.canceled || !preview.passwordNeeded
                    text: preview.canceled ? "Unlock PDF" : "Retry"
                    onClicked: preview.reload()
                }
            }
        }
    }
}
