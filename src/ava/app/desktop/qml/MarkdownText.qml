pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls

TextArea {
    id: content
    required property var backend
    ContextMenu.menu: TextMenu { editor: content }
    leftPadding: padding
    rightPadding: padding
    topPadding: padding
    bottomPadding: padding
    property bool formatting: false
    readonly property color linkColor: palette.link
    readonly property color codeBackground: Theme.inset
    property string codeFont: "monospace"
    property color bubbleColor: "transparent"
    property var decorations: []
    property int layoutRevision: 0
    function scheduleFormat() {
        if (!formatting)
            Qt.callLater(formatDocument);
    }
    function formatDocument() {
        if (!backend || formatting || textFormat !== TextEdit.MarkdownText)
            return;
        formatting = true;
        decorations = backend.formatMarkdown(textDocument, linkColor, codeBackground, codeFont);
        formatting = false;
    }
    // Style before ListView measures this update, not a frame later: otherwise
    // every streamed chunk briefly restores Qt's unstyled paragraph heights.
    onTextChanged: formatDocument()
    onTextFormatChanged: {
        decorations = [];
        scheduleFormat();
    }
    onCodeFontChanged: scheduleFormat()
    onLinkColorChanged: scheduleFormat()
    onCodeBackgroundChanged: scheduleFormat()
    onFontChanged: scheduleFormat()
    onContentSizeChanged: layoutRevision++
    onWidthChanged: layoutRevision++
    Component.onCompleted: scheduleFormat()
    background: Item {
        Rectangle {
            anchors.fill: parent
            color: content.bubbleColor
            radius: Theme.cardRadius
        }
        Repeater {
            model: content.decorations
            delegate: Rectangle {
                id: decoration
                required property var modelData
                objectName: modelData.kind === "code" ? "markdownCodeBackground" : "markdownQuoteBorder"
                readonly property rect first: {
                    content.layoutRevision;
                    return content.positionToRectangle(Math.min(modelData.start, content.length));
                }
                readonly property rect last: {
                    content.layoutRevision;
                    return content.positionToRectangle(Math.min(modelData.end, content.length));
                }
                x: content.leftPadding
                y: first.y - (modelData.kind === "code" ? 6 : 0)
                width: modelData.kind === "code" ? (content.width - content.leftPadding - content.rightPadding) : 3
                height: last.y + last.height - first.y + (modelData.kind === "code" ? 12 : 0)
                radius: modelData.kind === "code" ? 8 : 1
                color: content.codeBackground
            }
        }
    }
}
