"""Statistics from real log files: exact totals, physical recovery, and bounded hot queries."""
from __future__ import annotations

import json
import os
import statistics
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ava.app.analytics import AnalyticsIndex
from ava.session.log import encode_frame

NOW = datetime(2026, 11, 2, 18, tzinfo=UTC)


def records(rows: list[tuple[str, dict]], start: int = 0) -> bytes:
    return b"".join((json.dumps({"seq": seq, "at": at, **row}) + '\n').encode()
                    for seq, (at, row) in enumerate(rows, start))


def header(identity: str = 'a') -> tuple[str, dict]:
    return ('2026-11-01T08:00:00.000Z', {'kind': 'session/start', 'id': identity, 'cwd': '/fixture', 'provider': 'fixture', 'model': 'test'})


def put(path: Path, rows: list[tuple[str, dict]], *, append: bool = False, start: int = 0) -> None:
    content = records(rows, start)
    with path.open('ab' if append else 'wb') as output:
        output.write(encode_frame(content) if path.suffix == '.zst' else content)


def catch_up(index, sources):
    for _ in range(1000):
        index.scan(sources)
        if not index.query(now=NOW)['indexing']:
            return
    pytest.fail('Index did not catch up')


@pytest.mark.parametrize('suffix', ['.jsonl', '.jsonl.zst'])
def test_analytics_totals_overlap_dst_append_restart_and_replacement(tmp_path, suffix):
    first, second = tmp_path/('a'+suffix), tmp_path/('b'+suffix)
    rows = [header(),
            ('2026-11-01T08:30:00.000Z', {'kind': 'turn/start', 'turn': 1}),
            ('2026-11-01T09:00:00.000Z', {'kind': 'assistant/message', 'item': {'blocks': [{'kind':'tool_call', 'call_id':'read-1', 'name':'read'}]}}),
            ('2026-11-01T09:01:00.000Z', {'kind': 'tool/result', 'item': {'blocks': [{'kind':'tool_result', 'call_id':'read-1'}]}, 'durations':[{'call_id':'read-1','elapsed_ms':10}]}),
            ('2026-11-01T09:02:00.000Z', {'kind': 'skill/loaded', 'name':'review'}),
            ('2026-11-01T10:30:00.000Z', {'kind': 'usage', 'attempt_id':'1', 'tokens':{'input':100,'cached_read':40,'cache_write':20,'cache_write_1h':10,'output':30,'reasoning':10}}),
            ('2026-11-01T10:30:00.000Z', {'kind': 'attempt/timing', 'attempt_id':'1', 'elapsed_ms':50}),
            ('2026-11-01T10:30:00.000Z', {'kind': 'turn/end', 'turn':1, 'reason':'completed'})]
    put(first, rows)
    put(second, [header('b'),
                 ('2026-11-01T09:30:00.000Z', {'kind':'turn/start','turn':1}),
                 ('2026-11-01T11:30:00.000Z', {'kind':'usage','attempt_id':'1','tokens':{'input':200,'output':50}}),
                 ('2026-11-01T11:30:00.000Z', {'kind':'turn/end','turn':1,'reason':'completed'})])
    sources = {str(first): ('p1', False), str(second): ('p2', False)}
    cache = tmp_path/'analytics.sqlite3'
    index = AnalyticsIndex(cache)
    try:
        catch_up(index, sources)
        report = index.query(7, 'America/Los_Angeles', now=NOW)
        assert report['totals']['tokens'] == 440  # 190 + 250; reasoning/1h cache write are subsets.
        assert report['totals']['responses'] == 2 and not report['totals']['missing_usage']
        assert report['totals']['active_ms'] == 3*3600_000
        assert report['totals']['run_ms'] == 4*3600_000
        assert report['totals']['tools'] == report['totals']['skills'] == 1
        dst = next(day for day in report['days'] if day['date'] == '2026-11-01')
        assert dst['end'] - dst['start'] == 25*3600_000
        assert index.query(project='p1', now=NOW)['totals']['tokens'] == 190
        frames = index.frames_read
        computed = index.days_computed
        for _ in range(3):
            index.scan(sources)
            assert index.query(7, 'America/Los_Angeles', now=NOW) == report
        assert index.frames_read == frames and index.days_computed == computed
        put(first, [('2026-11-02T12:00:00.000Z', {'kind':'attempt/timing','attempt_id':'missing','elapsed_ms':20})], append=True, start=len(rows))
        index.scan(sources)
        changed = index.query(7, 'America/Los_Angeles', now=NOW)
        assert changed['totals']['missing_usage'] == 1 and changed['totals']['tokens'] == 440
        assert index.days_computed == computed + 1, 'Only the affected day should be recalculated'
    finally:
        index.close()
    index = AnalyticsIndex(cache)
    try:
        catch_up(index, sources)
        assert index.frames_read == 0, 'Restart validates a physical checkpoint without decompressing history'
        assert index.query(now=NOW)['totals']['tokens'] == 440
        replacement = first.with_name('replacement'+suffix)
        put(replacement, [header(), ('2026-11-01T10:00:00.000Z', {'kind':'usage','attempt_id':'new','tokens':{'input':1,'output':2}})])
        replacement.replace(first)
        catch_up(index, sources)
        assert index.query(now=NOW)['totals']['tokens'] == 253
        index.scan({str(first): ('p1', False)})
        assert index.query(now=NOW)['totals']['tokens'] == 3, 'Hidden project contributions must disappear'
        first.unlink()
        index.scan({str(first): ('p1', False)})
        assert index.query(now=NOW)['unavailable'] == 1
        assert index.query(now=NOW)['totals']['tokens'] == 0
    finally:
        index.close()


def test_analytics_checkpoints_only_complete_frames(tmp_path):
    log = tmp_path/'session.jsonl.zst'
    put(log, [header()])
    frame = encode_frame(records([('2026-11-02T12:00:00.000Z', {'kind':'usage','attempt_id':'1','tokens':{'input':3,'output':4}})], 1))
    with log.open('ab') as output:
        output.write(frame[:-2])
    index = AnalyticsIndex(tmp_path/'analytics.sqlite3')
    sources = {str(log): ('p1', False)}
    try:
        index.scan(sources)
        assert index.query(now=NOW)['totals']['tokens'] == 0
        assert index.query(now=NOW)['incomplete'] == 1
        with log.open('ab') as output:
            output.write(frame[-2:])
        index.scan(sources)
        assert index.query(now=NOW)['totals']['tokens'] == 7
        index.scan(sources)
        assert index.query(now=NOW)['totals']['tokens'] == 7
    finally:
        index.close()


@pytest.mark.skipif(os.environ.get('AVA_ANALYTICS_PERF') != '1', reason='Explicit million-event performance run')
def test_analytics_million_events(tmp_path):
    import threading

    import psutil

    sources = {}
    expected = 0
    for session in range(1000):
        at = (NOW - timedelta(days=session % 30)).isoformat()
        rows = [(at, header(str(session))[1]), (at, {'kind':'usage','attempt_id':'1','tokens':{'input':session,'output':10}})]
        rows += [(at, {'kind':'assistant/chunk','attempt_id':'1','delta':'Streaming progress; do not retain this text.'})]*998
        path = tmp_path/f'{session}.jsonl.zst'
        put(path, rows)
        sources[str(path)] = (f'p{session%10}', False)
        # Independent recount from persisted JSON, rather than querying index internals.
        import zstandard
        events = zstandard.ZstdDecompressor().decompress(path.read_bytes()).splitlines()
        for raw in events:
            event = json.loads(raw)
            if event['kind'] == 'usage':
                expected += sum(event['tokens'].values())
    cache = tmp_path/'analytics.sqlite3'
    index = AnalyticsIndex(cache)
    index.scan(sources, budget=.001)
    before_restart = index.records_read
    assert 0 < before_restart < 1_000_000, 'Backfill must yield between bounded batches'
    index.close()
    index = AnalyticsIndex(cache)
    process = psutil.Process()
    baseline = process.memory_info().rss
    peak = [baseline]
    stopped = threading.Event()
    def sample():
        while not stopped.wait(.01):
            peak[0] = max(peak[0], process.memory_info().rss)
    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        started = time.perf_counter()
        catch_up(index, sources)
        cold = time.perf_counter()-started
        assert index.records_read + before_restart == 1_000_000
        assert index.query(30, now=NOW)['totals']['tokens'] == expected
        frames = index.frames_read
        samples = []
        for count in [7,30]*15:
            started = time.perf_counter()
            index.scan(sources)
            index.query(count, now=NOW)
            samples.append((time.perf_counter()-started)*1000)
        p95 = statistics.quantiles(samples, n=20)[18]
        assert index.frames_read == frames
        assert p95 < 100, samples
        growth = (peak[0]-baseline)/1024**2
        assert growth < 64, f'Indexing retained too much process memory: {growth:.1f} MiB'
        print(f'Analytics: 1,000 sessions / 1,000,000 events, cold resume {cold:.2f}s, hot p95 {p95:.2f}ms, sampled process RSS growth {growth:.1f} MiB; zero history decompressions on hot queries')
    finally:
        stopped.set()
        sampler.join()
        index.close()
