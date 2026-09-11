# Desktop design system

Ava is a calm, native-feeling desktop workspace. Content takes priority over chrome.
Use system fonts, clear hierarchy, quiet neutral surfaces, and blue selection/focus.
Do not emulate glass with translucent content or add shadows to every panel.

## Tokens

`src/ava/app/desktop/qml/Theme.qml` owns appearance tokens. `Main.qml` binds its
saved light/dark preference to the theme and maps semantic colors to the Qt palette,
so built-in controls, text selection, and embedded views inherit the same appearance.

- Surfaces: workspace, sidebar, surface, inset, hover, selection.
- Text: primary, secondary, disabled; accent and on-accent are a separate pair.
- Status: success, warning, danger; always accompany color with text.
- Spacing: 4, 8, 12, 16, 24, 32 logical pixels.
- Type: 13 controls, 12 supporting text, 11 dense metadata, 15 reading,
  17 section headings, 21 detail headings, 24 page headings, 28 metrics.
  Conversation reading size remains user-adjustable. Code retains the system fixed font.
- Controls: 32 high, 28 for compact actions; 16-pixel icons.
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

## Evaluation

Baseline on the pre-migration working tree: 7 focused desktop acceptance tests passed
(workbench, theme/search, board, and composer alignment). Captures and test logs live
in the agent scratch directory, not in the repository. Design-system checks capture
each requested appearance and verify capture does not change it.

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
