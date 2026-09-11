pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Pane {
    id: pane
    objectName: "reviewPane"
    required property var backend
    required property string codeFont
    required property string rootPath
    property string projectId: ""
    property var review: null
    readonly property var gitState: review ? review.state : ({})
    readonly property var selected: review ? review.files.find(row => row.id === gitState.selected) || ({}) : ({})
    readonly property int stagedCount: review ? review.files.filter(row => row.scope === "staged").length : 0
    property string lastCommit: ""
    padding: 0
    background: null
    Component.onCompleted: review = backend.createGitReview(rootPath, projectId)
    Component.onDestruction: {
        if (review)
            review.close();
    }
    Timer {
        interval: 3000
        repeat: true
        running: pane.visible && !!pane.review && pane.gitState.ready && !pane.gitState.busy
        onTriggered: pane.review.refresh()
    }
    Connections {
        target: pane.review
        function onCommitted(summary) {
            commitDialog.close();
            commitMessage.text = "";
            pane.lastCommit = summary;
        }
    }
    ColumnLayout {
        anchors.fill: parent
        spacing: 10
        RowLayout {
            Layout.fillWidth: true
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 3
                Label {
                    Layout.fillWidth: true
                    text: (pane.backend.machines.length > 1 || pane.rootPath !== pane.backend.projectPath) ? pane.backend.workspaceLabel(pane.projectId, pane.rootPath) : pane.rootPath.split("/").pop()
                    font.pixelSize: 13
                    font.weight: Font.DemiBold
                    elide: Text.ElideMiddle
                    textFormat: Text.PlainText
                }
                Label {
                    Layout.fillWidth: true
                    text: pane.gitState.branch || "Working tree"
                    color: palette.placeholderText
                    font.pixelSize: 11
                    elide: Text.ElideMiddle
                    textFormat: Text.PlainText
                }
            }
            NativeButton {
                objectName: "refreshChangesButton"
                icon.source: "icons/reload.svg"
                quiet: true
                tip: "Refresh changes"
                enabled: !!pane.review && !pane.gitState.busy
                onClicked: pane.review.refresh()
            }
            NativeButton {
                objectName: "commitChangesButton"
                text: "Commit…"
                enabled: pane.stagedCount > 0 && !pane.gitState.busy
                onClicked: commitDialog.open()
            }
        }
        Label {
            Layout.fillWidth: true
            visible: !!pane.gitState.error || !!pane.gitState.refreshError
            text: pane.gitState.error || pane.gitState.refreshError || ""
            wrapMode: Text.Wrap
            font.pixelSize: 12
            color: Theme.danger
            textFormat: Text.PlainText
        }
        SplitView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            orientation: Qt.Vertical
            handle: Rectangle {
                implicitHeight: 5
                color: SplitHandle.hovered || SplitHandle.pressed ? pane.palette.mid : "transparent"
            }
            ListView {
                id: files
                objectName: "changedFileList"
                SplitView.preferredHeight: Math.min(200, Math.max(90, contentHeight))
                SplitView.minimumHeight: 70
                SplitView.maximumHeight: Math.max(100, pane.height * 0.6)
                model: pane.review ? pane.review.files : []
                clip: true
                reuseItems: true
                ScrollBar.vertical: ScrollBar {}
                delegate: ItemDelegate {
                    id: change
                    required property var modelData
                    objectName: "change_" + modelData.id
                    width: files.width
                    height: 40
                    highlighted: pane.gitState.selected === modelData.id
                    background: Surface {
                        radius: Theme.controlRadius
                        border.width: 0
                        focused: change.visualFocus
                        color: change.highlighted ? Theme.selection : change.hovered ? Theme.hover : "transparent"
                    }
                    contentItem: RowLayout {
                        spacing: 8
                        Label {
                            text: change.modelData.status === "?" ? "A" : change.modelData.status
                            font.family: pane.codeFont
                            font.pixelSize: 11
                            color: change.modelData.status === "D" ? Theme.danger : Theme.success
                        }
                        Label {
                            Layout.fillWidth: true
                            text: change.modelData.previous ? change.modelData.previous + " → " + change.modelData.path : change.modelData.path
                            elide: Text.ElideMiddle
                            font.pixelSize: 12
                            textFormat: Text.PlainText
                        }
                        Label {
                            text: change.modelData.scope === "staged" ? "Staged" : ""
                            font.pixelSize: 10
                            color: palette.placeholderText
                        }
                    }
                    onClicked: pane.review.select(modelData.id)
                }
                Label {
                    anchors.centerIn: parent
                    text: pane.gitState.loading ? "Loading changes…" : pane.gitState.ready ? "No uncommitted changes" : "Open a Git project to review changes"
                    visible: files.count === 0
                    font.pixelSize: 12
                    color: palette.placeholderText
                }
            }
            ColumnLayout {
                SplitView.fillHeight: true
                SplitView.minimumHeight: 160
                spacing: 8
                RowLayout {
                    Layout.fillWidth: true
                    Label {
                        Layout.fillWidth: true
                        text: pane.selected.path || "Changes"
                        elide: Text.ElideMiddle
                        font.pixelSize: 12
                        font.weight: Font.Medium
                        textFormat: Text.PlainText
                    }
                    NativeButton {
                        objectName: "copyDiffButton"
                        icon.source: "icons/copy.svg"
                        quiet: true
                        tip: "Copy diff"
                        enabled: !!pane.gitState.diff
                        onClicked: pane.backend.copyText(pane.gitState.diff)
                    }
                    NativeButton {
                        objectName: pane.selected.scope === "staged" ? "unstageFileButton" : "stageFileButton"
                        text: pane.selected.scope === "staged" ? "Unstage" : "Stage file"
                        visible: !!pane.selected.id
                        enabled: !pane.gitState.busy
                        onClicked: pane.review.toggleStage()
                    }
                }
                Label {
                    Layout.fillWidth: true
                    visible: !!pane.gitState.notice
                    text: pane.gitState.notice || ""
                    wrapMode: Text.Wrap
                    font.pixelSize: 11
                    color: palette.placeholderText
                    textFormat: Text.PlainText
                }
                CodePreview {
                    objectName: "diffPreview"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    backend: pane.backend
                    codeFont: pane.codeFont
                    text: pane.gitState.diff || ""
                    filename: "changes.diff"
                    visible: !!pane.gitState.selected
                }
                Item {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    visible: !pane.gitState.selected
                    Label {
                        anchors.centerIn: parent
                        width: parent.width - 30
                        horizontalAlignment: Text.AlignHCenter
                        text: pane.lastCommit || "Select a file to inspect its changes."
                        wrapMode: Text.Wrap
                        font.pixelSize: 12
                        color: palette.placeholderText
                        textFormat: Text.PlainText
                    }
                }
            }
        }
        Label {
            visible: pane.gitState.busy || false
            text: "Working…"
            font.pixelSize: 11
            color: palette.placeholderText
        }
    }
    NativeDialog {
        id: commitDialog
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(480, parent ? parent.width - 48 : 480)
        title: "Commit staged changes"
        modal: true
        padding: 20
        closePolicy: pane.gitState.busy ? Popup.NoAutoClose : Popup.CloseOnEscape | Popup.CloseOnPressOutside
        ColumnLayout {
            width: parent.width
            spacing: 12
            Label {
                text: pane.stagedCount + " staged file" + (pane.stagedCount === 1 ? "" : "s")
                font.pixelSize: 12
                color: palette.placeholderText
            }
            TextArea {
                id: commitMessage
                ContextMenu.menu: TextMenu { editor: commitMessage }
                objectName: "commitMessageField"
                Layout.fillWidth: true
                Layout.preferredHeight: 110
                placeholderText: "Describe this change"
                wrapMode: TextEdit.Wrap
                enabled: !pane.gitState.busy
                selectByMouse: true
                padding: 10
                font.pixelSize: 13
                background: Surface { radius: Theme.controlRadius; focused: commitMessage.activeFocus }
            }
            Label {
                Layout.fillWidth: true
                text: pane.gitState.error || ""
                visible: !!text
                wrapMode: Text.Wrap
                color: Theme.danger
                font.pixelSize: 12
                textFormat: Text.PlainText
            }
            RowLayout {
                Layout.alignment: Qt.AlignRight
                NativeButton {
                    text: "Cancel"
                    enabled: !pane.gitState.busy
                    onClicked: commitDialog.close()
                }
                NativeButton {
                    objectName: "confirmCommitButton"
                    text: pane.gitState.busy ? "Committing…" : "Commit"
                    primary: true
                    enabled: pane.stagedCount > 0 && !!commitMessage.text.trim() && !pane.gitState.busy
                    onClicked: pane.review.commit(commitMessage.text)
                }
            }
        }
        onOpened: commitMessage.forceActiveFocus()
    }
}
