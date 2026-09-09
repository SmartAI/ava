"""Private, pipe-only PDF rasterizer. Never shares PDFium's lock with the GUI."""

from __future__ import annotations

import json
import struct
import sys

from PySide6.QtCore import QSize
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtPdf import QPdfDocument


def main() -> None:
    application = QGuiApplication(["ava-pdf-renderer"])
    while line := sys.stdin.buffer.readline(65537):
        image = QImage()
        document = QPdfDocument()
        try:
            if len(line) > 65536:
                break
            path, password, page, width, height = json.loads(line)
            if not (isinstance(path, str) and isinstance(password, str)
                    and isinstance(page, int) and page >= 0
                    and isinstance(width, int) and 0 < width <= 4096
                    and isinstance(height, int) and 0 < height <= 4096):
                raise ValueError("Invalid PDF render request")
            document.setPassword(password)
            if document.load(path) == QPdfDocument.Error.None_ and page < document.pageCount():
                image = document.render(page, QSize(width, height)).convertToFormat(QImage.Format.Format_RGBA8888)
        except (ValueError, TypeError, OSError):
            pass
        finally:
            document.close()
        sys.stdout.buffer.write(struct.pack("!III", image.width(), image.height(), image.sizeInBytes()))
        if not image.isNull():
            sys.stdout.buffer.write(image.constBits())
        sys.stdout.buffer.flush()
    application.quit()


if __name__ == "__main__":
    main()
