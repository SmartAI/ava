pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls

Pane {
    id: preview
    required property var backend
    required property string codeFont
    required property string text
    required property string filename
    readonly property bool dark: palette.base.hslLightness < 0.5
    property var document: null
    property real measuredWidth: 0
    readonly property int lineCount: document ? document.count : 0
    property int startLine: -1
    property int startColumn: 0
    property int endLine: -1
    property int endColumn: 0
    readonly property int firstLine: Math.min(startLine, endLine)
    readonly property int lastLine: Math.max(startLine, endLine)
    readonly property int firstColumn: startLine < endLine || startLine === endLine && startColumn < endColumn ? startColumn : endColumn
    readonly property int lastColumn: startLine < endLine || startLine === endLine && startColumn < endColumn ? endColumn : startColumn
    readonly property int lineHeight: Math.ceil(metrics.height) + 5
    readonly property real gutterWidth: Math.max(3, String(lineCount).length) * metrics.averageCharacterWidth + 20
    signal selectionChanged()
    onStartLineChanged: selectionChanged()
    onEndLineChanged: selectionChanged()
    onStartColumnChanged: selectionChanged()
    onEndColumnChanged: selectionChanged()
    function reload() {
        if (!document || !backend) return
        measuredWidth = 0
        document.load(text, filename, dark)
        startLine = endLine = -1
    }
    onTextChanged: Qt.callLater(reload)
    onFilenameChanged: Qt.callLater(reload)
    onDarkChanged: Qt.callLater(reload)
    Component.onCompleted: { document = backend.createCodeDocument(); reload() }
    Component.onDestruction: { if (backend && document) backend.releasePreview(document) }
    function copySelection() {
        if (document && startLine >= 0)
            backend.copyText(document.selectedText(startLine, startColumn, endLine, endColumn));
    }
    function selectAll() {
        startLine = 0; startColumn = 0;
        endLine = lineCount - 1; endColumn = 2147483647;
    }
    ContextMenu.menu: NativeMenu {
        palette: preview.palette
        onClosed: preview.forceActiveFocus()
        NativeMenuItem {
            objectName: "codeCopySelection"
            text: "Copy"
            enabled: preview.startLine >= 0 && (preview.startLine !== preview.endLine || preview.startColumn !== preview.endColumn)
            onTriggered: preview.copySelection()
        }
        NativeMenuItem {
            objectName: "codeSelectAll"
            text: "Select all"
            enabled: preview.lineCount > 0
            onTriggered: preview.selectAll()
        }
    }
    padding: 0
    focus: true
    background: Rectangle { color: preview.palette.base; radius: 10; border.color: preview.palette.mid }
    FontMetrics { id: metrics; font.family: preview.codeFont; font.pixelSize: 13 }
    Keys.onPressed: function(event) {
        if (event.matches(StandardKey.Copy) && preview.startLine >= 0) {
            preview.copySelection()
        } else if (event.matches(StandardKey.SelectAll)) {
            preview.selectAll()
        } else if (event.key === Qt.Key_PageDown || event.key === Qt.Key_Down) {
            lines.contentY = Math.min(Math.max(0, lines.contentHeight - lines.height), lines.contentY + (event.key === Qt.Key_Down ? preview.lineHeight : lines.height))
        } else if (event.key === Qt.Key_PageUp || event.key === Qt.Key_Up) {
            lines.contentY = Math.max(0, lines.contentY - (event.key === Qt.Key_Up ? preview.lineHeight : lines.height))
        } else if (event.key === Qt.Key_Home) lines.positionViewAtBeginning()
        else if (event.key === Qt.Key_End) lines.positionViewAtEnd()
        else return
        event.accepted = true
    }
    Flickable {
        id: horizontal
        objectName: "codeHorizontalScroll"
        anchors.fill: parent
        anchors.margins: 1
        clip: true
        contentWidth: Math.max(width, preview.measuredWidth, preview.gutterWidth + (preview.document ? preview.document.columns : 0) * metrics.averageCharacterWidth + 30)
        contentHeight: height
        flickableDirection: Flickable.HorizontalFlick
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.horizontal: ScrollBar {}
        ListView {
            id: lines
            objectName: "codeLines"
            width: horizontal.contentWidth
            height: horizontal.height
            clip: true
            model: preview.document
            reuseItems: true
            cacheBuffer: preview.lineHeight * 3
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar { parent: horizontal; anchors.right: parent.right; anchors.top: parent.top; anchors.bottom: parent.bottom }
            delegate: Item {
                id: line
                objectName: "codeLine"
                required property int index
                required property int lineNumber
                required property string lineText
                required property string lineHtml
                readonly property alias editor: code
                width: lines.width
                height: preview.lineHeight
                TextEdit {
                    id: code
                    objectName: "codeLineText"
                    x: preview.gutterWidth
                    width: parent.width - x
                    height: parent.height
                    text: line.lineHtml
                    textFormat: TextEdit.RichText
                    wrapMode: TextEdit.NoWrap
                    readOnly: true
                    persistentSelection: true
                    font.family: preview.codeFont
                    font.pixelSize: 13
                    color: preview.palette.text
                    selectionColor: preview.palette.highlight
                    selectedTextColor: preview.palette.highlightedText
                    function updateSelection() {
                        if (preview.firstLine < 0 || line.index < preview.firstLine || line.index > preview.lastLine) deselect()
                        else select(line.index === preview.firstLine ? preview.firstColumn : 0, line.index === preview.lastLine ? preview.lastColumn : length)
                    }
                    onTextChanged: Qt.callLater(updateSelection)
                    onContentSizeChanged: preview.measuredWidth = Math.max(preview.measuredWidth, contentWidth + preview.gutterWidth + 30)
                    Connections { target: preview; function onSelectionChanged() { code.updateSelection() } }
                }
                Rectangle {
                    x: horizontal.contentX
                    width: preview.gutterWidth
                    height: parent.height
                    color: preview.palette.window
                    Text { anchors.fill: parent; anchors.rightMargin: 10; text: line.lineNumber || "↳"; horizontalAlignment: Text.AlignRight; font.family: preview.codeFont; font.pixelSize: 13; color: preview.palette.placeholderText }
                }
                ListView.onReused: code.updateSelection()
            }
        }
    }
    MouseArea {
        anchors.fill: horizontal
        anchors.rightMargin: 12
        anchors.bottomMargin: 12
        acceptedButtons: Qt.LeftButton
        cursorShape: Qt.IBeamCursor
        function selectAt(mouse, start) {
            const row = Math.max(0, Math.min(preview.lineCount - 1, Math.floor((mouse.y + lines.contentY) / preview.lineHeight)))
            const item = lines.itemAtIndex(row)
            if (!item) return
            // itemAtIndex returns QQuickItem; this delegate exposes its TextEdit alias.
            // qmllint disable missing-property
            const code = item.editor
            // qmllint enable missing-property
            const column = code.positionAt(Math.max(0, mouse.x + horizontal.contentX - preview.gutterWidth), preview.lineHeight / 2)
            if (start) { preview.startLine = row; preview.startColumn = column }
            preview.endLine = row; preview.endColumn = column
        }
        onPressed: function(mouse) { preview.forceActiveFocus(); selectAt(mouse, true) }
        onPositionChanged: function(mouse) { if (pressed) selectAt(mouse, false) }
        onWheel: function(wheel) { lines.contentY = Math.max(0, Math.min(Math.max(0, lines.contentHeight - lines.height), lines.contentY - (wheel.pixelDelta.y || wheel.angleDelta.y / 2))); wheel.accepted = true }
    }
}
