pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Pane {
    id: panel
    required property var backend
    readonly property var goal: backend.goal
    property double receivedAt: Date.now()
    property double now: Date.now()
    readonly property int seconds: Math.max(0, Math.floor(((goal.elapsed_ms || 0) + (goal.timing_running && backend.connected ? now - receivedAt : 0)) / 1000))
    readonly property string stateLabel: ({active: "Active", paused: "Paused", blocked: "Blocked", budget_limited: "Limit reached", complete: "Complete"})[goal.status] || ""
    visible: !!goal.id && goal.status !== "cleared"
    objectName: "goalStatus"
    padding: 12
    onGoalChanged: { receivedAt = Date.now(); now = receivedAt; }
    Timer {
        interval: 1000
        repeat: true
        running: panel.visible && !!panel.goal.timing_running && panel.backend.connected
        onTriggered: panel.now = Date.now()
    }
    background: Surface { color: Theme.inset }
    contentItem: ColumnLayout {
        spacing: 6
        RowLayout {
            Layout.fillWidth: true
            Label { text: "Goal"; font.pixelSize: 12; font.weight: Font.DemiBold }
            Label {
                objectName: "goalState"
                text: panel.stateLabel
                color: panel.goal.status === "blocked" || panel.goal.status === "budget_limited" ? Theme.warning : palette.highlight
                font.pixelSize: 11
            }
            Item { Layout.fillWidth: true }
            Label {
                objectName: "goalElapsed"
                text: Math.floor(panel.seconds / 3600) > 0
                    ? Math.floor(panel.seconds / 3600) + "h " + Math.floor(panel.seconds / 60) % 60 + "m"
                    : Math.floor(panel.seconds / 60) + "m " + panel.seconds % 60 + "s"
                font.pixelSize: 11
                color: palette.placeholderText
                ToolTip.visible: elapsedHover.hovered
                ToolTip.text: "Worker execution time, excluding pauses and completion audits"
                HoverHandler { id: elapsedHover }
            }
        }
        Label {
            objectName: "goalObjective"
            Layout.fillWidth: true
            text: panel.goal.objective || ""
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            maximumLineCount: 2
            elide: Text.ElideRight
            font.pixelSize: 13
            ToolTip.visible: objectiveHover.hovered
            ToolTip.text: text
            HoverHandler { id: objectiveHover }
        }
        Label {
            objectName: "goalLimits"
            Layout.fillWidth: true
            text: "Turns " + (panel.goal.turns || 0) + " / " + (panel.goal.max_turns || 0)
                + "  ·  Tokens " + (panel.goal.tokens_used || 0).toLocaleString(Qt.locale(), 'f', 0)
                + (panel.goal.token_budget ? " / " + panel.goal.token_budget.toLocaleString(Qt.locale(), 'f', 0) : " · no token limit")
                + (panel.goal.usage_complete === false ? " (partial usage)" : "")
            wrapMode: Text.Wrap
            font.pixelSize: 11
            color: palette.placeholderText
        }
        Label {
            Layout.fillWidth: true
            visible: !!panel.goal.check
            text: "Verify: " + (panel.goal.check || "")
            textFormat: Text.PlainText
            elide: Text.ElideRight
            font.pixelSize: 11
            color: palette.placeholderText
            ToolTip.visible: checkHover.hovered
            ToolTip.text: text
            HoverHandler { id: checkHover }
        }
        Label {
            objectName: "goalReason"
            Layout.fillWidth: true
            visible: !!text
            text: panel.goal.reason || ""
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            maximumLineCount: 2
            elide: Text.ElideRight
            font.pixelSize: 11
            color: palette.placeholderText
            ToolTip.visible: reasonHover.hovered
            ToolTip.text: text
            HoverHandler { id: reasonHover }
        }
    }
}
