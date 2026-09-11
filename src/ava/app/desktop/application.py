"""Desktop entrypoint with optional Qt dependencies and a persistent local backend."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, QLockFile, QSettings, QUrl
from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication, QIcon
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickWindow
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtWebEngineQuick import QtWebEngineQuick

from ava.base import AvaError, ava_home

from .controller import Controller
from .quick_chat import QuickChatShortcut, toggle_quick_chat


def create_engine(controller: Controller) -> QQmlApplicationEngine:
    available = set(QFontDatabase.families())
    general = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    # Offscreen/Linux platforms can return a generic alias rather than a real UI font.
    candidates = [general.family(), "Helvetica Neue", "Segoe UI", "Noto Sans", "DejaVu Sans"]
    latin = next((name for name in candidates if name in available), general.family())
    cjk = next(
        (
            name
            for name in (
                "PingFang SC",
                "Microsoft YaHei UI",
                "Noto Sans CJK SC",
                "Noto Sans SC",
                "WenQuanYi Micro Hei",
            )
            if name in available
        ),
        latin,
    )
    general.setFamilies(list(dict.fromkeys([latin, cjk])))
    general.setPixelSize(14)
    general.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    general.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    QGuiApplication.setFont(general)
    QQuickWindow.setTextRenderType(QQuickWindow.TextRenderType.NativeTextRendering)
    fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()
    code = next(
        (
            name
            for name in (fixed, "Menlo", "Cascadia Mono", "DejaVu Sans Mono")
            if name in available
        ),
        fixed,
    )
    engine = QQmlApplicationEngine()
    engine.addImageProvider("ava-pdf", controller.pdf_images)
    engine.setInitialProperties({"backend": controller, "codeFont": code})
    engine.load(QUrl.fromLocalFile(str(Path(__file__).parent / "qml" / "Main.qml")))
    return engine


def run() -> int:
    parser = argparse.ArgumentParser(description="Ava Qt Quick desktop application")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--effort")
    args = parser.parse_args()
    cwd = args.project.expanduser().resolve()
    if not cwd.is_dir():
        parser.error("--project must name an existing directory")
    try:
        state = ava_home()
        state.mkdir(parents=True, exist_ok=True)
    except (AvaError, OSError) as error:
        print(f"ava-desktop: {error}", file=sys.stderr)
        return 1
    QtWebEngineQuick.initialize()
    app = QGuiApplication([sys.argv[0]])
    app.setOrganizationName("Ava")
    app.setApplicationName("Ava Desktop")
    app.setWindowIcon(QIcon(str(Path(__file__).parents[1] / "web/assets/ava-logo.svg")))
    QQuickStyle.setStyle("Basic")
    lock = QLockFile(str(state / "desktop.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        print("ava-desktop: another desktop instance is using this Ava home", file=sys.stderr)
        return 1
    settings = QSettings(str(state / "desktop.ini"), QSettings.Format.IniFormat)
    arguments = []
    for name in ("provider", "model", "effort"):
        if getattr(args, name):
            arguments.extend([f"--{name}", getattr(args, name)])
    controller = Controller(cwd, arguments, settings)
    app.setQuitOnLastWindowClosed(False)
    controller.closed.connect(app.quit)
    engine = create_engine(controller)
    if not engine.rootObjects():
        lock.unlock()
        return 1
    shortcut = QuickChatShortcut(app)
    # Keep the root wrapper alive: releasing it can invalidate child window wrappers.
    window = engine.rootObjects()[0]
    panel = window.findChild(QQuickWindow, "quickChatWindow")
    if panel is not None:
        shortcut.activated.connect(lambda: toggle_quick_chat(panel))
        if not shortcut.register():
            print(f"ava-desktop: {shortcut.error}", file=sys.stderr)
    try:
        controller.start()
        return app.exec()
    finally:
        shortcut.close()
        for machine in controller._machines.values():
            machine.runtime.ensure_stopped()
        engine.deleteLater()
        # app.exec() has stopped. Destroy the QML tree while its Python backend is
        # still alive, rather than leaving deletion to interpreter shutdown.
        QCoreApplication.sendPostedEvents(engine, QEvent.Type.DeferredDelete)
        lock.unlock()
