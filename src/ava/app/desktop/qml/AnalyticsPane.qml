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
    readonly property var colors: Theme.dark ? ["#95b7ee", "#9b8acb", "#d3ad71", "#70b9a6"] : ["#476fba", "#8a6eb0", "#bd9456", "#478c77"]
    readonly property var tokenKinds: [
        {key: "input", name: "Input", detail: "Uncached input"},
        {key: "cached_read", name: "Cache read", detail: "Reused input"},
        {key: "cache_write", name: "Cache write", detail: "Input saved to cache"},
        {key: "output", name: "Output", detail: "Includes reasoning"}
    ]
    readonly property var metrics: [
        {id: "tokens", name: "Tokens"}, {id: "active_ms", name: "Active time"},
        {id: "tools", name: "Tools"}, {id: "skills", name: "Skills"}
    ]
    readonly property string period: report.days.length ? dayLabel(report.days[0].date) + " – " + dayLabel(report.days[report.days.length - 1].date) : "Last " + analytics.filters.days + " days"
    property string metric: "tokens"
    property int toolsLimit: 8
    property int skillsLimit: 8
    signal closeRequested
    signal sidebarRequested
    padding: narrow ? Theme.spaceLg : Theme.spaceXl
    background: Rectangle { color: Theme.workspace }
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
    function tickText(value) {
        if (metric !== "active_ms") return compact(value);
        const seconds = Math.round(value / 1000);
        if (seconds < 60) return seconds + "s";
        if (seconds < 3600) return Math.floor(seconds / 60) + "m" + (seconds % 60 ? seconds % 60 + "s" : "");
        return duration(value);
    }
    function dayLabel(day) { return Qt.formatDate(new Date(day + "T12:00:00"), "MMM d"); }
    function percentage(value, total) {
        if (!total || !value) return "0%";
        return value / total < 0.01 ? "<1%" : Math.round(value / total * 100) + "%";
    }
    function scaleMaximum(value) {
        if (value <= 0) return 1;
        // Four equal intervals with human-readable ticks, always starting at zero.
        const step = value / 4;
        const magnitude = Math.pow(10, Math.floor(Math.log(step) / Math.LN10));
        const fraction = step / magnitude;
        return (fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10) * magnitude * 4;
    }

    component Card: Pane {
        id: card
        required property string title
        required property string value
        required property string detail
        padding: Theme.spaceLg
        background: Surface {}
        contentItem: ColumnLayout {
            spacing: Theme.spaceSm
            Label { text: card.title; color: Theme.secondaryText; font.pixelSize: Theme.caption }
            Label { text: card.value; color: Theme.text; font.pixelSize: 28; font.weight: Font.DemiBold }
            Label {
                Layout.fillWidth: true
                text: pane.report.reports ? card.detail : pane.analytics.loading ? "Loading statistics…" : "No available statistics"
                color: Theme.secondaryText
                font.pixelSize: Theme.caption
                wrapMode: Text.WordWrap
            }
        }
    }

    component Breakdown: Pane {
        id: breakdown
        required property string kind
        required property string title
        readonly property var rows: pane.report[kind]
        readonly property int limit: kind === "tools" ? pane.toolsLimit : pane.skillsLimit
        readonly property real total: pane.report.totals[kind]
        Layout.fillWidth: true
        Layout.fillHeight: true
        padding: Theme.spaceLg
        background: Surface {}
        contentItem: ColumnLayout {
            spacing: Theme.spaceLg
            RowLayout {
                Layout.fillWidth: true
                Label { Layout.fillWidth: true; text: breakdown.title; font.pixelSize: Theme.sectionTitle; font.weight: Font.DemiBold }
                Label { text: pane.number(breakdown.total) + (breakdown.kind === "tools" ? " calls" : " loads"); font.pixelSize: Theme.caption; color: Theme.secondaryText }
            }
            Label {
                Layout.fillWidth: true
                text: breakdown.kind === "tools" ? "Completed calls, ranked by frequency" : "Instruction loads, ranked by frequency"
                color: Theme.secondaryText
                font.pixelSize: Theme.caption
                wrapMode: Text.WordWrap
            }
            Label {
                Layout.fillWidth: true
                visible: !breakdown.rows.length
                text: breakdown.kind === "skills" ? "No skill loads recorded in this period. Older sessions may not include skill events." : "No completed tool calls in this period."
                color: Theme.secondaryText
                font.pixelSize: Theme.body
                wrapMode: Text.WordWrap
                topPadding: Theme.spaceLg
                bottomPadding: Theme.spaceLg
            }
            Repeater {
                model: breakdown.rows.slice(0, breakdown.limit)
                ColumnLayout {
                    id: entry
                    required property var modelData
                    required property int index
                    objectName: "analyticsRank_" + breakdown.kind + "_" + index
                    Layout.fillWidth: true
                    spacing: 6
                    readonly property real share: breakdown.total > 0 ? modelData.count / breakdown.total : 0
                    Accessible.role: Accessible.StaticText
                    Accessible.name: modelData.name + ": " + pane.number(modelData.count) + ", " + pane.percentage(modelData.count, breakdown.total) + (modelData.errors ? ", " + pane.number(modelData.errors) + " errors" : "")
                    RowLayout {
                        Layout.fillWidth: true
                        Label {
                            Layout.fillWidth: true
                            text: entry.modelData.name
                            textFormat: Text.PlainText
                            font.pixelSize: Theme.body
                            elide: Text.ElideRight
                            HoverHandler { id: nameHover }
                            NativeToolTip { visible: nameHover.hovered; text: entry.modelData.name }
                        }
                        Label { text: pane.number(entry.modelData.count); font.pixelSize: Theme.body; font.weight: Font.DemiBold }
                        Label { text: pane.percentage(entry.modelData.count, breakdown.total); Layout.preferredWidth: 36; horizontalAlignment: Text.AlignRight; font.pixelSize: Theme.caption; color: Theme.secondaryText }
                    }
                    Rectangle {
                        objectName: "analyticsRankTrack_" + breakdown.kind + "_" + entry.index
                        Layout.fillWidth: true
                        implicitHeight: 6
                        radius: 3
                        color: Theme.inset
                        Rectangle { width: parent.width * entry.share; height: parent.height; radius: 3; color: breakdown.kind === "tools" ? pane.colors[0] : pane.colors[3] }
                    }
                    Label {
                        visible: entry.modelData.errors > 0
                        text: pane.number(entry.modelData.errors) + " returned errors"
                        font.pixelSize: Theme.captionSmall
                        color: Theme.danger
                    }
                }
            }
            NativeButton {
                visible: breakdown.rows.length > 8
                text: breakdown.rows.length > breakdown.limit ? "Show more (" + (breakdown.rows.length - breakdown.limit) + ")" : "Show less"
                quiet: true
                onClicked: {
                    const next = breakdown.rows.length > breakdown.limit ? breakdown.limit + 8 : 8;
                    if (breakdown.kind === "tools") pane.toolsLimit = next;
                    else pane.skillsLimit = next;
                }
            }
            Item { Layout.fillHeight: true }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: Theme.spaceLg
        PageHeader {
            Layout.fillWidth: true
            title: "Session information"
            description: "Understand your usage, activity, and the tools behind your work."
            sidebarVisible: pane.sidebarVisible
            onSidebarRequested: pane.sidebarRequested()
            NativeButton { objectName: "closeAnalytics"; text: "Back to chat"; onClicked: pane.closeRequested() }
        }
        GridLayout {
            Layout.fillWidth: true
            columns: pane.narrow ? 2 : 3
            columnSpacing: Theme.spaceMd
            rowSpacing: Theme.spaceSm
            NativeCombo {
                objectName: "analyticsMachine"
                Accessible.name: "Filter by machine"
                Layout.fillWidth: true
                Layout.minimumWidth: 100
                model: pane.analytics.machines; textRole: "name"; valueRole: "id"
                currentIndex: model.findIndex(row => row.id === pane.analytics.filters.machine)
                onActivated: pane.analytics.filter("machine", currentValue)
            }
            NativeCombo {
                objectName: "analyticsProject"
                Accessible.name: "Filter by project"
                Layout.fillWidth: true
                Layout.minimumWidth: 100
                model: pane.analytics.projects; textRole: "name"; valueRole: "id"
                currentIndex: model.findIndex(row => row.id === pane.analytics.filters.project)
                onActivated: pane.analytics.filter("project", currentValue)
            }
            RowLayout {
                spacing: Theme.spaceXs
                NativeButton { objectName: "analytics7d"; text: "7 days"; quiet: true; checkable: true; checked: pane.analytics.filters.days === 7; onClicked: pane.analytics.filter("days", "7") }
                NativeButton { objectName: "analytics30d"; text: "30 days"; quiet: true; checkable: true; checked: pane.analytics.filters.days === 30; onClicked: pane.analytics.filter("days", "30") }
            }
        }
        ScrollView {
            id: scroll
            objectName: "analyticsScroll"
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            contentWidth: availableWidth
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            ColumnLayout {
                width: scroll.availableWidth
                spacing: Theme.spaceLg
                Pane {
                    Layout.fillWidth: true
                    visible: !!pane.report.notice || pane.analytics.loading
                    padding: Theme.spaceMd
                    background: Surface { color: pane.analytics.loading ? Theme.inset : Theme.warningSurface; border.width: 0 }
                    contentItem: Label {
                        objectName: "analyticsNotice"
                        text: pane.analytics.loading ? "Loading your statistics…" : pane.report.notice
                        wrapMode: Text.WordWrap
                        textFormat: Text.PlainText
                        font.pixelSize: Theme.caption
                        color: pane.analytics.loading ? Theme.secondaryText : Theme.warning
                    }
                }
                RowLayout {
                    Layout.fillWidth: true
                    Label { text: "Overview"; font.pixelSize: Theme.sectionTitle; font.weight: Font.DemiBold }
                    Label { Layout.fillWidth: true; text: pane.period + " · " + pane.analytics.filters.timezone; horizontalAlignment: Text.AlignRight; elide: Text.ElideMiddle; font.pixelSize: Theme.caption; color: Theme.secondaryText }
                }
                GridLayout {
                    Layout.fillWidth: true
                    columns: pane.narrow ? 2 : 4
                    uniformCellWidths: true
                    columnSpacing: Theme.spaceMd
                    rowSpacing: Theme.spaceMd
                    Card { objectName: "analyticsTokens"; Layout.fillWidth: true; Layout.fillHeight: true; title: "Total tokens"; value: pane.report.reports ? pane.compact(pane.report.totals.tokens) : "—"; detail: pane.number(pane.report.totals.responses) + " model requests" }
                    Card { objectName: "analyticsTime"; Layout.fillWidth: true; Layout.fillHeight: true; title: "Active time"; value: pane.report.reports ? pane.duration(pane.report.totals.active_ms) : "—"; detail: "Overlapping sessions counted once" }
                    Card { Layout.fillWidth: true; Layout.fillHeight: true; title: "Tool calls"; value: pane.report.reports ? pane.compact(pane.report.totals.tools) : "—"; detail: pane.number(pane.report.totals.tool_errors) + " returned errors" }
                    Card { Layout.fillWidth: true; Layout.fillHeight: true; title: "Skills loaded"; value: pane.report.reports ? pane.number(pane.report.totals.skills) : "—"; detail: "Recorded instruction loads" }
                }
                GridLayout {
                    Layout.fillWidth: true
                    columns: pane.narrow ? 1 : 2
                    columnSpacing: Theme.spaceLg
                    rowSpacing: Theme.spaceLg
                    Pane {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Layout.preferredWidth: 620
                        padding: Theme.spaceLg
                        background: Surface {}
                        contentItem: ColumnLayout {
                            spacing: Theme.spaceLg
                            RowLayout {
                                Layout.fillWidth: true
                                Label { Layout.fillWidth: true; text: "Daily activity"; font.pixelSize: Theme.sectionTitle; font.weight: Font.DemiBold }
                                Label { text: pane.analytics.filters.days + " days"; color: Theme.secondaryText; font.pixelSize: Theme.caption }
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: Theme.spaceXs
                                Repeater {
                                    model: pane.metrics
                                    NativeButton {
                                        required property var modelData
                                        objectName: "analyticsMetric_" + modelData.id
                                        Layout.fillWidth: true
                                        text: modelData.name
                                        quiet: true
                                        checkable: true
                                        checked: pane.metric === modelData.id
                                        onClicked: pane.metric = modelData.id
                                    }
                                }
                            }
                            Label {
                                Layout.fillWidth: true
                                text: pane.report.reports ? pane.valueText(pane.report.totals[pane.metric]) + (pane.metric === "tokens" ? " tokens" : pane.metric === "active_ms" ? " of agent execution" : pane.metric === "tools" ? " completed calls" : " skill loads") + " in this period" : "Statistics are not available yet"
                                color: Theme.secondaryText
                                font.pixelSize: Theme.caption
                                wrapMode: Text.WordWrap
                            }
                            Item {
                                id: chart
                                objectName: "analyticsChart"
                                Layout.fillWidth: true
                                implicitHeight: 206
                                readonly property real maximum: Math.max(pane.metric === "active_ms" ? 4000 : 4, pane.scaleMaximum(Math.max(0, ...pane.report.days.map(day => day[pane.metric]))))
                                readonly property bool hasActivity: pane.report.days.some(day => day[pane.metric] > 0)
                                readonly property real plotHeight: height - 30
                                Repeater {
                                    model: 5
                                    Item {
                                        required property int index
                                        width: chart.width
                                        y: chart.plotHeight * index / 4
                                        Rectangle { x: 48; width: Math.max(0, parent.width - 48); height: 1; color: Theme.border; opacity: 0.6 }
                                        Label { visible: chart.hasActivity; width: 42; horizontalAlignment: Text.AlignRight; text: pane.tickText(chart.maximum * (1 - parent.index / 4)); font.pixelSize: Theme.captionSmall; color: Theme.secondaryText; y: -7 }
                                    }
                                }
                                Row {
                                    id: bars
                                    x: 54
                                    width: Math.max(0, chart.width - x)
                                    height: chart.plotHeight
                                    spacing: pane.report.days.length === 7 ? 8 : 3
                                    Repeater {
                                        id: barRepeater
                                        model: pane.report.days
                                        Item {
                                            id: bar
                                            required property var modelData
                                            required property int index
                                            objectName: "analyticsDay_" + index
                                            width: Math.max(1, (bars.width - bars.spacing * (pane.report.days.length - 1)) / Math.max(1, pane.report.days.length))
                                            height: bars.height
                                            readonly property var segments: pane.metric === "tokens" ? [modelData.output, modelData.cache_write, modelData.cached_read, modelData.input] : [modelData[pane.metric]]
                                            activeFocusOnTab: true
                                            Accessible.role: Accessible.StaticText
                                            Accessible.name: pane.dayLabel(modelData.date) + ": " + pane.valueText(modelData[pane.metric]) + " " + pane.metrics.find(row => row.id === pane.metric).name
                                            Keys.onLeftPressed: { if (index > 0) barRepeater.itemAt(index - 1).forceActiveFocus(); }
                                            Keys.onRightPressed: { if (index + 1 < barRepeater.count) barRepeater.itemAt(index + 1).forceActiveFocus(); }
                                            Rectangle { anchors.fill: parent; color: Theme.selection; visible: hover.hovered || bar.activeFocus; radius: 3 }
                                            Column {
                                                id: barStack
                                                objectName: "analyticsBar_" + bar.index
                                                anchors.bottom: parent.bottom
                                                anchors.horizontalCenter: parent.horizontalCenter
                                                width: Math.min(36, bar.width)
                                                Repeater {
                                                    model: bar.segments
                                                    Rectangle {
                                                        required property real modelData
                                                        required property int index
                                                        width: barStack.width
                                                        height: chart.plotHeight * modelData / chart.maximum
                                                        color: pane.metric === "tokens" ? pane.colors[3 - index] : pane.colors[0]
                                                    }
                                                }
                                            }
                                            HoverHandler { id: hover }
                                            NativeToolTip {
                                                visible: hover.hovered || bar.activeFocus
                                                text: bar.Accessible.name + (pane.metric === "tokens" ? "\nInput " + pane.number(bar.modelData.input) + " · Cache read " + pane.number(bar.modelData.cached_read) + "\nCache write " + pane.number(bar.modelData.cache_write) + " · Output " + pane.number(bar.modelData.output) : "")
                                            }
                                            Label {
                                                anchors.top: parent.bottom
                                                anchors.topMargin: Theme.spaceSm
                                                anchors.horizontalCenter: parent.horizontalCenter
                                                visible: pane.report.days.length === 7 ? (bars.width >= 340 || bar.index % 2 === 0) : (bar.index % 7 === 0 && bar.index < pane.report.days.length - 4) || bar.index === pane.report.days.length - 1
                                                text: pane.dayLabel(bar.modelData.date)
                                                font.pixelSize: Theme.captionSmall
                                                color: Theme.secondaryText
                                            }
                                        }
                                    }
                                }
                                Label {
                                    anchors.centerIn: parent
                                    visible: !chart.hasActivity
                                    text: pane.analytics.loading ? "Loading activity…" : pane.report.reports ? "No activity in this period" : "No available activity data"
                                    color: Theme.secondaryText
                                    font.pixelSize: Theme.body
                                }
                            }
                            Flow {
                                Layout.fillWidth: true
                                spacing: Theme.spaceMd
                                Repeater {
                                    model: pane.metric === "tokens" ? pane.tokenKinds : []
                                    Row {
                                        required property var modelData
                                        required property int index
                                        spacing: 6
                                        Rectangle { width: 8; height: 8; radius: 2; color: pane.colors[parent.index]; y: 3 }
                                        Label { text: parent.modelData.name; font.pixelSize: Theme.captionSmall; color: Theme.secondaryText }
                                    }
                                }
                            }
                        }
                    }
                    Pane {
                        objectName: "analyticsTokenMix"
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Layout.preferredWidth: 280
                        padding: Theme.spaceLg
                        background: Surface {}
                        contentItem: ColumnLayout {
                            spacing: Theme.spaceLg
                            Label { text: "Token breakdown"; font.pixelSize: Theme.sectionTitle; font.weight: Font.DemiBold }
                            Label { text: "How reported tokens are used"; color: Theme.secondaryText; font.pixelSize: Theme.caption }
                            Row {
                                id: tokenStack
                                Layout.fillWidth: true
                                visible: pane.report.totals.tokens > 0
                                Repeater {
                                    model: pane.tokenKinds
                                    Rectangle {
                                        required property var modelData
                                        required property int index
                                        objectName: "analyticsTokenSegment_" + modelData.key
                                        width: pane.report.totals.tokens ? tokenStack.width * pane.report.totals[modelData.key] / pane.report.totals.tokens : 0
                                        height: 12
                                        color: pane.colors[index]
                                    }
                                }
                            }
                            Repeater {
                                model: pane.tokenKinds
                                ColumnLayout {
                                    id: token
                                    required property var modelData
                                    required property int index
                                    Layout.fillWidth: true
                                    spacing: Theme.spaceXs
                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: Theme.spaceSm
                                        Rectangle { implicitWidth: 8; implicitHeight: 8; radius: 2; color: pane.colors[token.index] }
                                        Label { Layout.fillWidth: true; text: token.modelData.name; font.pixelSize: Theme.body }
                                        Label { text: pane.report.reports ? pane.compact(pane.report.totals[token.modelData.key]) : "—"; font.pixelSize: Theme.body; font.weight: Font.DemiBold }
                                        Label { text: pane.report.reports ? pane.percentage(pane.report.totals[token.modelData.key], pane.report.totals.tokens) : "—"; Layout.preferredWidth: 36; horizontalAlignment: Text.AlignRight; font.pixelSize: Theme.caption; color: Theme.secondaryText }
                                    }
                                    Label { text: token.modelData.detail; leftPadding: 16; color: Theme.secondaryText; font.pixelSize: Theme.captionSmall }
                                    HoverHandler { id: tokenHover }
                                    NativeToolTip { visible: tokenHover.hovered && pane.report.reports > 0; text: pane.number(pane.report.totals[token.modelData.key]) + " " + token.modelData.name.toLowerCase() + " tokens" }
                                }
                            }
                            Item { Layout.fillHeight: true }
                        }
                    }
                }
                Pane {
                    Layout.fillWidth: true
                    visible: pane.report.totals.missing_usage > 0
                    padding: Theme.spaceMd
                    background: Surface { color: Theme.warningSurface; border.width: 0 }
                    contentItem: Label {
                        text: pane.number(pane.report.totals.missing_usage) + " model responses have missing or partial usage. Token totals may be incomplete."
                        color: Theme.warning
                        font.pixelSize: Theme.caption
                        wrapMode: Text.WordWrap
                    }
                }
                GridLayout {
                    Layout.fillWidth: true
                    visible: pane.report.reports > 0
                    columns: pane.narrow ? 1 : 2
                    uniformCellWidths: true
                    columnSpacing: Theme.spaceLg
                    rowSpacing: Theme.spaceLg
                    Breakdown { kind: "tools"; title: "Tool activity" }
                    Breakdown { kind: "skills"; title: "Skill activity" }
                }
                Label {
                    Layout.fillWidth: true
                    text: "About these numbers · Tokens reflect provider-reported usage; reasoning is included in output. Active time measures agent execution, not time at your desk."
                    color: Theme.secondaryText
                    font.pixelSize: Theme.captionSmall
                    wrapMode: Text.WordWrap
                }
                Item { implicitHeight: Theme.spaceSm }
            }
        }
    }
}
