pragma ComponentBehavior: Bound
import QtQuick
import QtWebEngine

QtObject {
    id: agent
    required property var control
    required property var browser
    required property int tabId
    property var pending: null
    property double startedAt: 0
    property string phase: ""
    function valid() { return pending && control && control.accepts(tabId, pending.id); }
    function finish(result) {
        if (valid()) control.complete(tabId, pending.id, result);
        pending = null;
    }
    function inspect() {
        if (!valid()) return;
        const command = pending;
        const action = phase === "after" ? Object.assign({}, command, {action: "snapshot"}) : command;
        phase = "callback";
        browser.runJavaScript(control.script(action), WebEngineScript.ApplicationWorld, function(result) {
            if (!agent.valid() || agent.pending.id !== command.id) return;
            if (!result) { agent.finish({error: "The page changed while it was being inspected. Take a new snapshot."}); return; }
            if (result.error) { agent.finish({error: result.error}); return; }
            if (result.input) {
                const error = agent.control.input(agent.tabId, command.id, result.input);
                if (error) agent.finish({error: error});
                else agent.phase = "after";
            } else if (command.action === "wait" && command.text && !JSON.stringify(result.page).includes(command.text)) {
                agent.phase = "ready";
            } else {
                agent.finish({text: JSON.stringify(result.page)});
            }
        });
    }
    function start(command) {
        pending = command; startedAt = Date.now(); phase = "ready";
        if (!browser.visible) { finish({error: "Keep the handed-off tab visible while Ava uses it."}); return; }
        if (["navigate", "back", "forward", "reload"].includes(command.action)) {
            if (command.action === "navigate") {
                if (!/^https?:\/\//i.test(command.url)) { finish({error: "Navigation requires an http or https URL."}); return; }
                browser.url = command.url;
            } else if (command.action === "back") browser.goBack();
            else if (command.action === "forward") browser.goForward();
            else browser.reload();
            phase = "after";
        }
    }
    property Connections commands: Connections {
        target: agent.control
        function onExecute(identity, command) { if (identity === agent.tabId) agent.start(command); }
        function onChanged() { if (agent.pending && !agent.valid()) agent.pending = null; }
    }
    property Connections loading: Connections {
        target: agent.browser
        function onLoadingChanged(info) {
            if (agent.valid() && info.status === WebEngineView.LoadFailedStatus)
                agent.finish({error: "This page could not be loaded. " + info.errorString});
        }
    }
    property Timer poll: Timer {
        interval: 50
        repeat: true
        running: !!agent.pending
        onTriggered: {
            if (!agent.valid()) { agent.pending = null; return; }
            if (Date.now() - agent.startedAt > 22000) { agent.finish({error: "This page did not become ready in time."}); return; }
            if (agent.browser.loading || (agent.phase !== "ready" && agent.phase !== "after")) return;
            if (agent.pending.action === "screenshot") {
                agent.phase = "capture";
                const command = agent.pending;
                agent.browser.runJavaScript("requestAnimationFrame(() => requestAnimationFrame(() => globalThis.__avaPaintReady = true)); globalThis.__avaPaintReady = false;", WebEngineScript.ApplicationWorld, function() {
                    if (agent.valid() && agent.pending.id === command.id) agent.phase = "paint";
                });
            } else agent.inspect();
        }
    }
    property Timer paint: Timer {
        interval: 32
        repeat: true
        running: agent.phase === "paint" && !!agent.pending
        onTriggered: {
            if (!agent.valid()) return;
            const command = agent.pending;
            agent.browser.runJavaScript("globalThis.__avaPaintReady === true", WebEngineScript.ApplicationWorld, function(ready) {
                if (ready && agent.valid() && agent.pending.id === command.id) {
                    agent.phase = "capture";
                    agent.control.capture(agent.tabId, command.id);
                    agent.pending = null;
                }
            });
        }
    }
}
