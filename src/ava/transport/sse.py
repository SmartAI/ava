"""A byte-fed Server-Sent Events parser with no knowledge of what the events mean."""

from __future__ import annotations

from dataclasses import dataclass, field

UTF8_BOM = "﻿"


@dataclass(slots=True)
class SseEvent:
    event: str = ""
    data: str = ""


@dataclass(slots=True)
class SseParser:
    _pending: str = ""
    _current: SseEvent = field(default_factory=SseEvent)
    _has_data: bool = False
    _first_line: bool = True

    def feed(self, text: str) -> list[SseEvent]:
        self._pending += text
        return self._parse_available(False)

    def finish(self) -> list[SseEvent]:
        return self._parse_available(True)

    def _parse_available(self, finishing: bool) -> list[SseEvent]:
        events: list[SseEvent] = []
        pending = self._pending
        offset = 0
        while offset < len(pending):
            cr = pending.find("\r", offset)
            lf = pending.find("\n", offset)
            candidates = [index for index in (cr, lf) if index != -1]
            if not candidates:
                break
            end = min(candidates)
            # A trailing CR may be the first half of CRLF split across two chunks.
            if not finishing and pending[end] == "\r" and end + 1 == len(pending):
                break
            self._process_line(pending[offset:end], events)
            offset = end + 1
            if pending[end] == "\r" and offset < len(pending) and pending[offset] == "\n":
                offset += 1
        self._pending = pending[offset:]
        if finishing:
            # Accept a complete final line without its blank terminator when the connection
            # closes immediately after its last data field.
            if self._pending:
                self._process_line(self._pending, events)
                self._pending = ""
            self._dispatch(events)
        return events

    def _process_line(self, line: str, events: list[SseEvent]) -> None:
        if self._first_line:
            self._first_line = False
            if line.startswith(UTF8_BOM):
                line = line[len(UTF8_BOM) :]
        if line == "":
            self._dispatch(events)
            return
        if line.startswith(":"):
            return
        colon = line.find(":")
        if colon == -1:
            fieldname, value = line, ""
        else:
            fieldname, value = line[:colon], line[colon + 1 :]
        if value.startswith(" "):
            value = value[1:]
        if fieldname == "event":
            self._current.event = value
        elif fieldname == "data":
            # Consecutive data fields form one payload separated by newlines.
            self._current.data += value + "\n"
            self._has_data = True

    def _dispatch(self, events: list[SseEvent]) -> None:
        if self._has_data:
            self._current.data = self._current.data[:-1]
            events.append(self._current)
        self._current = SseEvent()
        self._has_data = False
