pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Pane {
    id: pane
    objectName: "filePane"
    padding: 0
    background: null
    required property var backend
    required property string codeFont
    required property string rootPath
    property string projectId: ""
    readonly property bool remote: !!treeModel && treeModel.remote === true
    property string requestedPath: ""
    property var treeModel: null
    property var fileState: ({})
    readonly property PdfPreview pdfPreview: pdfLoader.item as PdfPreview
    signal titleUpdated(string title)
    function openPath(path) {
        if (!backend || !path || path === rootPath) return
        if (remote) { treeModel.preview(path); return; }
        const reloading = fileState.path === path
        fileState = backend.previewFile(rootPath, path)
        if (reloading && fileState.kind === "pdf" && pdfPreview) pdfPreview.reload()
        titleUpdated(fileState.name || "Files")
    }
    Component.onCompleted: { treeModel = backend.createFileModel(rootPath, projectId); if (requestedPath) openPath(requestedPath) }
    Component.onDestruction: { if (backend && treeModel) backend.releasePreview(treeModel) }
    onRequestedPathChanged: { if (treeModel && requestedPath !== fileState.path) openPath(requestedPath) }
    Connections {
        target: pane.remote ? pane.treeModel : null
        function onPreviewReady(state) { pane.fileState = state; pane.titleUpdated(state.name || "Files"); }
    }
    SplitView {
        id: panels
        objectName: "filePanels"
        anchors.fill: parent
        handle: Rectangle {
            implicitWidth: 5
            color: SplitHandle.hovered || SplitHandle.pressed ? pane.palette.highlight : "transparent"
            Rectangle { anchors.centerIn: parent; width: 1; height: parent.height; color: "#55879189" }
            HoverHandler { cursorShape: Qt.SplitHCursor }
        }
        ColumnLayout {
            id: treePanel
            objectName: "fileTreePanel"
            SplitView.preferredWidth: 160
            SplitView.minimumWidth: 95
            SplitView.maximumWidth: Math.max(95, panels.width - 150)
            spacing: 8
            Label { Layout.fillWidth: true; text: pane.rootPath.split("/").pop(); font.pixelSize: 11; font.weight: Font.DemiBold; elide: Text.ElideMiddle; color: palette.placeholderText; NativeToolTip { text: pane.backend.workspaceLabel(pane.projectId) + "\n" + pane.rootPath; visible: rootHover.hovered; palette: pane.palette } HoverHandler { id: rootHover } }
            RowLayout {
                visible: pane.remote
                Layout.fillWidth: true
                Label { Layout.fillWidth: true; text: pane.remote && pane.treeModel.loading ? "Loading files…" : pane.backend.workspaceLabel(pane.projectId); font.pixelSize: 11; elide: Text.ElideRight; color: palette.placeholderText }
                NativeButton { objectName: "refreshFileTree"; quiet: true; implicitHeight: 26; icon.source: "icons/reload.svg"; tip: "Reload file tree"; onClicked: pane.treeModel.refresh() }
            }
            Label { objectName: "fileTreeNotice"; Layout.fillWidth: true; visible: pane.remote && !!pane.treeModel.error; text: pane.remote ? pane.treeModel.error : ""; wrapMode: Text.Wrap; font.pixelSize: 11; color: palette.placeholderText }
            TreeView {
                id: tree
                objectName: "fileTree"
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                model: pane.treeModel
                rootIndex: pane.treeModel ? pane.treeModel.rootIndex : undefined
                reuseItems: true
                onExpanded: function(row, depth) { if (pane.remote) pane.treeModel.setExpanded(index(row, 0), true); }
                onCollapsed: function(row, recursively) { if (pane.remote) pane.treeModel.setExpanded(index(row, 0), false); }
                columnWidthProvider: function(column) { return column === 0 ? width : 0 }
                ScrollBar.vertical: ScrollBar {}
                delegate: TreeViewDelegate {
                    id: entry
                    required property string fileName
                    required property string filePath
                    required property bool directory
                    objectName: "file_" + fileName
                    implicitWidth: tree.width
                    implicitHeight: 30
                    indentation: 14
                    indicator: Image {
                        x: entry.leftMargin + entry.depth * entry.indentation
                        y: (entry.height - height) / 2
                        width: 12; height: 12
                        visible: entry.hasChildren
                        source: "icons/back.svg"
                        rotation: entry.expanded ? -90 : 180
                        opacity: 0.65
                    }
                    leftMargin: 2
                    rightMargin: 3
                    text: fileName
                    font.pixelSize: 12
                    highlighted: pane.fileState.path === filePath
                    onClicked: { if (directory) tree.toggleExpanded(row); else pane.openPath(filePath) }
                    NativeToolTip { text: entry.filePath; visible: entry.hovered; palette: pane.palette }
                    background: Rectangle { radius: 7; color: entry.highlighted ? entry.palette.alternateBase : entry.hovered ? entry.palette.light : "transparent" }
                    contentItem: Label { text: entry.text; font: entry.font; elide: Text.ElideRight; color: entry.palette.text; verticalAlignment: Text.AlignVCenter }
                }
            }
        }
        ColumnLayout {
            objectName: "fileContentPanel"
            SplitView.fillWidth: true
            SplitView.minimumWidth: 145
            spacing: 8
            RowLayout {
                Layout.fillWidth: true
                Label { Layout.fillWidth: true; text: pane.fileState.relative || "Preview"; font.pixelSize: 11; color: palette.placeholderText; elide: Text.ElideMiddle }
                NativeButton { objectName: "copyFileButton"; quiet: true; implicitHeight: 28; icon.source: "icons/copy.svg"; tip: "Copy file contents"; visible: !!pane.fileState.text; onClicked: pane.backend.copyText(pane.fileState.text) }
                NativeButton { objectName: "refreshFilesButton"; quiet: true; implicitHeight: 28; icon.source: "icons/reload.svg"; tip: "Reload file"; enabled: !!pane.fileState.path; onClicked: pane.openPath(pane.fileState.path) }
            }
            Label { Layout.fillWidth: true; visible: !!pane.fileState.notice; text: pane.fileState.notice || ""; wrapMode: Text.Wrap; font.pixelSize: 12; color: palette.placeholderText }
            ScrollView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                visible: pane.fileState.kind === "markdown" && (pane.fileState.text || "").length < 64000
                clip: true
                MarkdownText {
                    objectName: "filePreview"
                    backend: pane.backend
                    codeFont: pane.codeFont
                    text: pane.fileState.kind === "markdown" && (pane.fileState.text || "").length < 64000 ? pane.fileState.text : ""
                    textFormat: TextEdit.MarkdownText
                    font.pixelSize: 14
                    readOnly: true
                    selectByMouse: true
                    wrapMode: TextEdit.Wrap
                    onLinkActivated: function(link) { pane.backend.openLink(link) }
                }
            }
            CodePreview {
                objectName: "codePreview"
                Layout.fillWidth: true
                Layout.fillHeight: true
                visible: pane.fileState.kind === "text" || pane.fileState.kind === "markdown" && (pane.fileState.text || "").length >= 64000
                backend: pane.backend
                codeFont: pane.codeFont
                text: pane.fileState.kind === "text" || pane.fileState.kind === "markdown" && (pane.fileState.text || "").length >= 64000 ? pane.fileState.text : ""
                filename: pane.fileState.name || ""
            }
            Image { Layout.fillWidth: true; Layout.fillHeight: true; visible: pane.fileState.kind === "image"; source: visible ? pane.fileState.source || "" : ""; fillMode: Image.PreserveAspectFit; asynchronous: true; sourceSize.width: 1600; sourceSize.height: 1600 }
            Loader {
                id: pdfLoader
                Layout.fillWidth: true
                Layout.fillHeight: true
                active: pane.fileState.kind === "pdf"
                visible: status === Loader.Ready
                asynchronous: true
                sourceComponent: PdfPreview {
                    backend: pane.backend
                    source: pane.fileState.source || ""
                    onLinkRequested: function(link) { pane.backend.openLink(link.toString()); }
                }
            }
            Item { Layout.fillWidth: true; Layout.fillHeight: true; visible: !pane.fileState.kind || pane.fileState.kind === "unsupported" || pane.fileState.kind === "directory" || pane.fileState.kind === "loading"; Label { anchors.centerIn: parent; width: Math.max(0, parent.width - 20); text: pane.fileState.kind === "loading" ? "" : "Choose a file to preview"; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.WordWrap; font.pixelSize: 12; color: palette.placeholderText } }
            NativeButton { objectName: "attachPreviewButton"; Layout.alignment: Qt.AlignRight; visible: ["text", "markdown", "image"].includes(pane.fileState.kind); enabled: !!pane.backend && !!pane.backend.chatId; icon.source: "icons/plus.svg"; text: "Add to message"; onClicked: pane.backend.addPreviewAttachment(pane.fileState.attachmentPath || pane.fileState.path, pane.fileState.name) }
        }
    }
}
