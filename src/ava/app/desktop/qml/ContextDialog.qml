pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

NativeDialog {
    id: dialog
    objectName: "contextDialog"
    required property bool dark
    property var report: ({})
    readonly property real total: Math.max(0, Number(report.estimated_tokens) || 0)
    readonly property real capacity: Math.max(0, Number(report.context_window) || 0)
    readonly property var sections: (report.sections || []).slice().sort((a, b) => b.tokens - a.tokens)
    readonly property color accent: Theme.accent
    readonly property color warning: Theme.danger
    anchors.centerIn: parent
    width: Math.min(600, parent.width - 48)
    height: Math.min(implicitHeight, parent.height - 64)
    implicitHeight: header.implicitHeight + body.implicitHeight + footer.implicitHeight + topPadding + bottomPadding
    modal: true
    padding: 24
    rightPadding: 12
    topPadding: 0
    bottomPadding: 0
    title: "Context"
    onOpened: scroll.ScrollBar.vertical.position = 0

    function number(value) {
        return Number(value).toLocaleString(Qt.locale(), "f", 0);
    }
    function percent(value, denominator) {
        if (denominator <= 0)
            return "0%";
        const amount = 100 * value / denominator;
        if (amount === 0)
            return "0%";
        if (amount < 0.1)
            return "<0.1%";
        if (amount < 100 && amount > 99.9)
            return ">99.9%";
        return amount.toLocaleString(Qt.locale(), "f", 1) + "%";
    }
    header: Item {
        implicitHeight: 64
        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 24
            anchors.rightMargin: 16
            Label {
                Layout.fillWidth: true
                text: dialog.title
                font.pixelSize: Theme.sectionTitle
                font.weight: Font.DemiBold
            }
            NativeButton {
                objectName: "closeContextButton"
                icon.source: "icons/close.svg"
                quiet: true
                tip: "Close context"
                onClicked: dialog.close()
            }
        }
    }
    contentItem: ScrollView {
        id: scroll
        objectName: "contextScroll"
        contentWidth: availableWidth
        rightPadding: 12
        clip: true
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical.policy: contentHeight > availableHeight + 1 ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff
        ColumnLayout {
            id: body
            width: scroll.availableWidth
            spacing: 20
            Rectangle {
                Layout.fillWidth: true
                implicitHeight: summary.implicitHeight + 32
                radius: 12
                color: Theme.inset
                ColumnLayout {
                    id: summary
                    anchors.fill: parent
                    anchors.margins: 16
                    spacing: 12
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 20
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 4
                            Label {
                                objectName: "contextTotal"
                                Layout.fillWidth: true
                                text: dialog.number(dialog.total)
                                font.pixelSize: 30
                                font.weight: Font.DemiBold
                            }
                            Label {
                                text: "Estimated tokens"
                                font.pixelSize: 12
                                color: palette.placeholderText
                            }
                        }
                        ColumnLayout {
                            spacing: 4
                            Label {
                                objectName: "contextWindowShare"
                                Layout.alignment: Qt.AlignRight
                                text: dialog.capacity > 0 ? dialog.percent(dialog.total, dialog.capacity) : "Unknown"
                                font.pixelSize: dialog.capacity > 0 ? 24 : 17
                                font.weight: Font.DemiBold
                                color: dialog.capacity > 0 && dialog.total > dialog.capacity ? dialog.warning : dialog.palette.text
                            }
                            Label {
                                Layout.alignment: Qt.AlignRight
                                text: "Window usage"
                                font.pixelSize: 12
                                color: palette.placeholderText
                            }
                        }
                    }
                    Rectangle {
                        objectName: "contextWindowTrack"
                        Layout.fillWidth: true
                        implicitHeight: 8
                        radius: 4
                        color: Theme.border
                        Rectangle {
                            objectName: "contextWindowFill"
                            height: parent.height
                            width: parent.width * (dialog.capacity > 0 ? Math.min(1, dialog.total / dialog.capacity) : 0)
                            radius: parent.radius
                            color: dialog.total > dialog.capacity ? dialog.warning : dialog.accent
                        }
                    }
                    Label {
                        objectName: "contextCapacity"
                        Layout.fillWidth: true
                        text: dialog.capacity > 0 ? dialog.number(dialog.capacity) + " token window" : "This model has no reported context limit."
                        wrapMode: Text.Wrap
                        font.pixelSize: 12
                        color: palette.placeholderText
                    }
                }
            }
            Label {
                objectName: "contextOverflow"
                Layout.fillWidth: true
                visible: dialog.capacity > 0 && dialog.total > dialog.capacity
                text: "Estimated context exceeds this model’s window."
                wrapMode: Text.Wrap
                font.pixelSize: 12
                color: dialog.warning
            }
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 5
                Label {
                    text: "What fills the context"
                    font.pixelSize: 14
                    font.weight: Font.DemiBold
                }
                Label {
                    objectName: "contextShareNote"
                    Layout.fillWidth: true
                    text: "Share of estimated tokens · largest first"
                    wrapMode: Text.Wrap
                    font.pixelSize: 12
                    color: palette.placeholderText
                }
            }
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 16
                Repeater {
                    model: dialog.sections
                    delegate: ColumnLayout {
                        id: section
                        required property var modelData
                        readonly property real share: dialog.total > 0 ? modelData.tokens / dialog.total : 0
                        Layout.fillWidth: true
                        spacing: 7
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 12
                            Label {
                                objectName: "contextLabel_" + section.modelData.kind
                                Layout.fillWidth: true
                                text: section.modelData.label
                                wrapMode: Text.Wrap
                                font.pixelSize: 13
                            }
                            Label {
                                objectName: "contextTokens_" + section.modelData.kind
                                text: dialog.number(section.modelData.tokens)
                                font.pixelSize: 12
                                color: palette.placeholderText
                            }
                            Label {
                                objectName: "contextShare_" + section.modelData.kind
                                Layout.preferredWidth: 54
                                horizontalAlignment: Text.AlignRight
                                text: dialog.percent(section.modelData.tokens, dialog.total)
                                font.pixelSize: 13
                                font.weight: Font.Medium
                            }
                        }
                        Rectangle {
                            objectName: "contextTrack_" + section.modelData.kind
                            Layout.fillWidth: true
                            implicitHeight: 6
                            radius: 3
                            color: dialog.palette.light
                            Rectangle {
                                objectName: "contextFill_" + section.modelData.kind
                                height: parent.height
                                width: parent.width * Math.min(1, section.share)
                                radius: parent.radius
                                color: dialog.accent
                            }
                        }
                    }
                }
                Label {
                    objectName: "contextEmpty"
                    Layout.fillWidth: true
                    visible: dialog.total === 0
                    text: "No context tokens to show yet."
                    font.pixelSize: 13
                    color: palette.placeholderText
                    wrapMode: Text.Wrap
                }
            }
            Label {
                objectName: "contextMeasured"
                Layout.fillWidth: true
                visible: dialog.report.measured_input_tokens !== null && dialog.report.measured_input_tokens !== undefined
                text: visible ? "Last request: " + dialog.number(dialog.report.measured_input_tokens) + " input tokens reported by the provider." : ""
                font.pixelSize: 12
                color: palette.placeholderText
                wrapMode: Text.Wrap
            }
            Label {
                Layout.fillWidth: true
                visible: !!dialog.report.compacted
                text: "Older messages have been compacted into a summary."
                font.pixelSize: 12
                color: palette.placeholderText
                wrapMode: Text.Wrap
            }
        }
    }
    footer: Item {
        implicitHeight: 80
        RowLayout {
            anchors.fill: parent
            anchors.margins: 24
            spacing: 16
            Label {
                Layout.fillWidth: true
                text: "Estimates for the next request.\nActual usage may differ."
                font.pixelSize: 11
                color: palette.placeholderText
                wrapMode: Text.Wrap
            }
            NativeButton {
                objectName: "contextDoneButton"
                text: "Done"
                onClicked: dialog.close()
            }
        }
    }
}
