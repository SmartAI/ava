pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls

NativeMenu {
    id: menu
    required property var editor
    property var pasteHandler: null
    readonly property bool editable: !!editor && !editor.readOnly
    readonly property bool hasSelection: !!editor && !!editor.selectedText
    readonly property bool canCopy: hasSelection && (editor.echoMode === undefined || editor.echoMode === TextInput.Normal)
    property bool canPaste: !!editor && editor.canPaste
    palette: editor ? editor.palette : undefined
    onClosed: { if (editor && editor.enabled) editor.forceActiveFocus(); }
    NativeMenuItem {
        objectName: "textUndo"
        text: "Undo"
        visible: menu.editable
        enabled: menu.editable && menu.editor.canUndo
        onTriggered: menu.editor.undo()
    }
    NativeMenuItem {
        objectName: "textRedo"
        text: "Redo"
        visible: menu.editable
        enabled: menu.editable && menu.editor.canRedo
        onTriggered: menu.editor.redo()
    }
    MenuSeparator { visible: menu.editable }
    NativeMenuItem {
        objectName: "textCut"
        text: "Cut"
        visible: menu.editable
        enabled: menu.editable && menu.canCopy
        onTriggered: menu.editor.cut()
    }
    NativeMenuItem {
        objectName: "textCopy"
        text: "Copy"
        enabled: menu.canCopy
        onTriggered: menu.editor.copy()
    }
    NativeMenuItem {
        objectName: "textPaste"
        text: "Paste"
        visible: menu.editable
        enabled: menu.editable && menu.canPaste
        onTriggered: {
            if (!menu.pasteHandler || !menu.pasteHandler())
                menu.editor.paste();
        }
    }
    MenuSeparator { visible: menu.editable }
    NativeMenuItem {
        objectName: "textSelectAll"
        text: "Select all"
        enabled: !!menu.editor && menu.editor.length > 0
        onTriggered: menu.editor.selectAll()
    }
}
