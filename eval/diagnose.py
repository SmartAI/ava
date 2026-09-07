"""Read-only context evidence from existing sessions; findings are hypotheses, not grades.

Only evidence coordinates and sizes leave the session by default. Arguments, prompts, source
code, and tool output are never copied into the report. Character counts are Unicode code
points, not provider tokens or costs. No tool, provider, replay, or lifecycle repair is invoked.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import zstandard

from ava.base import AvaError
from ava.llm.types import ContentBlockKind, Origin
from ava.session.event import AssistantMessage, SessionStart, ToolResult
from ava.session.log import Log, OpenMode
from ava.session.session import Session

MAX_STORED_BYTES = 16 * 1024 * 1024
MAX_DECODED_BYTES = 128 * 1024 * 1024
MAX_EVENTS = 100_000
MAX_TOOL_CALLS = 10_000
MAX_EVIDENCE = 1_000


def _read_bounded(path: Path) -> bytes:
    with path.open("rb") as source:
        data = source.read(MAX_STORED_BYTES + 1)
    if len(data) > MAX_STORED_BYTES:
        raise ValueError("session exceeds the 16 MiB stored-input limit")
    return data


def _check_decoded_size(data: bytes, path: Path) -> None:
    if not path.name.endswith(".jsonl.zst"):
        return
    total = 0
    # Preflight the cumulative expansion before the canonical reader loads all records.
    # The canonical reader subsequently validates frames and handles readable torn tails.
    try:
        with zstandard.ZstdDecompressor(max_window_size=1 << 27).stream_reader(
            io.BytesIO(data)
        ) as reader:
            while chunk := reader.read(64 * 1024):
                total += len(chunk)
                if total > MAX_DECODED_BYTES:
                    raise ValueError("session exceeds the 128 MiB decoded-input limit")
    except zstandard.ZstdError as error:
        raise ValueError("cannot safely decode compressed session") from error


def _arguments_hash(arguments: str) -> tuple[str, str]:
    try:
        normalized = json.dumps(
            json.loads(arguments), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        encoding = "canonical_json"
    except (ValueError, RecursionError):
        normalized = arguments
        encoding = "raw_invalid_json"
    return hashlib.sha256(normalized.encode()).hexdigest(), encoding


def _reference(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: entry[key] for key in ("seq", "block_index", "call_id")}


def _truncation_clues(text: str, flagged: bool) -> list[str]:
    clues = ["event_flag"] if flagged else []
    if re.search(r"\[Output truncated\. Continue with offset=\d+\.\]$", text):
        clues.append("read_continuation_notice")
    if (
        "[Output truncated and the command was stopped after reaching the model-output budget"
        in text
        or "[Output truncated; showing trailing output." in text
    ):
        clues.append("bash_output_notice")
    if "... [line exceeded 500 bytes]" in text:
        clues.append("bash_line_notice")
    return clues


def diagnose_session(path: Path) -> dict[str, Any]:
    """Summarize the readable prefix without modifying it or running recorded commands.

    An identical invocation is only a review candidate. Even with no intervening recorded
    mutation, files may have changed outside Ava. All tools except the built-in ``read`` are
    conservatively considered potentially mutating; failure flags do not prove no side effects.
    """
    path = path.resolve()
    data = _read_bounded(path)
    _check_decoded_size(data, path)
    log = Log.open(path, OpenMode.read_only)
    try:
        events = log.loaded_events
    finally:
        log.close()
    if _read_bounded(path) != data:
        raise ValueError("session changed during diagnosis; inspect a stable copy")
    if len(events) > MAX_EVENTS:
        raise ValueError("session exceeds the 100000-event diagnostic limit")
    header = events[0].payload
    assert isinstance(header, SessionStart)

    calls: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    repeats: list[dict[str, Any]] = []
    pending: dict[str, list[dict[str, Any]]] = defaultdict(list)
    last_identical: dict[tuple[str, str], int] = {}
    last_potential_mutation = -1
    tool_totals: dict[str, dict[str, int]] = defaultdict(
        lambda: {"calls": 0, "results": 0, "error_results": 0, "result_characters": 0}
    )

    for event in events:
        payload = event.payload
        if isinstance(payload, AssistantMessage):
            for block_index, block in enumerate(payload.item.blocks):
                if block.kind != ContentBlockKind.tool_call:
                    continue
                if len(calls) >= MAX_TOOL_CALLS:
                    raise ValueError("session exceeds the 10000-tool-call diagnostic limit")
                digest, encoding = _arguments_hash(block.arguments_json)
                call = {
                    "seq": event.seq,
                    "block_index": block_index,
                    "call_id": block.call_id,
                    "tool_name": block.tool_name,
                    "arguments_sha256": digest,
                    "arguments_encoding": encoding,
                    "result_references": [],
                }
                signature = (block.tool_name, digest)
                previous = last_identical.get(signature)
                if previous is not None:
                    # Include the previous call itself: repeating a write or shell command
                    # cannot assume that its own first invocation left the world unchanged.
                    mutation = last_potential_mutation >= previous
                    repeats.append(
                        {
                            "tool_name": block.tool_name,
                            "arguments_sha256": digest,
                            "previous": _reference(calls[previous]),
                            "current": _reference(call),
                            "other_calls_between": len(calls) - previous - 1,
                            "recorded_potential_mutation_since_previous": mutation,
                            "review_hint": (
                                "state_may_have_changed" if mutation else "inspect_repeat_reason"
                            ),
                        }
                    )
                last_identical[signature] = len(calls)
                if block.tool_name != "read":
                    last_potential_mutation = len(calls)
                calls.append(call)
                pending[block.call_id].append(call)
                tool_totals[block.tool_name]["calls"] += 1
        elif isinstance(payload, ToolResult):
            for block_index, block in enumerate(payload.item.blocks):
                if block.kind != ContentBlockKind.tool_result:
                    continue
                if len(results) >= MAX_TOOL_CALLS:
                    raise ValueError("session exceeds the 10000-tool-result diagnostic limit")
                candidates = pending.get(block.call_id, [])
                matching_call = candidates.pop(0) if candidates else None
                result = {
                    "seq": event.seq,
                    "block_index": block_index,
                    "call_id": block.call_id,
                    "call_reference": _reference(matching_call) if matching_call else None,
                    "tool_name": matching_call["tool_name"] if matching_call else None,
                    "characters": len(block.text),
                    "text_sha256": hashlib.sha256(block.text.encode()).hexdigest(),
                    "is_error": block.is_error,
                    "origin": block.origin.value,
                    "truncation_clues": _truncation_clues(block.text, payload.truncated),
                }
                results.append(result)
                if matching_call is not None:
                    matching_call["result_references"].append(_reference(result))
                    total = tool_totals[matching_call["tool_name"]]
                    total["results"] += 1
                    total["error_results"] += int(block.is_error)
                    total["result_characters"] += len(block.text)

    missing = [_reference(call) for call in calls if not call["result_references"]]
    errors = [result for result in results if result["is_error"]]
    truncations = [result for result in results if result["truncation_clues"]]
    context = Session(list(events)).model_context()
    final_visible = sum(
        len(block.text)
        for item in context.items
        for block in item.blocks
        if block.kind == ContentBlockKind.tool_result
    )
    return {
        "schema_version": 1,
        "session": {
            "id": header.id,
            "path": str(path),
            "sha256": hashlib.sha256(data).hexdigest(),
            "last_sequence": events[-1].seq,
            "read_mode": "readable_prefix_without_repair",
        },
        "summary": {
            "events": len(events),
            "tool_calls": len(calls),
            "tool_results": len(results),
            "error_results": len(errors),
            "execution_error_signals": sum(e["origin"] == Origin.none.value for e in errors),
            "missing_results": len(missing),
            "unmatched_results": sum(r["call_reference"] is None for r in results),
            "repeat_candidates": len(repeats),
            "results_with_truncation_clues": len(truncations),
            "recorded_result_characters": sum(r["characters"] for r in results),
            "final_context_tool_result_characters": final_visible,
            "cumulative_request_tool_result_characters": None,
            "task_correctness": None,
        },
        "per_tool": dict(sorted(tool_totals.items())),
        "calls": calls[:MAX_EVIDENCE],
        "results": results[:MAX_EVIDENCE],
        "repeat_candidates": repeats[:MAX_EVIDENCE],
        "error_results": errors[:MAX_EVIDENCE],
        "truncation_evidence": truncations[:MAX_EVIDENCE],
        "missing_result_evidence": missing[:MAX_EVIDENCE],
        "evidence_limit_per_list": MAX_EVIDENCE,
        "evidence_lists_truncated": {
            name: len(values) > MAX_EVIDENCE
            for name, values in {
                "calls": calls,
                "results": results,
                "repeat_candidates": repeats,
                "error_results": errors,
                "truncation_evidence": truncations,
                "missing_result_evidence": missing,
            }.items()
        },
        "interpretation": [
            "Sizes are Unicode character counts, not tokens, cost, or measures of usefulness.",
            "Repeats and error flags are review signals; successful recovery may require both.",
            "Potential mutations include bash, edit, write, and unknown tools, even on errors.",
            "Absence of recorded mutation cannot rule out external changes or deliberate checks.",
            "Text truncation markers are clues and can also occur literally in file contents.",
            "Final context uses Session.model_context; it may never have been sent to a model.",
            "Cumulative request volume is unknown: exact request-start snapshots are not logged.",
            "A readable prefix does not certify physical integrity or completion of a session.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument(
        "--output", type=Path, required=True, help="new JSON report; never overwrite"
    )
    args = parser.parse_args()
    try:
        report = diagnose_session(args.session)
        with args.output.open("x", encoding="utf-8") as destination:
            json.dump(report, destination, indent=2, ensure_ascii=False)
            destination.write("\n")
    except (OSError, ValueError, AvaError) as error:
        # AvaError.detail can contain source data; do not print it in a diagnostic failure.
        message = error.message if isinstance(error, AvaError) else str(error)
        parser.exit(2, f"diagnosis failed: {message}\n")
    print(json.dumps({"output": str(args.output), "summary": report["summary"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
