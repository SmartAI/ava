import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';
import './terminal.css';

const lightTheme = {
  background: '#ffffff', foreground: '#202123', cursor: '#202123', selectionBackground: '#cbd5e1',
  black: '#202123', red: '#b42318', green: '#2e6f40', yellow: '#8a5a00',
  blue: '#1b59b6', magenta: '#8a3795', cyan: '#176a79', white: '#e7e7eb',
  brightBlack: '#66666b', brightRed: '#c23435', brightGreen: '#367c2b', brightYellow: '#946d00',
  brightBlue: '#3269c3', brightMagenta: '#9f4cb6', brightCyan: '#007585', brightWhite: '#ffffff',
};
const darkTheme = {
  background: '#202020', foreground: '#ececec', cursor: '#ececec', selectionBackground: '#505057',
  black: '#333333', red: '#f07178', green: '#addb67', yellow: '#ecc48d',
  blue: '#82aaff', magenta: '#c792ea', cyan: '#89ddff', white: '#d0d0d0',
  brightBlack: '#a2a2a8', brightRed: '#ff8b92', brightGreen: '#c3e88d', brightYellow: '#ffdcad',
  brightBlue: '#a1c0ff', brightMagenta: '#d8afff', brightCyan: '#b2ebf2', brightWhite: '#ffffff',
};
const term = new Terminal({
  fontFamily: 'Menlo, monospace', fontSize: 13, lineHeight: 1.2,
  scrollback: 5000, cursorBlink: true, cursorStyle: 'bar',
  allowProposedApi: false, screenReaderMode: true,
  theme: lightTheme,
});
const fit = new FitAddon();
term.loadAddon(fit);
term.open(document.getElementById('terminal'));
let bridge;
let fitFrame;
let pastePending = false;
const pendingInput = [];
function requestPaste() {
  if (!bridge) return;
  if (pastePending) pendingInput.push(null);
  else { pastePending = true; bridge.paste(); }
}
function copySelection() {
  const selected = term.getSelection();
  if (selected) bridge?.copy(selected);
}
function fitTerminal() {
  cancelAnimationFrame(fitFrame);
  fitFrame = requestAnimationFrame(() => {
    if (document.body.clientWidth > 0 && document.body.clientHeight > 0) fit.fit();
  });
}
new ResizeObserver(fitTerminal).observe(document.getElementById('terminal'));
window.avaTerminal = {
  focus: () => term.focus(),
  copy: copySelection,
  hasSelection: () => term.hasSelection(),
  selectAll: () => term.selectAll(),
  paste: requestPaste,
  clear: () => term.clear(),
  setAppearance: (dark, font, colors, reducedMotion = false) => {
    term.options.fontFamily = `${font}, "PingFang SC", "Noto Sans CJK SC", monospace`;
    // Keep ANSI colors, but inherit workspace colors from the QML design system.
    term.options.theme = { ...(dark ? darkTheme : lightTheme), ...colors };
    term.options.cursorBlink = !reducedMotion;
    document.body.style.background = term.options.theme.background;
    fitTerminal();
  },
  // Used by native UI tests to inspect the rendered buffer, including ANSI processing.
  text: () => Array.from({ length: term.buffer.active.length }, (_, i) => term.buffer.active.getLine(i)?.translateToString(true)).join('\n'),
  size: () => [term.cols, term.rows],
};
new QWebChannel(qt.webChannelTransport, channel => {
  bridge = channel.objects.terminal;
  bridge.outputReceived.connect((encoded, size) => {
    const bytes = Uint8Array.from(atob(encoded), c => c.charCodeAt(0));
    term.write(bytes, () => bridge.acknowledge(size));
  });
  bridge.resetRequested.connect(() => {
    pendingInput.length = 0;
    pastePending = false;
    term.write('', () => term.reset());
  });
  bridge.pasteRequested.connect(text => {
    pastePending = false;
    term.paste(text);
    while (pendingInput.length && !pastePending) {
      const data = pendingInput.shift();
      if (data === null) requestPaste();
      else bridge.write(data);
    }
  });
  term.onData(data => { if (pastePending) pendingInput.push(data); else bridge.write(data); });
  term.onResize(({ cols, rows }) => bridge.resize(cols, rows));
  term.attachCustomKeyEventHandler(event => {
    const mac = /Mac/.test(navigator.platform);
    if ((mac ? event.metaKey : event.ctrlKey) && event.key.toLowerCase() === 'j') {
      if (event.type === 'keydown') { event.preventDefault(); bridge.toggle(); }
      return false;
    }
    const shortcut = mac ? event.metaKey : event.ctrlKey && event.shiftKey;
    if (shortcut && event.key.toLowerCase() === 'a') {
      if (event.type === 'keydown') { event.preventDefault(); term.selectAll(); }
      return false;
    }
    if (shortcut && ['c', 'v'].includes(event.key.toLowerCase())) {
      if (event.type === 'keydown') {
        event.preventDefault();
        if (event.key.toLowerCase() === 'c') copySelection();
        else requestPaste();
      }
      return false;
    }
    return true;
  });
  fit.fit();
  bridge.start(term.cols, term.rows);
  term.focus();
});
