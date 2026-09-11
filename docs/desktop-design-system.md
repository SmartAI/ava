# Desktop design system

[Feature tour](features.md#desktop-workbench) · [Usage](usage.md#qt-quick-desktop-app) ·
[Architecture](architecture.md)

Ava is a calm, native-feeling desktop workspace. Content takes priority over chrome.
Use system fonts, clear hierarchy, quiet neutral surfaces, and blue selection/focus.
Do not emulate glass with translucent content or add shadows to every panel.

## Tokens

`src/ava/app/desktop/qml/Theme.qml` owns appearance tokens. `Main.qml` binds its
saved light/dark preference to the theme and maps semantic colors to the Qt palette.
The desktop uses Qt Quick Controls' Basic style with shared QML components, not OS-native
widgets. Native text rendering and platform font fallbacks are configured in
`application.py`; preferences are saved in `$AVA_HOME/desktop.ini`.
The terminal receives an explicit theme; external web pages retain their own styling.

- Surfaces: workspace, sidebar, surface, inset, hover, selection.
- Text: `text`, `secondaryText`, `disabledText`; `accent` / `accentText` for
  selection and focus, separate from `primary` / `primaryText` for action buttons.
- Status: success, warning, danger; warning/danger surfaces; accompany color with text.
- Edges and overlays: border, shadow, scrim.
- Spacing tokens: `spaceXs` 4, `spaceSm` 8, `spaceMd` 12, `spaceLg` 16,
  `spaceXl` 24 logical pixels. Larger spacing is currently local to its layout.
- Type tokens: `body` 13, `caption` 12, `captionSmall` 11, `sectionTitle` 17,
  `pageTitle` 24. The application default is 14; conversation reading defaults to
  15 and is user-adjustable. Detail headings (21) and metrics (28) are pane-local
  conventions, not Theme properties. Code uses the platform fixed-font fallback chain.
- Controls: `controlHeight` 32 and `iconSize` 16. Compact 28-high actions are local overrides.
- Corners: 8 controls/rows, 12 cards/popovers, 16 dialogs/composer.
- Motion: 120ms state transitions, disabled by the Reduce motion preference.

Use semantic names instead of introducing another local color or size. Specialized
chart geometry, code layout, and media aspect ratios are not general UI tokens.

## Shared components

- `Surface`: fill, edge, corners, optional elevation, and visible keyboard focus.
  Elevation 0 is flat; 1 is a subtle raised surface; 2 is a floating overlay.
  Qt's rectangular shadow avoids capturing/blur-rendering entire content trees.
  Borders also provide separation on software renderers without shader effects.
- `NativeButton`, `NativeField`, `NativeCombo`: consistent input and action states.
  Primary buttons use a contrast-safe blue/white pair in both appearances.
- `NativeDialog`: shared background, padding, title, and dimming. Open/close is
  immediate so initialization/focus and rapid keyboard input never race a fade.
  Specialized headers and scroll layouts may override layout, not surface styling.
- `NativeMenu`, `NativeMenuItem`, `NativeToolTip`: shared floating presentation.
- `NavigationItem`: icon, title, optional accessory, hover, and persistent selection.
- `PageHeader`: shared heading/description/actions; actions move below the title
  at narrow widths instead of squeezing or clipping it.
- `EmptyState`: shared title and explanation; feature actions remain in the pane.

Keep feature state in its existing pane. Avoid building a generic page framework.
List rows stay flat; menus, dialogs, and the composer carry elevation. Status labels
use the existing Label rather than a new badge wrapper unless a badge is needed.

## Conversation and navigation patterns

- New conversations keep the heading and composer together. Project selection and the
  checkable Worktree action share a quiet rounded footer, separated from the message
  box by `spaceSm` (8 px). Detailed
  paths and setup explanations belong in tooltips, not a second form above the composer.
- The session tree initially exposes five unpinned chats per project. Use a flat,
  caption-sized **Show more** / **Show fewer** row at the same indentation as chats,
  with the existing hover and keyboard-focus treatment. Keep pins separate. Expansion
  is view state for the current app instance; opening an older chat reveals it.
- Keep the existing Qt `ListView` and its delegate recycling. Limit `sessionRows` in
  the model rather than hiding delegates: [Qt documents that invisible delegates
  still occupy space](https://doc.qt.io/qt-6/qml-qtquick-listview.html#hiding-delegates).
  An explicit expansion row does not require migrating to `TreeView` or using automatic
  `fetchMore`, which serves incremental data loading rather than a user-controlled limit.
- Machine status belongs in Machines. Restart is quiet and secondary; Open is primary.
  Restart and Open/Reconnect use shared column widths across rows; optional removal and
  update actions must not shift those columns.
- Scrollable Skills/MCP/Automation forms have inset content inside the clipped viewport, leaving
  room for full input borders and the 3 px focus-ring outset on both sides. Dialog
  padding alone cannot prevent a ScrollView's inner viewport from clipping focus.
- Read-only text menus show adjacent Copy and Select all rows. Hidden editing actions
  and separators must also have zero layout height. Editable fields retain their full
  menu, selection, clipboard behavior, and focus restoration.

## Streaming transcripts

Keep Qt's native Markdown renderer, but apply paragraph styling synchronously when
text changes, before the next ListView layout/paint. Qt's
[`setMarkdown()`](https://doc.qt.io/qt-6/qtextdocument.html#setMarkdown) replaces the
whole document; deferring our styling with
[`Qt.callLater()`](https://doc.qt.io/qt-6/qml-qtqml-qt.html#callLater-method) can expose
unstyled paragraph heights for a frame on every SSE update. Keep accumulating the
Markdown source: [`TextEdit.append()`](https://doc.qt.io/qt-6/qml-qtquick-textedit.html#append-method)
adds a paragraph, and parsing each arbitrary SSE fragment separately breaks syntax
split across chunks (for example, bold delimiters or code fences).

Qt documents that [variable-height ListView delegates](https://doc.qt.io/qt-6/qml-qtquick-listview.html#variable-delegate-size-and-section-labels)
make its content-size estimate unstable. Follow the measured last row plus the
running-status height, not the estimated footer position. Geometry callbacks must
not re-enter layout with `forceLayout()`. Transcript delegates remain virtualized,
but are not pooled: unloading their lazy content while pooled exposes zero/stale
heights when reused. Scrolling up opts out of following; Jump to latest resumes it.

The `switch_to_running_session` acceptance scenario replays durable mixed-height
history through the real backend, switches via the sidebar, holds the provider open,
then streams at 5/25 ms intervals. It checks per-frame tail/paragraph stability
within one logical pixel, delegate identity, Markdown formatting, and reading
position. The `jump_to_latest_renders_message_pixels` scenario drags back to the
beginning, receives more output while the latest message is offscreen, then clicks
the down arrow. It checks actual glyph pixels at the destination: `atYEnd` alone
can briefly report success even when the message is outside the viewport. Run both
scenarios on the native and offscreen renderers described below.

## Evaluation

Use the current implementation as the baseline, with the deterministic acceptance
flows in `tests/test_desktop.py` as the repeatable benchmark. Measure behavioral
correctness, QML warnings/binding loops, contrast, clipping, focus visibility, and
interaction latency. The [feature tour](features.md) provides visual references,
not pixel-diff goldens or proof that every platform has passed visual review.
Design-system checks capture each requested appearance and verify capture does not
change it. Keep captures and performance logs outside source control.

A focused check of shared components and layout:

```sh
QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software uv run --extra desktop pytest -q tests/test_desktop.py -k 'design_system or settings_theme or message_composer_alignment'
uv run --extra desktop pyside6-qmllint --max-warnings 0 src/ava/app/desktop/qml/*.qml
```

Set `AVA_DESKTOP_SCREENSHOT` to an absolute PNG path in a scratch directory to
retain captures. Run the full desktop suite for feature coverage; the focused
selection above does not test every pane.

On macOS, select the test platform explicitly: an inherited XQuartz `DISPLAY` can
otherwise prevent the acceptance fixture from choosing its offscreen fallback.
Use `QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software` for headless checks and
`QT_QPA_PLATFORM=cocoa QT_QUICK_BACKEND=` for native GPU checks. Menus use item
popups on headless/Wayland platforms and separate popup windows on macOS.

Validate every desktop section using deterministic local-backend acceptance flows:
conversation, board, automations, skills, MCP, analytics, files/code/PDF, browser,
terminal, changes, settings, and dialogs. Compare light/dark at 1280×820 and 800×600.
Check normal text contrast >= 4.5:1, visible focus and selection, no overlapping or
clipped controls, keyboard navigation, theme changes while popups are open, and
unchanged backend actions. Run QML lint with zero warnings and desktop tests with
runtime binding-loop checks. Use the existing file-explorer performance benchmark
for interaction-to-first-frame latency and bounded delegates; avoid adding effects
inside scrolling lists. Native GPU visual review remains distinct from offscreen
software-renderer acceptance tests.
