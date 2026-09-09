"""Rebuildable session statistics. Read complete log units; retain no conversation content."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from ava.base import AvaError
from ava.session.log import ZSTD_MAGIC, _scan_plain, _scan_zstd

TOKEN_FIELDS = ("input", "cached_read", "cache_write", "output", "reasoning")


def merge_intervals(intervals: list) -> list[list[int]]:
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def totals(days: list[dict]) -> dict:
    names = (*TOKEN_FIELDS, "responses", "missing_usage", "tools", "tool_errors", "skills", "runs", "run_ms", "active_ms")
    result = {name: sum(day.get(name, 0) for day in days) for name in names}
    result["tokens"] = sum(result[key] for key in TOKEN_FIELDS if key != "reasoning")
    return result


class AnalyticsIndex:
    """Owned by one background executor. Checkpoints and facts commit together."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5, check_same_thread=False)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        try:
            if self.db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                raise sqlite3.DatabaseError("Analytics cache uses a different aggregation version.")
            self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS logs (
                path TEXT PRIMARY KEY, project TEXT NOT NULL, visible INTEGER NOT NULL DEFAULT 1,
                device INTEGER DEFAULT 0, inode INTEGER DEFAULT 0, size INTEGER DEFAULT 0,
                mtime INTEGER DEFAULT 0, offset INTEGER DEFAULT 0, seq INTEGER DEFAULT -1,
                frame_start INTEGER DEFAULT 0, frame_size INTEGER DEFAULT 0, fingerprint TEXT DEFAULT '',
                model TEXT DEFAULT '', last_at INTEGER DEFAULT 0, error TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS attempts (
                path TEXT REFERENCES logs(path) ON DELETE CASCADE, id TEXT, at INTEGER, model TEXT,
                input INTEGER, cached_read INTEGER, cache_write INTEGER, output INTEGER, reasoning INTEGER,
                PRIMARY KEY(path,id));
            CREATE INDEX IF NOT EXISTS attempts_at ON attempts(at);
            CREATE TABLE IF NOT EXISTS calls (
                path TEXT REFERENCES logs(path) ON DELETE CASCADE, seq INTEGER, id TEXT, at INTEGER,
                name TEXT, finished INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, elapsed INTEGER DEFAULT 0,
                PRIMARY KEY(path,seq,id));
            CREATE INDEX IF NOT EXISTS calls_id ON calls(path,id);
            CREATE INDEX IF NOT EXISTS calls_at ON calls(at);
            CREATE TABLE IF NOT EXISTS skills (
                path TEXT REFERENCES logs(path) ON DELETE CASCADE, seq INTEGER, at INTEGER, name TEXT,
                PRIMARY KEY(path,seq));
            CREATE INDEX IF NOT EXISTS skills_at ON skills(at);
            CREATE TABLE IF NOT EXISTS runs (
                path TEXT REFERENCES logs(path) ON DELETE CASCADE, turn INTEGER, start INTEGER, end INTEGER,
                PRIMARY KEY(path,turn));
            CREATE INDEX IF NOT EXISTS runs_time ON runs(start,end);
            CREATE TABLE IF NOT EXISTS days (
                zone TEXT, project TEXT, day TEXT, start INTEGER, end INTEGER, body TEXT,
                PRIMARY KEY(zone,project,day));
            PRAGMA user_version=1;
            """)
        except sqlite3.DatabaseError:
            self.db.close()
            raise
        self.sources: dict[str, tuple[str, bool]] = {}
        self.checked: set[str] = set()
        self.next_source = 0
        self.frames_read = self.records_read = self.days_computed = 0

    def close(self) -> None:
        self.db.close()

    def _invalidate(self, project: str, start: int = 0, end: int = 2**63 - 1) -> None:
        self.db.execute("DELETE FROM days WHERE project IN ('',?) AND start<=? AND end>?", (project, end, start))

    def scan(self, sources: dict[str, tuple[str, bool]], budget: float = .08) -> bool:
        """A bounded pass; callers yield between passes. Hot logs require only stat()."""
        if sources != self.sources:
            with self.db:
                for row in self.db.execute("SELECT path,project,visible FROM logs").fetchall():
                    visible = row["path"] in sources
                    project = sources.get(row["path"], (row["project"], False))[0]
                    if bool(row["visible"]) != visible or project != row["project"]:
                        self._invalidate(row["project"])
                        self._invalidate(project)
                        self.db.execute("UPDATE logs SET visible=?,project=? WHERE path=?", (visible, project, row["path"]))
            self.sources = dict(sources)
        paths = list(sources)
        if not paths:
            return False
        deadline = time.monotonic() + budget
        changed = False
        for _ in paths:
            self.next_source %= len(paths)
            path = paths[self.next_source]
            self.next_source += 1
            changed |= self._scan_file(path, sources[path][0])
            if time.monotonic() >= deadline:
                break
        return changed

    def _scan_file(self, path: str, project: str) -> bool:
        previous = self.db.execute("SELECT * FROM logs WHERE path=?", (path,)).fetchone()
        try:
            info = os.stat(path)
            if previous and path in self.checked and (previous["inode"], previous["device"], previous["size"], previous["mtime"]) == (info.st_ino, info.st_dev, info.st_size, info.st_mtime_ns):
                return False
            with open(path, "rb") as source, self.db:
                fd = source.fileno()
                info = os.fstat(fd)
                reset = bool(previous and ((previous["inode"], previous["device"]) != (info.st_ino, info.st_dev)
                             or info.st_size < previous["offset"]
                             or (info.st_size == previous["size"] and info.st_mtime_ns != previous["mtime"])))
                if previous and previous["fingerprint"] and not reset:
                    frame = os.pread(fd, previous["frame_size"], previous["frame_start"])
                    reset = hashlib.sha256(frame).hexdigest() != previous["fingerprint"]
                if reset:
                    self._invalidate(project)
                    self.db.execute("DELETE FROM logs WHERE path=?", (path,))
                    previous = None
                self.db.execute("INSERT OR IGNORE INTO logs(path,project) VALUES (?,?)", (path, project))
                row = dict(self.db.execute("SELECT * FROM logs WHERE path=?", (path,)).fetchone())
                if row["error"]:
                    self._invalidate(project)
                def consume(record: str, start: int, length: int, fingerprint: str) -> None:
                    event = json.loads(record)
                    if event["seq"] != row["seq"] + 1 or (row["seq"] == -1 and event["kind"] != "session/start"):
                        raise ValueError("Session event sequence changed; rebuild this session's statistics.")
                    self._event(path, project, row, event)
                    row.update(seq=event["seq"], frame_start=start, frame_size=length, fingerprint=fingerprint)
                    self.records_read += 1
                scanner = _scan_zstd if os.pread(fd, 4, 0) == ZSTD_MAGIC else _scan_plain
                scan = scanner(None, header_only=False, fd=fd, consume=consume, start_offset=row["offset"], unit_limit=32)
                self.frames_read += scan.unit_count
                # Never checkpoint the complete-looking prefix of a torn frame.
                complete = scan.end_offset == info.st_size or scan.torn_offset is not None
                self.db.execute("""UPDATE logs SET device=?,inode=?,size=?,mtime=?,offset=?,seq=?,
                    frame_start=?,frame_size=?,fingerprint=?,model=?,last_at=?,error='' WHERE path=?""",
                    (info.st_dev, info.st_ino, info.st_size if complete else -1, info.st_mtime_ns,
                     scan.end_offset, row["seq"], row["frame_start"], row["frame_size"], row["fingerprint"], row["model"], row["last_at"], path))
                self.checked.add(path)
                return bool(scan.unit_count or reset)
        except (AvaError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
            self.checked.add(path)
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO logs(path,project) VALUES (?,?)", (path, project))
                self.db.execute("UPDATE logs SET error=? WHERE path=?", (str(error)[:300], path))
                self._invalidate(project)
            return False

    def _event(self, path: str, project: str, row: dict, event: dict) -> None:
        kind, seq = event["kind"], event["seq"]
        at = int(datetime.fromisoformat(event["at"]).timestamp() * 1000)
        if event.get("reason") != "interrupted":
            row["last_at"] = at
        if kind in ("session/start", "selection"):
            row["model"] = event.get("model", "")
        elif kind in ("usage", "attempt/timing"):
            identity = event.get("attempt_id") or f"event-{seq}"
            self.db.execute("INSERT OR IGNORE INTO attempts(path,id,at,model) VALUES (?,?,?,?)", (path, identity, at, row["model"]))
            if kind == "usage":
                tokens = event.get("tokens", {})
                values = [tokens.get(key) for key in TOKEN_FIELDS]
                if any(value is not None and (type(value) is not int or value < 0) for value in values):
                    raise ValueError("Invalid token usage in session history.")
                self.db.execute("UPDATE attempts SET input=?,cached_read=?,cache_write=?,output=?,reasoning=? WHERE path=? AND id=?", (*values, path, identity))
            fact_at = self.db.execute("SELECT at FROM attempts WHERE path=? AND id=?", (path, identity)).fetchone()[0]
            self._invalidate(project, fact_at, fact_at)
        elif kind == "assistant/message":
            for block in event.get("item", {}).get("blocks", []):
                if block.get("kind") == "tool_call":
                    self.db.execute("INSERT OR IGNORE INTO calls(path,seq,id,at,name) VALUES (?,?,?,?,?)", (path, seq, block.get("call_id", ""), at, block.get("name", "Unknown tool")))
        elif kind == "tool/result":
            durations = {item["call_id"]: item["elapsed_ms"] for item in event.get("durations", [])}
            for block in event.get("item", {}).get("blocks", []):
                if block.get("kind") != "tool_result" or block.get("origin") == "skipped":
                    continue
                identity = block.get("call_id", "")
                self.db.execute("""UPDATE calls SET finished=1,failed=?,elapsed=?,at=? WHERE path=? AND id=?
                    AND seq=(SELECT MAX(seq) FROM calls WHERE path=? AND id=?)""",
                    (bool(block.get("is_error")), durations.get(identity, 0), at, path, identity, path, identity))
            self._invalidate(project, at, at)
        elif kind == "skill/loaded":
            self.db.execute("INSERT OR IGNORE INTO skills VALUES (?,?,?,?)", (path, seq, at, event["name"]))
            self._invalidate(project, at, at)
        elif kind == "turn/start":
            self.db.execute("INSERT OR IGNORE INTO runs(path,turn,start) VALUES (?,?,?)", (path, event["turn"], at))
        elif kind == "turn/end":
            run = self.db.execute("SELECT start FROM runs WHERE path=? AND turn=?", (path, event["turn"])).fetchone()
            if run:
                end = max(run[0], min(at, row["last_at"]) if event.get("reason") == "interrupted" else at)
                self.db.execute("UPDATE runs SET end=? WHERE path=? AND turn=?", (end, path, event["turn"]))
                self._invalidate(project, run[0], end)

    def _day(self, day: date, zone: ZoneInfo, project: str) -> dict:
        cached = self.db.execute("SELECT body FROM days WHERE zone=? AND project=? AND day=?", (zone.key, project, day.isoformat())).fetchone()
        if cached:
            return cast(dict, json.loads(cached[0]))
        self.days_computed += 1
        start = int(datetime.combine(day, datetime.min.time(), zone).timestamp() * 1000)
        end = int(datetime.combine(day + timedelta(days=1), datetime.min.time(), zone).timestamp() * 1000)
        where = "logs.visible=1 AND logs.error=''" + (" AND logs.project=?" if project else "")
        params = (project,) if project else ()
        usage = self.db.execute(f"""SELECT COUNT(*) responses, SUM(input IS NULL OR output IS NULL) missing_usage,
            {','.join('SUM('+key+') '+key for key in TOKEN_FIELDS)} FROM attempts JOIN logs USING(path)
            WHERE {where} AND at>=? AND at<?""", (*params, start, end)).fetchone()
        body: dict[str, Any] = {key: usage[key] or 0 for key in usage.keys()}
        tools = self.db.execute(f"""SELECT name,COUNT(*) count,SUM(failed) errors,SUM(elapsed) elapsed_ms FROM calls JOIN logs USING(path)
            WHERE {where} AND finished=1 AND at>=? AND at<? GROUP BY name""", (*params, start, end)).fetchall()
        skills = self.db.execute(f"""SELECT name,COUNT(*) count FROM skills JOIN logs USING(path)
            WHERE {where} AND at>=? AND at<? GROUP BY name""", (*params, start, end)).fetchall()
        runs = self.db.execute(f"SELECT start,end FROM runs JOIN logs USING(path) WHERE {where} AND end>? AND start<?", (*params, start, end)).fetchall()
        intervals = [[max(start, run[0]), min(end, run[1])] for run in runs]
        body.update(date=day.isoformat(), start=start, end=end, tool_counts=[dict(r) for r in tools], skill_counts=[dict(r) for r in skills],
                    tools=sum(r["count"] for r in tools), tool_errors=sum(r["errors"] for r in tools), skills=sum(r["count"] for r in skills),
                    intervals=merge_intervals(intervals), run_ms=sum(b-a for a, b in intervals), runs=sum(start <= run[1] < end for run in runs))
        body["active_ms"] = sum(b-a for a, b in body["intervals"])
        body["tokens"] = sum(body[key] for key in TOKEN_FIELDS if key != "reasoning")
        self.db.execute("INSERT OR REPLACE INTO days VALUES (?,?,?,?,?,?)", (zone.key, project, day.isoformat(), start, end, json.dumps(body)))
        # Bound cache growth from arbitrary timezones/project selections.
        self.db.execute("DELETE FROM days WHERE rowid IN (SELECT rowid FROM days ORDER BY rowid DESC LIMIT -1 OFFSET 4096)")
        return body

    def query(self, count: int = 7, timezone: str = "UTC", project: str = "", *, now: datetime | None = None) -> dict:
        if count not in (7, 30):
            raise ValueError("Choose 7 or 30 days.")
        zone = ZoneInfo(timezone)
        now = now or datetime.now(UTC)
        last = now.astimezone(zone).date()
        with self.db:
            days = [self._day(last - timedelta(days=offset), zone, project) for offset in reversed(range(count))]
        # Running intervals are temporary display values, never cached as completed work.
        active = []
        for run in self.db.execute("SELECT runs.path,start,logs.project FROM runs JOIN logs USING(path) WHERE end IS NULL AND visible=1 AND error=''"):
            if self.sources.get(run["path"], ("", False))[1] and (not project or run["project"] == project):
                active.append([run["start"], int(now.timestamp() * 1000)])
        for day in days:
            live = [[max(day["start"], a), min(day["end"], b)] for a, b in active if b > day["start"] and a < day["end"]]
            day["intervals"] = merge_intervals(day["intervals"] + live)
            day["active_ms"] = sum(b-a for a, b in day["intervals"])
            day["run_ms"] += sum(b-a for a, b in live)
        condition = "visible=1" + (" AND project=?" if project else "")
        rows = self.db.execute(f"SELECT error,size,offset FROM logs WHERE {condition}", (project,) if project else ()).fetchall()
        return {"days": days, "totals": totals(days), "timezone": timezone, "as_of": now.isoformat(),
                "indexing": any(r["size"] == -1 for r in rows) or any(path not in self.checked for path in self.sources),
                "sessions": len(rows), "unavailable": sum(bool(r["error"]) for r in rows),
                "incomplete": sum(r["size"] > r["offset"] for r in rows), "active_sessions": len(active)}
