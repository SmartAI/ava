"""Desktop entrypoint with optional Qt dependencies and a single owned backend."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QLockFile, QSettings, QUrl
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from ava.base import AvaError, ava_home

from .controller import Controller


def create_engine(controller: Controller) -> QQmlApplicationEngine:
    engine = QQmlApplicationEngine()
    engine.setInitialProperties({"backend": controller})
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
    try:
        controller.start()
        return app.exec()
    finally:
        controller.runtime.ensure_stopped()
        engine.deleteLater()
        lock.unlock()
