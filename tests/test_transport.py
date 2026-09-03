from ava.transport import SseParser


def _feed_in_chunks(text: str, size: int) -> list:
    parser = SseParser()
    events = []
    for index in range(0, len(text), size):
        events.extend(parser.feed(text[index : index + size]))
    events.extend(parser.finish())
    return [(event.event, event.data) for event in events]


def test_sse_events_split_across_every_chunk_boundary():
    text = 'event: message\ndata: {"a":1}\n\ndata: first\ndata: second\n\n: comment\n\nevent: only\n\ndata: tail'
    expected = [("message", '{"a":1}'), ("", "first\nsecond"), ("", "tail")]
    for size in range(1, len(text) + 1):
        assert _feed_in_chunks(text, size) == expected, size


def test_sse_crlf_and_bom():
    assert _feed_in_chunks("﻿data: a\r\n\r\ndata: b\r\n\r\n", 3) == [("", "a"), ("", "b")]


def test_sse_split_crlf_across_chunks():
    parser = SseParser()
    events = parser.feed("data: x\r")
    assert events == []
    events += parser.feed("\n\r\n")
    assert [(e.event, e.data) for e in events] == [("", "x")]
