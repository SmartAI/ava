pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Window
import QtQuick.Controls
import QtQuick.Pdf
import QtQuick.Shapes

FocusScope {
    id: pages
    readonly property var hostWindow: Window.window
    required property PdfDocument document
    required property string rasterId
    // The preview supplies its initial fit after layout. Creating sheets sooner
    // would render at a temporary size and immediately reload every image.
    property real renderScale: 0
    property int currentPage: 0
    property int currentPageRenderingStatus: Image.Null
    property int pendingPage: -1
    property real pendingOffset: 0
    property PdfSelection menuSelection: null
    signal linkRequested(url link)
    Timer { id: layoutTimer; interval: 0; onTriggered: pages.layoutPages() }
    Timer { id: statusTimer; interval: 0; onTriggered: pages.updateStatus() }

    function updateStatus() {
        const sheet = table.itemAtCell(Qt.point(0, currentPage)) as Sheet;
        currentPageRenderingStatus = sheet ? sheet.renderingStatus : Image.Loading;
    }
    function layoutPages() {
        if (!table.laidOut)
            return;
        // Run after width/scale bindings and the current layout have settled.
        table.forceLayout();
        if (pendingPage >= 0 && table.rows) {
            const target = pendingPage;
            pendingPage = -1;
            table.positionViewAtRow(target, TableView.AlignTop, pendingOffset * renderScale);
            table.contentX = Math.max(0, (table.contentWidth - table.width) / 2);
            currentPage = target;
        }
        updateStatus();
    }
    function setScale(value) {
        if (value <= 0 || Math.abs(value - renderScale) < 0.00001)
            return;
        const sheet = table.itemAtCell(Qt.point(0, currentPage)) as Sheet;
        pendingPage = currentPage;
        pendingOffset = sheet ? Math.max(0, table.contentY - sheet.y) / renderScale : 0;
        renderScale = value;
        layoutTimer.restart();
    }
    function goToPage(number) {
        goToLocation(number, Qt.point(0, 0), 0);
    }
    function goToLocation(number, location, zoom) {
        if (!document || number < 0 || number >= document.pageCount)
            return;
        pendingPage = number;
        pendingOffset = Math.max(0, location.y);
        if (zoom > 0)
            renderScale = Math.max(0.1, Math.min(4, zoom));
        layoutTimer.restart();
    }
    onCurrentPageChanged: statusTimer.restart()
    onWidthChanged: layoutTimer.restart()
    onHeightChanged: layoutTimer.restart()
    Keys.onPressed: function(event) {
        const sheet = table.itemAtCell(Qt.point(0, currentPage)) as Sheet;
        if (event.matches(StandardKey.Copy) && sheet)
            sheet.selection.copyToClipboard();
        else if (event.matches(StandardKey.SelectAll) && sheet)
            sheet.selection.selectAll();
        else if (sheet && (event.key === Qt.Key_Menu || (event.key === Qt.Key_F10 && event.modifiers & Qt.ShiftModifier))) {
            menuSelection = sheet.selection;
            selectionMenu.popup(table, Qt.point(table.width / 2, table.height / 2));
        }
        else if (event.key === Qt.Key_PageDown && document)
            goToPage(Math.min(document.pageCount - 1, currentPage + 1));
        else if (event.key === Qt.Key_PageUp)
            goToPage(Math.max(0, currentPage - 1));
        else
            return;
        event.accepted = true;
    }
    TableView {
        id: table
        objectName: "pdfPages"
        property bool laidOut: false
        width: Math.max(0, parent.width - 12)
        height: parent.height
        clip: true
        model: pages.document && pages.renderScale > 0 ? pages.document.pageCount : 0
        reuseItems: false
        animate: false
        acceptedButtons: Qt.NoButton
        boundsBehavior: Flickable.StopAtBounds
        columnWidthProvider: function(column) { return Math.max(width, (pages.document ? pages.document.maxPageWidth : 0) * pages.renderScale + 16); }
        rowHeightProvider: function(row) { return (pages.document ? pages.document.pagePointSize(row).height : 0) * pages.renderScale + 16; }
        onLayoutChanged: {
            if (!laidOut) {
                laidOut = true;
                layoutTimer.restart();
            }
            pages.updateStatus();
        }
        onTopRowChanged: { if (moving) pages.currentPage = Math.max(0, topRow); }
        ScrollBar.horizontal: ScrollBar {}
        ScrollBar.vertical: ScrollBar {
            parent: pages
            x: pages.width - width
            height: pages.height
        }
        delegate: Sheet {}
    }
    component Sheet: Item {
        id: sheet
        objectName: "pdfSheet"
        required property int index
        property int renderAttempt: 0
        readonly property size pointSize: pages.document ? pages.document.pagePointSize(index) : Qt.size(1, 1)
        readonly property int renderingStatus: pageImage.status
        onRenderingStatusChanged: statusTimer.restart()
        property alias selection: selection
        Rectangle {
            id: paper
            anchors.centerIn: parent
            width: sheet.pointSize.width * pages.renderScale
            height: sheet.pointSize.height * pages.renderScale
            color: "white"
            Image {
                id: pageImage
                objectName: "pdfPageImage"
                anchors.fill: parent
                source: pages.rasterId ? "image://ava-pdf/" + pages.rasterId + "/" + sheet.index + "/" + sheet.renderAttempt : ""
                currentFrame: sheet.index
                asynchronous: true
                cache: false
                fillMode: Image.PreserveAspectFit
                // Image already applies the window's DPR to image-provider
                // requests. sourceSize stays in logical pixels; cap the final raster.
                readonly property real maxSourceExtent: Math.floor(4096 / Screen.devicePixelRatio)
                readonly property real rasterScale: Math.min(pages.renderScale, maxSourceExtent / sheet.pointSize.width, maxSourceExtent / sheet.pointSize.height)
                sourceSize.width: Math.ceil(sheet.pointSize.width * rasterScale)
                sourceSize.height: Math.ceil(sheet.pointSize.height * rasterScale)
            }
            Column {
                anchors.centerIn: parent
                width: Math.max(0, parent.width - 24)
                visible: pageImage.status === Image.Error
                spacing: 8
                Label {
                    width: parent.width
                    text: "This page could not be rendered."
                    color: "#666666"
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.Wrap
                    font.pixelSize: 12
                }
                NativeButton {
                    objectName: "pdfRenderRetry_" + sheet.index
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "Retry"
                    onClicked: sheet.renderAttempt++
                }
            }
            Shape {
                anchors.fill: parent
                ShapePath {
                    strokeWidth: -1
                    fillColor: "#664589e8"
                    scale: Qt.size(pages.renderScale, pages.renderScale)
                    // Qt PDF's QPolygonF list works at runtime but lacks QML type metadata.
                    // qmllint disable unresolved-type
                    PathMultiline { paths: selection.geometry }
                    // qmllint enable unresolved-type
                }
            }
            PdfSelection {
                id: selection
                objectName: "pdfSelection"
                anchors.fill: parent
                // Configure page/scale before attaching the document: changing
                // scale with a document already attached parses an empty selection.
                Component.onCompleted: document = pages.document
                page: sheet.index
                renderScale: pages.renderScale
                from: selectDrag.centroid.pressPosition
                to: selectDrag.centroid.position
                hold: !selectDrag.active && !selectTap.pressed
            }
            DragHandler {
                id: selectDrag
                acceptedDevices: PointerDevice.Mouse | PointerDevice.Stylus
                acceptedButtons: Qt.LeftButton
                target: null
                onActiveChanged: {
                    if (active) {
                        pages.currentPage = sheet.index;
                        selection.forceActiveFocus();
                    }
                }
            }
            TapHandler {
                id: selectTap
                acceptedButtons: Qt.LeftButton
                onTapped: {
                    pages.currentPage = sheet.index;
                    selection.clear();
                    selection.forceActiveFocus();
                }
            }
            TapHandler {
                acceptedButtons: Qt.RightButton
                onTapped: function(eventPoint) {
                    pages.menuSelection = selection;
                    selectionMenu.popup(paper, eventPoint.position);
                }
            }
            Repeater {
                model: PdfLinkModel {
                    page: sheet.index
                    // Attaching first would also parse default page 0 for every sheet.
                    Component.onCompleted: document = pages.document
                }
                delegate: PdfLinkDelegate {
                    objectName: "pdfLink_" + sheet.index
                    x: rectangle.x * pages.renderScale
                    y: rectangle.y * pages.renderScale
                    width: rectangle.width * pages.renderScale
                    height: rectangle.height * pages.renderScale
                    onTapped: function(link) {
                        if (link.page >= 0)
                            pages.goToLocation(link.page, link.location, link.zoom);
                        else
                            pages.linkRequested(url);
                    }
                }
            }
        }
    }
    NativeMenu {
        id: selectionMenu
        onClosed: Qt.callLater(function() { pages.hostWindow.requestActivate(); pages.forceActiveFocus(); })
        NativeMenuItem {
            objectName: "pdfCopySelection"
            text: "Copy"
            enabled: !!pages.menuSelection && !!pages.menuSelection.text
            onTriggered: pages.menuSelection.copyToClipboard()
        }
        NativeMenuItem {
            objectName: "pdfSelectPage"
            text: "Select all on this page"
            onTriggered: { if (pages.menuSelection) pages.menuSelection.selectAll(); }
        }
    }
}
