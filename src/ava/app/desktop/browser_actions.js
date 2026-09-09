/* Runs only in Qt's isolated ApplicationWorld; no host bridge is exposed to the page. */
function avaBrowserAction(command) {
    try {
        const state = globalThis.__avaBrowserState || (globalThis.__avaBrowserState = { refs: new Map(), epoch: "" });
        const compact = text => String(text || "").replace(/\s+/g, " ").trim().slice(0, 200);
        const labelOf = node => {
            const label = node.labels?.[0];
            if (!label) return "";
            const pending = [label], parts = [];
            let visited = 0;
            while (pending.length && visited++ < 256) {
                const child = pending.pop();
                if (child === node) continue;
                if (child.nodeType === Node.TEXT_NODE) parts.push(child.nodeValue);
                else for (let index = child.childNodes.length - 1; index >= 0; --index) pending.push(child.childNodes[index]);
            }
            return parts.join(" ");
        };
        const nameOf = node => compact(node.getAttribute("aria-label") ||
            (node.getAttribute("aria-labelledby") || "").split(/\s+/).map(id => node.ownerDocument.getElementById(id)?.textContent || "").join(" ") ||
            labelOf(node) || node.getAttribute("placeholder") || node.textContent || node.getAttribute("title") || node.getAttribute("name"));
        const signature = node => [node.tagName, node.getAttribute("role"), node.getAttribute("type"), nameOf(node), node.getAttribute("href")].join("\n");
        const visible = node => {
            const style = node.ownerDocument.defaultView.getComputedStyle(node);
            return node.getClientRects().length > 0 && style.visibility !== "hidden" && style.display !== "none" && style.opacity !== "0";
        };
        function snapshot() {
            state.refs.clear(); state.epoch = command.id;
            const elements = [], text = [], frames = [];
            const pending = [document.body || document.documentElement];
            let visited = 0, chars = 0;
            const started = performance.now();
            while (pending.length && visited++ < 12000 && performance.now() - started < 60) {
                const node = pending.pop();
                if (!node) continue;
                if (node.nodeType === Node.TEXT_NODE) {
                    if (chars < 12000) {
                        const value = compact(node.nodeValue).slice(0, 12000 - chars);
                        if (value) { text.push(value); chars += value.length + 1; }
                    }
                    continue;
                }
                if (node.nodeType !== Node.ELEMENT_NODE || /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(node.tagName) || !visible(node)) continue;
                if (elements.length < 150 && node.matches('a[href],button,input:not([type="hidden"]),textarea,select,[contenteditable="true"],[role="button"],[role="link"],[role="checkbox"],[role="tab"],[tabindex]:not([tabindex="-1"])')) {
                    const ref = state.epoch + ":" + elements.length;
                    state.refs.set(ref, { node: node, signature: signature(node) });
                    const value = node.type === "password" ? "[redacted]" : compact(node.value || "");
                    elements.push({ ref: ref, role: node.getAttribute("role") || node.tagName.toLowerCase(), name: nameOf(node),
                                    value: value, disabled: !!node.disabled || node.getAttribute("aria-disabled") === "true" });
                }
                if (node.tagName === "IFRAME" || node.tagName === "FRAME") {
                    let body = null;
                    try { body = node.contentDocument?.body; } catch (_) {}
                    if (body) pending.push(body);
                    else frames.push({ name: node.name || "", url: node.src, note: "Cross-origin frame; its DOM is not in this snapshot." });
                }
                const children = node.shadowRoot ? node.shadowRoot.childNodes : node.childNodes;
                for (let index = Math.min(children.length, 12000 - visited) - 1; index >= 0; --index) pending.push(children[index]);
            }
            return { title: document.title, url: location.href, text: text.join("\n"), elements: elements, frames: frames,
                     truncated: pending.length > 0 || elements.length === 150 || chars >= 12000 };
        }
        if (command.action === "snapshot" || command.action === "wait") return { page: snapshot() };
        if (command.action === "scroll") {
            window.scrollBy({ left: command.x || 0, top: command.y || 0, behavior: "instant" });
            return { page: snapshot() };
        }
        if (command.action === "press") return { input: { action: "press", key: command.key } };
        const stored = state.refs.get(command.ref);
        if (!stored || !stored.node.isConnected || stored.signature !== signature(stored.node)) throw new Error("This element reference is stale. Take another snapshot.");
        const node = stored.node;
        if (!visible(node) || node.disabled || node.getAttribute("aria-disabled") === "true") throw new Error("This element is hidden or disabled.");
        if (command.action === "fill" && !(node.isContentEditable || /^(INPUT|TEXTAREA)$/.test(node.tagName)) || command.action === "fill" && node.readOnly) throw new Error("This element cannot be edited.");
        if (command.action === "fill" && node.tagName === "INPUT" && !/^(text|search|tel|url|email|password|number)$/.test(node.type)) throw new Error("Fill requires a text input. Use click for other controls; file uploads require the user.");
        node.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
        const doc = node.ownerDocument;
        const box = node.getBoundingClientRect();
        let x = (Math.max(0, box.left) + Math.min(doc.defaultView.innerWidth, box.right)) / 2;
        let y = (Math.max(0, box.top) + Math.min(doc.defaultView.innerHeight, box.bottom)) / 2;
        let hit = doc.elementFromPoint(x, y);
        while (hit?.shadowRoot) {
            const child = hit.shadowRoot.elementFromPoint(x, y);
            if (!child || child === hit) break;
            hit = child;
        }
        if (!hit || !(hit === node || node.contains(hit))) throw new Error("Another element covers this target. Take another snapshot after it is dismissed.");
        let frame = doc.defaultView;
        while (frame !== window) {
            const element = frame.frameElement;
            if (!element) throw new Error("This frame cannot be targeted from the current page.");
            const rect = element.getBoundingClientRect();
            x += rect.left + element.clientLeft; y += rect.top + element.clientTop;
            const parentHit = element.ownerDocument.elementFromPoint(x, y);
            if (parentHit !== element) throw new Error("The frame is covered or outside the viewport.");
            frame = frame.parent;
        }
        if (command.action === "select") {
            if (node.tagName !== "SELECT") throw new Error("Use select with a select element.");
            const option = Array.from(node.options).find(option => option.value === command.value || option.text === command.value);
            if (!option || option.disabled) throw new Error("That option is unavailable.");
            node.value = option.value;
            node.dispatchEvent(new Event("input", { bubbles: true }));
            node.dispatchEvent(new Event("change", { bubbles: true }));
            return { page: snapshot() };
        }
        return { input: { action: command.action, x: x, y: y, text: command.text || "" } };
    } catch (error) {
        return { error: String(error.message || error).slice(0, 1000) };
    }
}
