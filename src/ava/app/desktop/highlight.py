"""A line model for viewport-only QML rendering, with incremental syntax colouring."""

from html import escape
from time import perf_counter

from pygments.lexers import TextLexer, get_lexer_for_filename
from pygments.styles import get_style_by_name
from pygments.util import ClassNotFound
from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QModelIndex,
    QObject,
    Qt,
    QTimer,
    Signal,
    Slot,
)


class CodeDocument(QAbstractListModel):
    changed = Signal()
    TEXT = int(Qt.ItemDataRole.UserRole) + 1
    HTML = TEXT + 1
    NUMBER = TEXT + 2
    ROW_LENGTH = 256

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._source = ""
        self._lines: list[str] = []
        self._offsets: list[int] = []
        self._numbers: list[int] = []
        self._html: dict[int, str] = {}
        self._configuration: tuple[str, str, bool] | None = None
        self._columns = 0
        self._timer = QTimer(self)
        self._timer.setInterval(1)
        self._timer.timeout.connect(self._advance)

    def roleNames(self) -> dict:
        return {self.TEXT: b"lineText", self.HTML: b"lineHtml", self.NUMBER: b"lineNumber"}

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._lines)

    def data(self, index, role=int(Qt.ItemDataRole.DisplayRole)):
        row = index.row()
        if not 0 <= row < len(self._lines):
            return None
        if role == self.TEXT:
            return self._lines[row]
        if role == self.NUMBER:
            return self._numbers[row]
        if role == self.HTML:
            return self._html.get(row, "<pre>" + escape(self._lines[row]) + "</pre>")
        return None

    @Property(int, notify=changed)
    def count(self) -> int:
        return len(self._lines)

    @Property(int, notify=changed)
    def columns(self) -> int:
        return self._columns

    @Slot(str, str, bool)
    def load(self, source: str, filename: str, dark: bool) -> None:
        configuration = (source, filename, dark)
        if configuration == self._configuration:
            return
        self._configuration = configuration
        self._timer.stop()
        self.beginResetModel()
        self._source = source
        self._lines = []
        self._offsets = []
        self._numbers = []
        offset = 0
        # A minified bundle can be one enormous line. Bound each native TextEdit
        # as well as the number of delegates, without discarding source text.
        for number, line in enumerate(source.split("\n") if source else [], 1):
            for start in range(0, max(1, len(line)), self.ROW_LENGTH):
                self._lines.append(line[start : start + self.ROW_LENGTH])
                self._offsets.append(offset + start)
                self._numbers.append(number if start == 0 else 0)
            offset += len(line) + 1
        self._html.clear()
        self._columns = max((len(line.expandtabs(4)) for line in self._lines), default=0)
        self.endResetModel()
        self.changed.emit()
        try:
            lexer = get_lexer_for_filename(filename, stripnl=False, ensurenl=False)
        except ClassNotFound:
            lexer = TextLexer(stripnl=False, ensurenl=False)
        self._style = get_style_by_name("native" if dark else "friendly")
        self._formats: dict = {}
        self._tokens = ((token, value) for _, token, value in lexer.get_tokens_unprocessed(source))
        self._line = 0
        self._column = 0
        self._parts: list[str] = []
        self._timer.start()

    def _advance(self) -> None:
        deadline = perf_counter() + 0.004
        first = self._line
        while perf_counter() < deadline:
            try:
                token, value = next(self._tokens)
            except StopIteration:
                self._finish_line()
                self._timer.stop()
                break
            style = self._formats.get(token)
            if style is None:
                spec = self._style.style_for_token(token)
                style = (
                    ("color:#" + spec["color"] + ";" if spec["color"] else "")
                    + ("font-weight:bold;" if spec["bold"] else "")
                    + ("font-style:italic;" if spec["italic"] else "")
                )
                self._formats[token] = style
            for index, part in enumerate(value.split("\n")):
                if index:
                    self._finish_line()
                while part:
                    if self._column == self.ROW_LENGTH:
                        self._finish_line()
                    length = min(len(part), self.ROW_LENGTH - self._column)
                    self._parts.append(
                        '<span style="' + style + '">' + escape(part[:length]) + "</span>"
                    )
                    self._column += length
                    part = part[length:]
        if self._line > first and first < len(self._lines):
            self.dataChanged.emit(
                self.index(first),
                self.index(min(self._line - 1, len(self._lines) - 1)),
                [self.HTML],
            )

    def _finish_line(self) -> None:
        self._html[self._line] = "<pre>" + "".join(self._parts) + "</pre>"
        self._line += 1
        self._column = 0
        self._parts = []

    @Slot(int, int, int, int, result=str)
    def selectedText(self, first: int, start: int, last: int, end: int) -> str:
        if (first, start) > (last, end):
            first, start, last, end = last, end, first, start
        if not 0 <= first <= last < len(self._lines):
            return ""
        # QTextEdit positions are UTF-16 offsets. Map visual continuations back to
        # the original source, so copying never inserts synthetic line breaks.
        prefix = (
            self._lines[first]
            .encode("utf-16-le")[: max(0, start) * 2]
            .decode("utf-16-le", "ignore")
        )
        suffix = (
            self._lines[last].encode("utf-16-le")[: max(0, end) * 2].decode("utf-16-le", "ignore")
        )
        return self._source[self._offsets[first] + len(prefix) : self._offsets[last] + len(suffix)]
