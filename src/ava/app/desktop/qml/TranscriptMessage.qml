pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Pane {
    id: message
    required property var backend
    required property string codeFont
    property int readingSize: 15
    required property string kind
    required property string heading
    required property string body
    required property string detail
    required property var attachments
    property bool expanded: false
    signal expansionToggled
    readonly property bool user: kind === "user"
    readonly property bool tool: kind === "tool" || kind === "error" && !!detail
    readonly property bool compact: tool || kind === "reasoning"
    readonly property bool large: body.length >= 64000
    readonly property var toolArguments: {
        try {
            const args = JSON.parse(detail);
            return args && typeof args === "object" ? args : ({});
        } catch (_) {
            return ({});
        }
    }
    readonly property bool markdown: {
        if (kind === "error")
            return false;
        if (!tool)
            return true;
        if (heading !== "read")
            return false;
        const path = toolArguments.path || toolArguments.file_path;
        if (typeof path === "string" && /\.[^/.]+$/.test(path))
            return /\.m(?:d|arkdown)$/i.test(path);
        return /(^|\n)(#{1,6} |[-*] |\*\*|```)/.test(body);
    }
    readonly property string summary: {
        if (!tool)
            return heading;
        const args = toolArguments;
        const candidate = args.path || args.file_path;
        const path = typeof candidate === "string" ? candidate : "";
        if (heading === "Browser" || heading === "browser") {
            const actions = {snapshot: "Inspect page", screenshot: "Capture page", navigate: "Open page",
                back: "Go back", forward: "Go forward", reload: "Reload page", click: "Click element",
                fill: "Fill field", press: "Press key", scroll: "Scroll page", select: "Choose option", wait: "Wait for page"};
            const label = actions[args.action] || "Use browser";
            return args.action === "navigate" && typeof args.url === "string" ? label + " · " + args.url
                 : args.action === "press" && typeof args.key === "string" ? label + " · " + args.key : label;
        }
        if (heading === "read" && path)
            return "Read " + path;
        if (heading === "write" && path)
            return "Write " + path;
        if (heading === "edit" && path)
            return "Edit " + path;
        if (heading === "bash" && typeof args.command === "string")
            return args.command.split("\n")[0];
        return heading;
    }
    function toggleOutput() {
        expansionToggled();
    }
    padding: compact && expanded ? 12 : 0
    background: Rectangle {
        visible: message.compact && message.expanded
        radius: 12
        color: message.palette.base
        border.color: message.palette.mid
    }
    contentItem: ColumnLayout {
        spacing: 9
        RowLayout {
            visible: message.compact || message.kind === "error" || message.kind === "notice"
            Layout.fillWidth: true
            spacing: 6
            NativeButton {
                objectName: "expandMessage"
                Layout.fillWidth: true
                implicitHeight: 30
                visible: message.compact
                quiet: true
                text: message.summary
                icon.source: message.expanded ? "icons/down.svg" : "icons/chevron.svg"
                icon.width: 14
                icon.height: 14
                tip: message.expanded ? "Hide output" : "Show complete output"
                contentItem: RowLayout {
                    spacing: 8
                    Image {
                        source: message.expanded ? "icons/down.svg" : "icons/chevron.svg"
                        sourceSize.width: 14
                        sourceSize.height: 14
                        opacity: 0.55
                    }
                    Label {
                        objectName: "activitySummary"
                        Layout.fillWidth: true
                        text: message.summary
                        elide: Text.ElideMiddle
                        font.pixelSize: 12
                        color: message.kind === "error" ? "#b3664e" : message.palette.placeholderText
                    }
                    Label {
                        visible: message.body === "Running…"
                        text: "Running…"
                        font.pixelSize: 11
                        color: message.palette.placeholderText
                    }
                    Label {
                        visible: message.kind === "error"
                        text: "Failed"
                        font.pixelSize: 11
                        color: "#b3664e"
                    }
                }
                onClicked: message.toggleOutput()
            }
            Label {
                visible: !message.compact
                Layout.fillWidth: true
                text: message.heading
                color: message.kind === "error" ? "#b3664e" : message.palette.placeholderText
                font.pixelSize: 11
                font.weight: Font.DemiBold
            }
            NativeButton {
                objectName: "copyActivityOutput"
                visible: !message.compact || message.expanded
                implicitHeight: 26
                icon.source: "icons/copy.svg"
                quiet: true
                tip: "Copy output"
                onClicked: message.backend.copyText(message.body)
            }
        }
        Loader {
            id: outputLoader
            Layout.fillWidth: !message.user
            Layout.preferredWidth: message.user ? Math.min(620, message.availableWidth * 0.82) : message.availableWidth
            Layout.alignment: message.user ? Qt.AlignRight : Qt.AlignLeft
            active: (!message.compact || message.expanded) && !!message.body
            visible: active
            sourceComponent: ColumnLayout {
                spacing: 12
                TextArea {
                    id: toolDetail
                    ContextMenu.menu: TextMenu { editor: toolDetail }
                    Layout.fillWidth: true
                    visible: message.expanded && !!message.detail
                    text: visible ? message.detail : ""
                    readOnly: true
                    selectByMouse: true
                    wrapMode: TextEdit.WrapAnywhere
                    textFormat: TextEdit.PlainText
                    font.family: message.codeFont
                    font.pixelSize: 11
                    color: message.palette.placeholderText
                    background: null
                    padding: 0
                }
                Loader {
                    Layout.fillWidth: true
                    active: !message.large
                    visible: active
                    sourceComponent: MarkdownText {
                        objectName: message.kind === "assistant" ? "assistantMarkdown" : "messageBody"
                        backend: message.backend
                        codeFont: message.codeFont
                        text: message.body
                        readOnly: true
                        selectByMouse: true
                        wrapMode: TextEdit.Wrap
                        textFormat: message.markdown ? TextEdit.MarkdownText : TextEdit.PlainText
                        font.family: message.tool && !message.markdown ? message.codeFont : message.font.family
                        font.pixelSize: message.tool ? Math.max(12, message.readingSize - 2) : message.readingSize
                        color: message.kind === "error" ? "#b3664e" : message.palette.text
                        bubbleColor: message.user ? message.palette.light : "transparent"
                        padding: message.user ? 12 : 0
                        onLinkActivated: function (link) {
                            message.backend.openLink(link);
                        }
                        Accessible.name: message.heading + ": " + message.body
                    }
                }
                Loader {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 420
                    active: message.large
                    visible: active
                    sourceComponent: CodePreview {
                        objectName: "activityCodePreview"
                        backend: message.backend
                        codeFont: message.codeFont
                        text: message.body
                        filename: message.markdown ? "output.md" : "output.txt"
                    }
                }
            }
        }
        Flow {
            Layout.preferredWidth: Math.min(620, message.availableWidth * 0.82)
            Layout.alignment: message.user ? Qt.AlignRight : Qt.AlignLeft
            visible: message.attachments.length > 0
            layoutDirection: message.user ? Qt.RightToLeft : Qt.LeftToRight
            spacing: 6
            Repeater {
                model: message.attachments
                delegate: NativeButton {
                    id: attachment
                    required property var modelData
                    objectName: "transcriptAttachment_" + modelData.display_path
                    width: Math.min(240, message.availableWidth * 0.82)
                    height: 44
                    padding: 8
                    enabled: !!modelData.path
                    opacity: 1
                    focusPolicy: modelData.path ? Qt.StrongFocus : Qt.NoFocus
                    Accessible.name: (modelData.path ? "View image " : "") + modelData.display_path
                    tip: modelData.path ? "View image" : modelData.display_path
                    onClicked: if (modelData.path) message.backend.openToolImage(modelData)
                    contentItem: RowLayout {
                        spacing: 8
                        Image {
                            source: attachment.modelData.kind === "image" ? "icons/image.svg" : "icons/file.svg"
                            sourceSize.width: 20
                            sourceSize.height: 20
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2
                            Label {
                                Layout.fillWidth: true
                                text: attachment.modelData.display_path
                                elide: Text.ElideMiddle
                                font.pixelSize: 12
                            }
                            Label {
                                text: (attachment.modelData.kind === "image" ? "Image" : "File") + " · " + Math.max(1, Math.round(attachment.modelData.byte_size / 1024)) + " KB"
                                font.pixelSize: 10
                                color: palette.placeholderText
                            }
                        }
                    }
                }
            }
        }
        NativeButton {
            visible: message.kind === "assistant"
            Layout.alignment: Qt.AlignLeft
            implicitWidth: 26
            implicitHeight: 26
            icon.source: "icons/copy.svg"
            icon.width: 14
            icon.height: 14
            quiet: true
            tip: "Copy response"
            onClicked: message.backend.copyText(message.body)
        }
    }
}
