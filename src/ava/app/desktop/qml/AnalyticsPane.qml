pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Pane {
    id: pane
    objectName: "analyticsPane"
    required property var backend
    required property bool sidebarVisible
    readonly property var analytics: backend.analytics
    readonly property var report: analytics.data
    readonly property bool narrow: width < 760
    readonly property bool dark: palette.window.hslLightness < 0.5
    readonly property var colors: dark ? ["#95b7ee", "#9b8acb", "#d3ad71", "#70b9a6"] : ["#476fba", "#8a6eb0", "#bd9456", "#478c77"]
    property string metric: "tokens"
    property int toolsLimit: 8
    property int skillsLimit: 8
    signal closeRequested
    signal sidebarRequested
    padding: narrow ? Theme.spaceLg : Theme.spaceXl
    background: Rectangle { color: pane.palette.window }
    Component.onCompleted: analytics.activate(true)
    Component.onDestruction: { if (analytics) analytics.activate(false); }
    function number(value) { return Number(value || 0).toLocaleString(Qt.locale(), "f", 0); }
    function compact(value) {
        if (value >= 1000000) return (value / 1000000).toFixed(1) + "M";
        if (value >= 1000) return (value / 1000).toFixed(1) + "k";
        return number(value);
    }
    function duration(value) {
        const minutes = Math.floor((value || 0) / 60000);
        return minutes >= 60 ? Math.floor(minutes / 60) + "h " + minutes % 60 + "m" : minutes ? minutes + "m" : value ? "< 1m" : "0m";
    }
    function valueText(value) { return metric === "active_ms" ? duration(value) : number(value); }
    function dayLabel(day) { return Qt.formatDate(new Date(day + "T12:00:00"), "MMM d"); }
    component Card: Pane {
        id: card
        required property string title
        required property string value
        required property string detail
        padding: 16
        background: Surface {}
        contentItem: ColumnLayout {
            spacing: 8
            Label { text: card.title; color: palette.placeholderText; font.pixelSize: 12 }
            Label { text: card.value; font.pixelSize: 28; font.weight: Font.DemiBold }
            Label { Layout.fillWidth: true; text: card.detail; color: palette.placeholderText; font.pixelSize: 11; wrapMode: Text.WordWrap }
        }
    }
    ColumnLayout {
        anchors.fill: parent
        spacing: 20
        PageHeader {
            Layout.fillWidth: true
            title: "Analytics"
            description: "Your work with Ava, over time."
            sidebarVisible: pane.sidebarVisible
            onSidebarRequested: pane.sidebarRequested()
            NativeButton { objectName: "closeAnalytics"; text: "Back to chat"; onClicked: pane.closeRequested() }
        }
        GridLayout {
            Layout.fillWidth: true
            columns: pane.narrow ? 2 : 4
            columnSpacing: 10
            rowSpacing: 10
            NativeCombo {
                objectName: "analyticsMachine"
                Layout.fillWidth: true
                Layout.minimumWidth: 100
                model: pane.analytics.machines; textRole: "name"; valueRole: "id"
                currentIndex: model.findIndex(row => row.id === pane.analytics.filters.machine)
                onActivated: pane.analytics.filter("machine", currentValue)
            }
            NativeCombo {
                objectName: "analyticsProject"
                Layout.fillWidth: true
                Layout.minimumWidth: 100
                model: pane.analytics.projects; textRole: "name"; valueRole: "id"
                currentIndex: model.findIndex(row => row.id === pane.analytics.filters.project)
                onActivated: pane.analytics.filter("project", currentValue)
            }
            RowLayout {
                Layout.fillWidth: true
                NativeButton { objectName: "analytics7d"; Layout.fillWidth: true; text: "7 days"; quiet: pane.analytics.filters.days !== 7; onClicked: pane.analytics.filter("days", "7") }
                NativeButton { objectName: "analytics30d"; Layout.fillWidth: true; text: "30 days"; quiet: pane.analytics.filters.days !== 30; onClicked: pane.analytics.filter("days", "30") }
            }
            Label { text: pane.analytics.filters.timezone; color: palette.placeholderText; font.pixelSize: 11; Layout.fillWidth: true; elide: Text.ElideMiddle }
        }
        Label {
            objectName: "analyticsNotice"
            Layout.fillWidth: true
            visible: !!pane.report.notice || pane.analytics.loading
            text: pane.analytics.loading ? "Loading your statistics…" : pane.report.notice
            wrapMode: Text.WordWrap
            font.pixelSize: 12
            color: palette.placeholderText
        }
        ScrollView {
            id: scroll
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            contentWidth: availableWidth
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            ColumnLayout {
                width: scroll.availableWidth
                spacing: 20
                GridLayout {
                    Layout.fillWidth: true
                    columns: pane.narrow ? 2 : 4
                    uniformCellWidths: true
                    columnSpacing: 12
                    rowSpacing: 12
                    Card { objectName: "analyticsTokens"; Layout.fillWidth: true; title: "Tokens"; value: pane.report.reports ? pane.compact(pane.report.totals.tokens) : "—"; detail: pane.number(pane.report.totals.responses) + " model requests" }
                    Card { objectName: "analyticsTime"; Layout.fillWidth: true; title: "Active time"; value: pane.report.reports ? pane.duration(pane.report.totals.active_ms) : "—"; detail: "Overlapping sessions counted once" }
                    Card { Layout.fillWidth: true; title: "Tool activity"; value: pane.report.reports ? pane.compact(pane.report.totals.tools) : "—"; detail: pane.number(pane.report.totals.tool_errors) + " returned errors" }
                    Card { Layout.fillWidth: true; title: "Skills loaded"; value: pane.report.reports ? pane.number(pane.report.totals.skills) : "—"; detail: "Observed reads of skill instructions" }
                }
                Pane {
                    Layout.fillWidth: true
                    padding: 18
                    background: Surface {}
                    contentItem: ColumnLayout {
                        spacing: 16
                        Label { text: "Daily activity"; font.pixelSize: 15; font.weight: Font.DemiBold }
                        RowLayout {
                            Layout.fillWidth: true
                            Repeater {
                                model: [{id:"tokens", name:"Tokens"}, {id:"active_ms", name:"Time"}, {id:"tools", name:"Tools"}, {id:"skills", name:"Skills"}]
                                NativeButton {
                                    required property var modelData
                                    objectName: "analyticsMetric_" + modelData.id
                                    Layout.fillWidth: true
                                    text: modelData.name
                                    quiet: pane.metric !== modelData.id
                                    onClicked: pane.metric = modelData.id
                                }
                            }
                        }
                        Item {
                            id: chart
                            objectName: "analyticsChart"
                            Layout.fillWidth: true
                            implicitHeight: 210
                            readonly property real maximum: Math.max(1, ...pane.report.days.map(day => day[pane.metric]))
                            readonly property bool hasActivity: pane.report.days.some(day => day[pane.metric] > 0)
                            readonly property real plotHeight: height - 30
                            Repeater {
                                model: 3
                                Item {
                                    required property int index
                                    width: chart.width
                                    y: chart.plotHeight * index / 2
                                    Rectangle { x: 48; width: parent.width - 48; height: 1; color: pane.palette.mid; opacity: 0.6 }
                                    Label { visible: chart.hasActivity; width: 42; horizontalAlignment: Text.AlignRight; text: pane.metric === "active_ms" ? pane.duration(chart.maximum*(1-parent.index/2)) : pane.compact(chart.maximum*(1-parent.index/2)); font.pixelSize: 10; color: palette.placeholderText; y: -7 }
                                }
                            }
                            Row {
                                id: bars
                                x: 54
                                width: chart.width - x
                                height: chart.plotHeight
                                spacing: pane.report.days.length === 7 ? 12 : 3
                                Repeater {
                                    model: pane.report.days
                                    Item {
                                        id: bar
                                        required property var modelData
                                        required property int index
                                        width: Math.max(1, (bars.width - bars.spacing * (pane.report.days.length-1)) / Math.max(1, pane.report.days.length))
                                        height: bars.height
                                        readonly property var segments: pane.metric === "tokens" ? [modelData.output, modelData.cache_write, modelData.cached_read, modelData.input] : [modelData[pane.metric]]
                                        Accessible.role: Accessible.StaticText
                                        Accessible.name: pane.dayLabel(modelData.date) + ": " + pane.valueText(modelData[pane.metric])
                                        Rectangle { anchors.fill: parent; color: pane.palette.highlight; opacity: hover.hovered ? 0.07 : 0; radius: 3 }
                                        Column {
                                            anchors.bottom: parent.bottom
                                            width: parent.width
                                            Repeater {
                                                model: bar.segments
                                                Rectangle {
                                                    required property real modelData
                                                    required property int index
                                                    width: bar.width
                                                    height: chart.plotHeight * modelData / chart.maximum
                                                    color: pane.metric === "tokens" ? pane.colors[3-index] : pane.colors[0]
                                                }
                                            }
                                        }
                                        HoverHandler { id: hover }
                                        NativeToolTip {
                                            visible: hover.hovered
                                            text: pane.dayLabel(bar.modelData.date) + " · " + pane.valueText(bar.modelData[pane.metric])
                                                + (pane.metric === "tokens" ? " tokens\nInput " + pane.number(bar.modelData.input) + " · Cache read " + pane.number(bar.modelData.cached_read) + "\nCache write " + pane.number(bar.modelData.cache_write) + " · Output " + pane.number(bar.modelData.output) : "")
                                        }
                                        Label {
                                            anchors.top: parent.bottom
                                            anchors.topMargin: 9
                                            anchors.horizontalCenter: parent.horizontalCenter
                                            visible: pane.report.days.length === 7 || (bar.index % 7 === 0 && bar.index < pane.report.days.length-4) || bar.index === pane.report.days.length-1
                                            text: pane.dayLabel(bar.modelData.date)
                                            font.pixelSize: 10
                                            color: palette.placeholderText
                                        }
                                    }
                                }
                            }
                            Label { anchors.centerIn: parent; visible: pane.report.reports > 0 && !chart.hasActivity; text: "No activity in this period"; color: palette.placeholderText; font.pixelSize: 12 }
                        }
                        Flow {
                            Layout.fillWidth: true
                            spacing: 16
                            visible: pane.metric === "tokens"
                            Repeater {
                                model: ["Input", "Cache read", "Cache write", "Output"]
                                Row {
                                    required property string modelData
                                    required property int index
                                    spacing: 6
                                    Rectangle { width: 8; height: 8; radius: 2; color: pane.colors[parent.index]; y: 3 }
                                    Label { text: parent.modelData; font.pixelSize: 11; color: palette.placeholderText }
                                }
                            }
                        }
                    }
                }
                Label {
                    Layout.fillWidth: true
                    text: (pane.report.totals.missing_usage ? pane.number(pane.report.totals.missing_usage) + " responses have missing or partial token usage. " : "")
                        + "Tokens use reported usage; reasoning is included in output. Active time measures agent execution, not time at your desk."
                    color: palette.placeholderText; font.pixelSize: 11; wrapMode: Text.WordWrap
                }
                Repeater {
                    model: [{name:"Tools", key:"tools"}, {name:"Skills", key:"skills"}]
                    ColumnLayout {
                        id: breakdown
                        required property var modelData
                        Layout.fillWidth: true
                        spacing: 10
                        readonly property var rows: pane.report[modelData.key]
                        readonly property int limit: modelData.key === "tools" ? pane.toolsLimit : pane.skillsLimit
                        Label { text: breakdown.modelData.name; font.pixelSize: 15; font.weight: Font.DemiBold }
                        Label { visible: !breakdown.rows.length; text: breakdown.modelData.key === "skills" ? "No recorded skill loads. Older sessions may not include this event." : "No tool activity in this period."; font.pixelSize: 12; color: palette.placeholderText; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                        Repeater {
                            model: breakdown.rows.slice(0, breakdown.limit)
                            RowLayout {
                                required property var modelData
                                Layout.fillWidth: true
                                Label { text: parent.modelData.name; font.pixelSize: 12; Layout.fillWidth: true; elide: Text.ElideRight }
                                Label { text: parent.modelData.errors ? pane.number(parent.modelData.errors) + " errors" : ""; color: palette.placeholderText; font.pixelSize: 11 }
                                Label { text: pane.number(parent.modelData.count); font.pixelSize: 12 }
                            }
                        }
                        NativeButton { visible: breakdown.rows.length > breakdown.limit; text: "Show more"; quiet: true; onClicked: breakdown.modelData.key === "tools" ? pane.toolsLimit += 8 : pane.skillsLimit += 8 }
                    }
                }
                Item { implicitHeight: 10 }
            }
        }
    }
}
