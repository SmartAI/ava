# Usage and development

[Quick start](../README.md#quick-start) · [Architecture](architecture.md) ·
[Evaluation guide](../eval/README.md)

## Use Ava from the CLI or Python

### CLI

```sh
uv run ava -p "explain src/ava/agent/turn.py"
uv run ava -c -p "which failure paths should have regression tests?"
uv run ava -p --session review.jsonl.zst "review this project"
uv run ava session inspect review.jsonl.zst
uv run ava session dump review.jsonl.zst
```

`-c` continues the latest session. Explicit session paths make a run easy to locate
and inspect later.

### Qt Quick desktop app

The desktop app uses QML and connects to a persistent local Ava backend, starting it when needed.
Install the optional Qt dependencies and launch it from a terminal:

```sh
uv sync --extra desktop
uv run --extra desktop ava-desktop --project .
```

`--provider`, `--model`, and `--effort` override the model for new desktop conversations;
existing conversations retain their saved selections and other clients keep their defaults.
For example, an existing Codex CLI login can be used with `--provider codex`.
Configure provider connections and credentials in **Settings → Providers**, through the settings file, or in the Web UI.
The desktop supports built-in providers and custom OpenAI-compatible or Anthropic-compatible endpoints.
An existing Codex CLI login is reused for Codex.

Choose a project folder, create or reopen a conversation, and send a message.
**New chat** opens a session directly, without a setup dialog. Before the first message,
use the project selector below the message box, or the adjacent **+** to choose another
folder, without losing your draft or attachments. Turn on **Worktree** in the same footer
to create a random branch and checkout with `git worktree add` on first send; leave it off
to work directly in the project folder.
Worktrees are grouped under `$HOME/.ava/worktrees/<project-folder-name>/<random-name>`
(or `$AVA_HOME/worktrees/...` when configured), on the selected machine. Toggling the
option alone creates nothing. Existing worktrees and uncommitted project changes are
left intact; after the conversation starts, its working directory is fixed.
Enter sends; Shift+Enter inserts a newline. While Ava runs, Enter steers the current
turn and Alt+Enter queues a follow-up, matching the Web UI. Enter resumes a paused turn.
Pause waits for a step boundary; Stop cancels the run; Resume continues a paused conversation.
Drafts and staged attachments are kept separately for each conversation while the app remains open.

- Sessions from all projects appear together, grouped under collapsible project headings.
  Each project initially shows its latest five unpinned sessions. **Show more** expands
  the remaining sessions; **Show fewer** returns to five. Search still includes all
  sessions, and opening an older result reveals it in the tree. Pinned sessions stay
  in their own section and do not count toward the five-session limit.
  Click a session to switch projects automatically; the `+` beside each project starts a
  conversation there. Group expansion is remembered. Search chats with Command/Ctrl+K;
  search matches titles, project names and paths across projects. Use a chat's `…` menu
  to pin, rename or archive it. Archived chats can be searched and restored from Settings.
  Pinned chats appear once in a top-level **Pinned** section with their project name,
  even when their project is collapsed. Unpinning returns the chat to its project and
  expands that group. Pins survive restarts; pinned empty chats are kept when switching.
- Use a project's `…` menu or right-click its heading and choose **Remove from Ava** to
  hide the project and all its chats, including pinned and archived chats. Files and
  session history stay on disk, and running tasks continue. Use **Undo** or add the same
  folder again to restore it. Removal survives desktop/backend restarts; other connected
  desktops update on their next heartbeat. Removing the selected project opens another
  visible project, or shows **Add project** when none remain.
- **Session board** groups work across machines and projects into **In progress**,
  **Needs review** and **Reviewed**. Completed results appear newest first; each inactive
  column initially shows 20 sessions, with **Review more** to expand it. Filter by machine,
  project, session title or outcomes needing attention. Narrow windows use column tabs.
  A result is considered reviewed once you see it in the conversation page, whether it
  finishes while you are watching or you open it later from the sessions list. You can
  also use **Mark reviewed** without leaving the board. This marker survives backend
  restarts and syncs to other desktops; a later result needs review again. Archived
  sessions and removed projects are excluded.
  Offline machines show their last known status and require reconnection before opening
  or reviewing a result. Older backends may need updating to support review tracking.
- **Analytics** shows 7-day and 30-day trends for tokens, active agent time, tool activity,
  and observed skill loads. Filter by machine or project; hover over a day for exact values.
  Token totals combine fresh input, cache reads, cache writes and output. Reasoning tokens
  are already included in output; missing usage is identified rather than estimated.
  Active time counts overlapping agent sessions once, including across selected machines;
  it does not measure time at your desk. Skill counts record successful reads of discovered
  skill instructions; older sessions may not contain these events.
  Each backend keeps a rebuildable, incremental SQLite cache at `$AVA_HOME/analytics.sqlite3`.
  Unchanged history is not decompressed again when opening or switching views. Indexing and
  incomplete history are indicated. Removed projects are excluded; archived sessions remain
  part of their project's usage. While the desktop stays open, disconnected machines retain
  their last fetched summaries with an offline notice. Older backends need an update.
- **Automations** schedules prompts on a chosen machine and project. Create a task,
  preview its next runs, and open each result as a conversation. See [Automations](#automations)
  for repetition counts, missed runs and background execution.
- Click the model name under the message field to choose a model and its reasoning effort.
  The choices come from the current provider, and changes apply at the next step.
- Use **+**, drag files into the composer, or paste images to add context. Attachments use
  the same limits as the Web UI: UTF-8 text files up to 50 KiB, PNG/JPEG/GIF/WebP images,
  at most 10 attachments per message, and 8 MiB / 10 images over a conversation's lifetime.
  Failed sends retain the draft and attachments.
- Type `/` to filter commands and discovered skills. Arrow keys navigate, Tab inserts,
  Enter runs a command, and Escape dismisses the menu. Selecting a skill adds its
  `$name` reference so you can add a task before sending it. `/help` lists commands.
- Open **Skills**, or run `/skills`, to search, preview, enable, disable or create skills.
  Choose a machine and project; existing worktrees appear as separate locations. Project
  skills live in `.agents/skills`, personal skills in `$AVA_HOME/skills`, and shared skills
  are discovered from `~/.codex/skills`. Project skills override personal and shared skills
  with the same name; disabling an override does not silently enable another copy.
  **Use in chat** inserts a reference into the open conversation in that workspace.
  Changes apply before the next model request, including in existing sessions; instructions
  already read into a conversation remain there. **Remove from Ava** hides a skill from its
  catalog while preserving its source files and other applications. Use the **Removed**
  filter to restore it. Personal and shared availability settings apply across that machine.
  Invalid metadata and overridden skills show their reason in the details panel. Skill
  previews load on selection and show at most 64 KiB; the complete source remains on disk.
- Open **MCP servers → Add server** to connect a local command (stdio) or Streamable HTTP
  endpoint. Choose the execution machine and workspace first. Programs must be installed
  on that machine; quote arguments containing spaces. Optional environment variables and
  HTTP headers are saved there, with existing values hidden when editing.
  For an HTTP server that requires browser login, choose **Authentication → OAuth**.
  Enter the client ID, secret (if required), and authorization-server issuer for an existing
  OAuth client; leave the client fields blank only if the server supports automatic registration.
  Register the displayed **Callback URL** with your OAuth provider. Save, then choose **Sign in**.
  Ava opens your system browser; the short-lived loopback callback runs on the desktop,
  even for SSH execution machines. No browser opens automatically during agent work.
  Tokens and client secrets stay on the execution machine in `capabilities.sqlite3`
  (owner-only `0600` permissions, not encrypted at rest). The SDK refreshes tokens automatically,
  including after a backend restart. **Sign out** removes Ava's saved tokens; it does not revoke
  the grant at the provider. Revoke that separately in the provider's account settings if needed.
  Permission changes require a new sign-in. A server can advertise tools without granting access:
  **Sign-in required** means its account-dependent tools are not yet available to the agent.
  **Gmail:** use `https://gmailmcp.googleapis.com/mcp/v1`, with issuer
  `https://accounts.google.com` and your Google Cloud OAuth client ID and secret.
  Enable both the Gmail API and Gmail MCP API and configure consent/test users as described in
  [Google's setup guide](https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server).
  Google currently lists the integration as Developer Preview. Register Ava's exact callback URL
  (default `http://127.0.0.1:8766/oauth/callback`), not another MCP client's callback.
  Gmail defaults to `https://www.googleapis.com/auth/gmail.readonly`; add only the scopes you need
  for writes. If the callback port is occupied, finish the other login or change the URL both
  in Ava and in your OAuth client's registered redirects. Update older SSH backends to enable OAuth.
  Configurations live in `$AVA_HOME/capabilities.sqlite3` and apply across that machine's
  projects. Each workspace gets its own connection and working directory. Agent tool calls
  use that connection; closing the desktop leaves the backend and active calls running.
  Idle connections close after five minutes and reopen when needed.
  Search tools and open their parameter definitions, or choose **Refresh tools** to reload
  the catalog. Server change notifications update it automatically. **Disable** prevents
  new calls while current calls finish; **Remove** also deletes the saved configuration and
  credentials, preserving the installed program and workspace files. Failed connections
  retain their settings and offer **Retry connection**.
  Tool results support text, structured JSON, text resources, links and PNG/JPEG/GIF/WebP
  images. Select an image attachment in the conversation to view it; previews download
  on demand and the desktop caches the last four until exit. Images reach the model and
  remain in saved history. Each call accepts up to four images, with 7.5 MB total image
  data per tool batch. Model context retains up to ten recent tool images within 8 MiB,
  reduced when necessary to leave room for user attachments; older images remain in history.
  Audio results and separate MCP resource/prompt browsers are not yet supported.
- `/context` shows estimated tokens and model-window usage, with category bars sorted
  by their share of the estimated total. Small nonzero shares remain visible as `<0.1%`;
  unknown model limits are labelled explicitly. The provider's last reported input count
  appears separately, since it describes the previous request. No byte counts are shown.
- The top-right inspector opens files and an embedded browser. Use its `+` menu to add
  independent file explorers or browser tabs; each has its own close button. Switching
  tabs retains the selected file or web page. All browser tabs share the same login profile.
- In a browser tab, choose **Use in this chat** to let the current session inspect and
  operate that page. The agent can navigate, inspect page text, click, fill text, select
  options, press keys, scroll and capture the viewport. Browser activities and screenshots
  appear in the conversation; inspecting screenshots requires a model that accepts images.
  **Take control**, clicking the page, switching away from the tab, or opening an Ava dialog
  ends the handoff. You can still type a follow-up in the composer while the tab is shared.
  Keep the desktop open and the tab visible. A disconnect ends control; reconnecting requires
  another explicit handoff, and interrupted actions are not automatically repeated.
  The session's backend runs the agent, while pages and cookies remain on the desktop.
  Older backends need updating to expose browser control. Page snapshots currently support
  the main document, open shadow DOM and same-origin frames. Cross-origin frame content,
  canvas-coordinate input, file uploads and browser-native permission dialogs are not yet
  automated. A link opening another tab needs a new handoff for that tab.
- Each file tab has a folder tree and a separate content panel, with a draggable separator.
  Qt's asynchronous `QFileSystemModel` populates folders on demand and watches for changes;
  `TreeView` recycles visible rows. Text previews (up to 1 MiB) use a recycling `ListView`
  with line numbers and incremental syntax highlighting. Only visible lines and a small
  buffer have native text items. Extremely long lines wrap into continuations marked `↳`;
  copying preserves the original line breaks. Markdown under 64,000 characters renders formatted;
  larger Markdown uses the virtual source preview. Images preview up to 8 MiB.
  PDFs open in the content panel with page navigation, zoom/fit controls, selectable
  text and internal links. Pages render on demand; each tab keeps its own position
  and zoom. Encrypted documents request a password, and damaged files show a retryable
  error. PDF preview does not enable PDF message attachments.
  Pages rasterize on demand in a separate helper process, isolating Qt's PDF rendering
  lock from chat interaction. Dense vector pages can still take several seconds to
  display; this is not a guarantee of fast rendering for every document. Closing the
  active preview cancels its renderer without waiting on the GUI thread.
  **Add to message** stages the selected file, subject to the composer's attachment limits.
- Browser tabs accept HTTP(S) addresses, including localhost, with back, forward and reload.
  Links in assistant replies open in the inspector. Browser storage persists under
  `$AVA_HOME/browser`, separately from conversations. Media support depends on the Qt build:
  the standard wheel tested here plays WebM but lacks H.264. Failed videos show a notice
  with **Open in browser**. Enabling H.264 requires a Qt WebEngine build with proprietary
  codecs; it cannot be enabled by a runtime setting. Website console warnings are handled
  within the browser instead of flooding the terminal.
- On first opening the browser, Ava imports cookies, bookmarks and history from the
  default browser's selected profile. Chrome, Edge, Brave, Chromium and Firefox are
  supported on macOS and Linux. Chromium cookies may require system keychain access.
  The source profile is read only. **Browser data** shows the result and lets you retry;
  **Bookmarks** and **History** search imported entries. Cookie values are never added
  to conversations or the import report. Passwords, extensions, localStorage, IndexedDB,
  Firefox session-restore cookies and partitioned/container cookies are not migrated;
  some sites therefore require signing in again. Safari is not yet supported.
- Both sidebars can be hidden and resized by dragging their separators. Visibility,
  manually chosen widths, and light/dark theme are remembered. Ctrl+B toggles the left
  sidebar and Ctrl+Alt+B toggles the inspector (Command replaces Ctrl on macOS).
  Settings in the sidebar (Command/Ctrl+,) groups appearance, providers and shortcuts.
  Chat text size has Small, Default and Large options with a Chinese/English preview; appearance changes are saved automatically.
  Archived chats can be restored from General.
  Providers stores connection details and credentials, without changing conversation models or defaults.
  Connection checks read provider model catalogs without sending a chat request.
  Leave the key blank to keep existing credentials, or use **Remove stored key** to remove the saved key.
  Environment credentials take precedence over saved keys; the active credential source and connection status are displayed.
  Click the model name in a conversation to choose a connected provider, one of its models, and supported reasoning effort, then click **Apply**.
  The choice is saved for that session, even before sending a message, and does not affect other conversations.
  Model and effort changes during a run apply at the next step; provider changes require an idle conversation.
  Providers with missing credentials, rejected credentials, or unavailable catalogs are excluded until their connection check succeeds.
- Open the integrated terminal with the header button, Command/Ctrl+J, or `/terminal`.
  Each `+` tab starts an interactive shell in the current project; switching projects or
  hiding the panel preserves existing shells. Drag the divider to resize it. ANSI colors,
  Unicode, selection, clipboard paste and Ctrl+C work in the terminal. On macOS, use
  Command+C/V/A for copy, paste and select all; on Linux use Ctrl+Shift+C/V/A.
  Typing `exit` closes that terminal tab; exiting the last tab also hides the panel and
  returns focus to the chat composer. An SSH transport failure keeps the tab available
  for reconnection rather than discarding its output.
  Closing a tab or quitting Ava stops its shell and tracked child processes. Shells are
  not restarted when the app reopens. The terminal uses local, bundled xterm.js assets;
  rebuild them with `npm run build:terminal` after editing the terminal frontend.

Chat messages and Markdown returned by the read tool render selectable headings, bold and
italic text, lists, tables, links and code blocks. Terminal output keeps its original plain
text. Code blocks have syntax colors and rounded backgrounds; block quotes have a side rule.
Consecutive tool and reasoning activity is grouped into one summary with running and failure
counts. Expand the group to see filenames and commands, then expand an entry for its complete
output. Output is loaded only when needed, and copying keeps the original source. Fold state
survives scrolling while offscreen previews are released. Scrolling up pauses automatic
following; the down-arrow button returns to the latest message. Outputs over 64,000 characters
use the virtual source viewer. The interface uses rounded controls and native text rendering; Chinese
uses PingFang on macOS when available, with platform CJK fallbacks elsewhere.

The desktop [design system](desktop-design-system.md) defines shared light/dark surfaces,
blue selection and focus states, consistent controls, and subtle elevation for the composer
and overlays. **Settings → General → Reduce motion** disables control transitions, the
running-status pulse, and terminal cursor blinking.

The app shares durable history under `$AVA_HOME` and remembers the last selected project and conversation.
Closing the window disconnects the desktop; agent tasks continue and their results remain in
the same conversations. Reopening reconnects to the running backend. Desktop terminal tabs
are separate: their shells still stop when the desktop closes.
Only one desktop instance may use an Ava home at a time.
Only one backend can own an Ava home. Independent `ava --serve` and legacy desktop servers
cannot write it concurrently; use separate `AVA_HOME` directories for independent servers.

```sh
uv run ava-backend status
uv run ava-backend stop          # Refuses while tasks are active
uv run ava-backend stop --force  # Explicitly cancels active tasks and stops the backend
```

The backend listens on loopback and requires a fresh bearer token for each process. Its private
`$AVA_HOME/backend.json` connection file is verified against the running process's identity;
`machine.json` keeps the durable instance-store identity. Crashes preserve the session logs and
the desktop reconnects without automatically resending prompts. `ava-backend serve --project PATH`
runs in the foreground; `serve --no-project` restores registered projects without adding a startup folder.
Without a startup service, the desktop starts a detached backend on demand and inherits its environment.
When a backend is already running, `ava-backend connect` copies the current shell's provider
API-key environment variables into it and reloads idle conversations; busy conversations keep
their existing credentials for that run.
`ava-backend connect` is a private bootstrap command whose stdout
contains connection credentials, so it should not be logged or shared.

Enable **Settings → General → Background work → Start at login** to install a user-owned
launchd service on macOS or systemd service on Linux. The service starts automatically and restarts
the backend after a crash. Settings displays its actual running/startup state. Changing this setting
while tasks are active is refused; stop those tasks from their conversations first. Turning startup
off removes the service and reconnects an open desktop to an ordinary backend. Projects, history and
saved credentials remain intact. The same controls are available from the CLI:

```sh
uv run ava-backend service-install
uv run ava-backend service-status
uv run ava-backend service-uninstall
```

Each Ava home has a separate service. Installation records the current Python environment, so keep
that environment available; after moving it, run `service-install` from the new installation while
tasks are idle. Store provider credentials in **Settings → Providers** before enabling startup. Service
definitions preserve provider/model/effort, an absolute `AVA_CONFIG` path, and basic path/locale
settings; they do not copy shell-only API keys or the whole shell environment. Explicit `stop` leaves
the service installed but stopped until the next desktop connection or login. Crashed tasks retain
their history and are not automatically rerun.

macOS uses a LaunchAgent and requires the user's GUI login session. Linux requires a working systemd
user manager; continuing after logout and starting before login additionally requires **linger**.
Settings reports the actual linger state; Ava does not enable it or request administrator access.
Neither service prevents system sleep. macOS service errors go to `$AVA_HOME/backend.log`; on Linux,
use `journalctl --user -u NAME`, with the name returned by `service-status`.
Use **Settings → Machines** to view connection status, restart or open a machine,
or connect an SSH alias or `user@hostname`. Status indicators live in that tab, not
in a second list below the conversations. Ava uses system OpenSSH,
your SSH configuration and existing verified host keys. The remote user needs Python 3.12+,
`venv`/pip and a working user service manager. First connection uploads the matching Ava package,
checks its SHA-256, installs an independent environment under `~/.local/share/ava/backends/`, and
starts the remote user service. Existing environments are retained; changing the service while
tasks are active is deferred. Ava connects to the compatible existing backend and shows
**Update backend** in Machines. Finish or stop active tasks, then use that button to apply
the prepared update and reconnect. A first connection displays the actual SSH host and SHA-256 fingerprint.
Verify it against the server, then choose **Trust and connect**; OpenSSH saves the key using your
SSH configuration. **Cancel** or Escape leaves it untrusted and does not install a backend. Existing
trusted hosts reconnect directly. A conflicting or revoked key is blocked; Ava does not replace it.
`AVA_SSH_CONFIG` can select an alternative SSH configuration.

On reconnection, Ava identifies its package by content hash. A successfully prepared matching
package is reused without another upload or pip installation. A changed desktop package is
uploaded in full once and installed in another environment; this is not a delta update or a copy
of the desktop's Python environment. Dependencies are installed by remote pip, using its cache
where available. When the existing backend is idle, the service switches to the new environment.
If it has active tasks, the update stays pending while the desktop connects to that backend;
tasks continue in their original sessions. Applying the update or reconnecting after they finish
reuses the prepared package and retries activation. An update attempt while tasks remain active
keeps the existing backend. Authentication failures and incompatible protocols are reported as
connection errors; they never force a restart or cancel tasks.

The matching backend is a separate executable installation; it does not replace `~/.local/bin/ava`
or unrelated systemd jobs. Its default data directory is still the remote user's `~/.ava`, so provider
settings and credentials there remain shared with that user's CLI. Separate session IDs avoid
direct session reuse, but a backend also opens discovered historical sessions for writing. Do not
resume the same discovered session simultaneously through an independent CLI and the backend.
Custom session directories outside the normal project buckets are not automatically imported.

Remote conversations use credentials on the remote machine. The built-in `codex` provider reads
that machine's file-based Codex login (`~/.codex/auth.json`, or `$CODEX_HOME/auth.json` when configured
in the backend's environment). Ava makes model requests itself; it does not run the Codex CLI or
copy the desktop's credentials over SSH. Other providers use the remote backend's saved API keys
and configuration. Usage belongs to the account/workspace or API project behind those credentials,
not to the physical computer. See the [official authentication documentation](https://learn.chatgpt.com/docs/auth).

Use **New chat** to open a session with project and **Worktree** controls below the message box.
The project can be changed before the first message, including by entering a remote folder
inline. Turn on **Worktree** to generate a random branch from the selected project's current HEAD;
Git and an existing commit are required. Ava creates the checkout on first send under
`$AVA_HOME/worktrees/<project-folder-name>/<random-name>` on the selected machine
(`$HOME/.ava/worktrees/...` by default). Uncommitted changes stay in the original folder.
Chats remain grouped under the original project, with their branch shown in the heading. Agent
tools, file previews, Git review and new terminals use the conversation's actual working folder.

Archiving a chat or removing its project from Ava keeps the worktree and its files. If creation
fails after checkout, Ava keeps your draft and reports the retained directory; send again from
the same session to retry without creating another checkout. Keep the worktree directory available to reopen its history.
Worktree cleanup is currently a Git operation; Ava does not automatically delete checkouts.

Each machine has its own collapsible project groups. Use its **+** to add a remote directory
(`~/projects/example` is accepted), then create or open a chat. The conversation heading identifies
the machine; provider settings and agent tools use that machine. Pins stay above all machine groups.
Local and remote chats with the same server ID keep separate messages, drafts and pins. The desktop
remembers machine connections and the selected machine/project/chat. Removing a machine removes
the desktop connection; its files, history and running backend stay on the server.

The SSH tunnel binds only to local loopback and preserves the remote API authority for Host checks.
Reconnection verifies the machine and process identity; compatible reconnects do not resend prompts.
Remote **Files** tabs browse the selected machine through the authenticated tunnel. Directories
load when expanded, in batches; collapse pauses additional pages. Use the tree's reload button to
refresh remote changes. File tabs retain their machine/project when you switch conversations.
Text, Markdown, images and PDFs download only when opened. Unchanged previews reuse a version-checked
private temporary cache (up to four files / 512 MiB per explorer), released when the tab closes.
Text and image limits match local previews; remote PDFs are limited to 256 MiB and render by visible
page after downloading. **Add to message** preserves the remote filename. Disconnected previews
show a retry action; already downloaded documents remain readable.

Remote **Changes** runs Git on the selected machine (Git must be installed there). Each tab
keeps its machine/project when you switch conversations. Stage, unstage and commit use the same
controls as local projects. After a connection failure, refresh to check the remote result before
retrying a write operation.

The terminal button or **Cmd/Ctrl+J** opens an SSH shell in the remote project. Use **+** for more
terminals; their tabs identify the machine and project. Window resizing and Ctrl+C reach the remote
shell, and hiding the terminal keeps it open. A dropped connection closes that shell and its jobs;
**Open new SSH shell** resets this terminal and starts a fresh shell. It does not replay commands.
Ava agent tasks run in the persistent backend and continue independently of terminal connections.
The embedded browser runs on the desktop; agent file and shell tools execute on the selected machine.

Tool output remains expandable. Source files retain literal Markdown-like characters;
Markdown code blocks use their own language for highlighting, including indented code.
Open **Changes** from the conversation header, inspector tab menu, or `/diff` to inspect
working-tree and staged diffs. Select a file to stage or unstage it, then use **Commit…**
to commit staged changes. Git runs asynchronously and honors commit hooks; failures keep
the message and staged files available for correction and retry. Desktop installers are
not yet included.
macOS and Linux are the initial targets; platform-specific packaging and native input-method testing remain necessary.

Run the desktop acceptance scenarios with a deterministic local model server:

```sh
uv run --extra desktop pytest -q tests/test_desktop.py
uv run --extra desktop pyside6-qmllint --max-warnings 0 src/ava/app/desktop/qml/*.qml
```

Without a display server, these tests render the QML window offscreen and deliver Qt mouse, keyboard, and input-method events.
They exercise the actual backend process and HTTP/SSE interface without calling a live model.
The workbench scenarios also verify that model/effort selection and text/image attachments
reach the provider request, skills can be inserted, Markdown renders in the native document,
sidebar handles actually resize, and the embedded browser executes JavaScript and navigates
history, multiple tabs, supported/unsupported video, cross-project session switching, and
cookie persistence across application restarts. All browser tests use synthetic profiles and
a local HTTP server. Context charts are checked against real API totals, including attachments,
small shares, unknown limits and overflow. Runtime QML binding loops fail the suite.
Set `AVA_DESKTOP_SCREENSHOT` to an absolute PNG path in a scratch directory to retain
light/dark, narrow-window, attachment, command, and browser screenshots for visual review.
On macOS, set `QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software` explicitly for
headless checks: an inherited XQuartz `DISPLAY` can prevent the fixture from selecting
its offscreen fallback. Use `QT_QPA_PLATFORM=cocoa QT_QUICK_BACKEND=` for native GPU review.

Run the native macOS performance scenario, including a 2,000-entry directory, a 16,000-line
source file, a 288 KB single-line JavaScript bundle, a 45 KB Markdown report with 300 code blocks,
and scrolling through recycled delegates:

```sh
QT_QPA_PLATFORM=cocoa QT_QUICK_BACKEND= AVA_DESKTOP_PERF=1 uv run --extra desktop pytest -q tests/test_desktop.py -k file_explorer_performance
```

The test writes `file-explorer-benchmark.json` in its pytest temporary directory. It measures
interaction-to-first-frame latency and GUI timer stalls, and checks that live code delegates
remain bounded. Qt APIs: [QFileSystemModel](https://doc.qt.io/qt-6/qfilesystemmodel.html),
[TreeView](https://doc.qt.io/qt-6/qml-qtquick-treeview.html),
[ListView](https://doc.qt.io/qt-6/qml-qtquick-listview.html), and
[WebEngine media support](https://doc.qt.io/qt-6/qtwebengine-features.html#audio-and-video-codecs).

With the same native environment and `AVA_DESKTOP_PERF=1`,
`-k pdf_complex_page_does_not_block_chat` checks typing and GUI timer stalls during
dense-vector PDF rendering, renderer failure/retry, and renderer cancellation on tab close.
`-k pdf_lazy_rendering_performance` exercises a generated 1,000-page PDF and bounded
page delegates. These opt-in benchmarks are separate from regular PDF acceptance tests.

With the same native environment, `-k terminal_output_backpressure` exercises 4.3 MB of
real shell output and writes `terminal-benchmark.json`. It checks GUI stalls, output
completion, bounded transport backlog and bounded terminal scrollback. The regular suite
also checks interactive input, paste ordering, resizing, independent tabs, Ctrl+C and
child-process cleanup on tab and application close.

Real user-service tests are opt-in because they install and remove an OS service for a temporary
Ava home. On macOS, run them from a GUI login session; on Linux, use a systemd user session:

```sh
AVA_SERVICE_TESTS=1 uv run --extra desktop pytest -q tests/test_backend.py tests/test_desktop.py -k 'user_service or background_startup'
```

The isolated Fedora/SSH fixture is reproducible with `bash tests/fixtures/backend-service.sh`.
It requires Docker, OpenSSH and `uv`, builds the current wheel, runs real systemd and SSH tests,
and cleans up its container and temporary credentials. The container has its own privileged
systemd environment, publishes SSH only on loopback, and mounts no host directories. Tests use
a generated key and a host key obtained directly from that container, never the user's SSH keys.
Set `AVA_DESKTOP_SSH_TESTS=1` to also exercise the actual desktop against that server, including
installation, remote tool execution, duplicate chat IDs, pinning, reconnection, desktop reopening,
remote Git operations, SSH terminal input/resize/interrupt, output backpressure and disconnect cleanup.
Set `AVA_SSH_BASE_IMAGE` to an earlier fixture image to test updating an older remote installation.
`AVA_DESKTOP_SSH_SELECTION=remote_machine_defers_update` selects the native deferred-update scenario.

#### Automations

Open **Automations → New automation**. Enter a name and prompt, choose a machine and
project, then set the first local date/time, timezone, interval and total number of runs.
Use **Upcoming runs** to check the schedule before saving. Tasks can run once, or repeat
every N minutes, hours, days or weeks. Each execution creates a separate conversation;
open it from **Run history** or review its result in **Session board**. History starts
with the latest 20 runs; **Review more** loads older results.

Choose **Current folder** to work in the project directly, or **New worktree per run**
to start each execution on its own Git branch. Worktrees remain on disk after completion.
Optional model settings use the execution machine's providers and credentials. Leaving
them blank uses that machine's defaults when the task starts.

The execution machine's Ava backend owns the schedule and stores it in
`$AVA_HOME/automations.sqlite3`. Closing the desktop or disconnecting SSH does not stop
scheduled work. The machine must be awake and its backend running; enable background
startup in Settings if tasks should resume after login. Update older backends before
using Automations. Remote schedules and results stay on the remote machine.

- **Run now** starts an extra execution without using a scheduled repetition.
- **Pause schedule** prevents future starts and leaves the current execution active.
  **Stop** in run history cancels that execution. The same task never runs concurrently
  with itself, including when its current conversation is paused.
- Missed occurrences count toward the total. After downtime, Ava executes only the latest
  missed occurrence and records the others as skipped. Resuming a paused schedule skips
  expired occurrences and continues at the next future time.
- Minute/hour intervals measure elapsed time. Day/week intervals keep the selected local
  time. For daylight-saving transitions, an ambiguous time uses its first occurrence;
  a nonexistent time moves to the next valid minute.
- Editing the prompt or name preserves progress. Changing the schedule starts a new series
  with the entered total. Conflicting edits from another desktop require reloading.
- **Remove** prevents future starts and keeps existing conversations and any current run.
  Removing a project from Ava also hides its automations; it does not cancel scheduled
  work. Pause or remove those tasks first if they should stop.
- After a backend crash, confirmed results are restored. An unconfirmed execution is marked
  **Interrupted** and is not retried automatically. Review its conversation before using
  **Run now**, since its tools may already have made changes.

### Python API

Use the same runtime inside your application. With a provider configured, save this
as a Python file and run it with `uv run python`:

```python
import asyncio
from pathlib import Path

from ava.agent import Agent
from ava.llm import Item, Role, make_text_block, provider_from_environment


async def main():
    async with Agent.create(provider_from_environment(), Path.cwd()) as agent:
        with agent.subscribe(lambda event: print(event.seq, type(event.payload).__name__)):
            await agent.followup(
                Item(
                    role=Role.user,
                    blocks=[make_text_block("Explain src/ava/agent/turn.py")],
                )
            )
            await agent.drive()


asyncio.run(main())
```

Applications submit input, control the driver, and subscribe to durable events without
handling provider wire formats. `Agent.create` also accepts custom `tools` and a
`system_prompt`, so evaluations and embedded applications can configure the runtime.

## Configuration

Settings live in `$AVA_HOME/settings.json` (default: `~/.ava`). Credentials come from the
provider's environment variable or `$AVA_HOME/auth.json`. Selection precedence is:

1. CLI flags
2. Resumed session
3. `AVA_PROVIDER`, `AVA_MODEL`, and `AVA_EFFORT`
4. Settings file
5. Built-in defaults

<details>
<summary>Configure a custom model gateway</summary>

Custom endpoints can be registered in the settings file. The `openai` family uses streaming
Chat Completions, and the `anthropic` family uses Messages; endpoints must support the request
fields and tool-calling behaviour required by the selected adapter:

```json
{
  "provider": "my-gateway",
  "model": "company-model",
  "providers": {
    "my-gateway": {
      "family": "openai",
      "base_url": "https://gateway.internal/v1",
      "api_key_env": "GATEWAY_KEY",
      "models": {
        "company-model": {
          "context_window": 128000,
          "effort_values": ["low", "high"]
        }
      }
    }
  }
}
```

</details>

## Develop

Run the standard checks (the Python test selection matches CI):

```sh
uv sync --extra desktop
uv run --extra desktop pytest -q --ignore=tests/test_desktop.py
uv run --extra desktop ruff check src tests eval
uv run --extra desktop mypy src eval/run.py eval/benchmark.py eval/diagnose.py eval/evolve.py eval/incidents.py eval/integrations/agent_config.py
npm ci
npm run check
npm test
```

Desktop acceptance and performance tests run **manually in a local development
environment only**, not in CI/CD or release jobs. Run desktop acceptance separately:

```sh
uv run --extra desktop pytest -q tests/test_desktop.py
```

See the [desktop testing instructions](#qt-quick-desktop-app) for headless/native
platform selection and opt-in performance, service, and SSH scenarios. QML lint,
Python lint and type checks, frontend checks, and packaged-asset checks remain in
CI/CD; these do not launch the desktop acceptance suite.

When the React source in `src/ava/app/web/frontend/` changes, rebuild the checked-in browser bundle
with `npm run build`.

The acceptance suite focuses on observable invariants: durable input is never lost or duplicated;
tool calls remain paired after abort and recovery; torn tails recover idempotently; pause/resume
preserves valid model history; and provider failures do not discard pending work.


### Try it without an API key

After `uv sync`, run a deterministic installation check:

```sh
printf 'text Ava is ready.\ndone\n' > /tmp/ava-mock.txt
AVA_PROVIDER=mock AVA_MOCK_SCRIPT=/tmp/ava-mock.txt uv run ava -p "hello"
```

Expected output: `Ava is ready.` The mock checks the runtime; it does not solve coding tasks.
