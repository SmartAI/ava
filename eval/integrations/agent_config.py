"""Small, dependency-free configuration and usage helpers for benchmark adapters."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, cast

PI_VERSION = "0.85.1"
EFFORTS = {"off", "low", "medium", "high", "xhigh"}


def _nonreasoning_openai(model: str) -> bool:
    return any(model == family or model.startswith(family + "-") for family in ("gpt-4.1", "gpt-4o"))


def model_parts(model_name: str | None, effort: str, *, mock: bool = False) -> tuple[str, str]:
    if not model_name or "/" not in model_name:
        raise ValueError("Specify provider/<exact-model-id>")
    provider, model = model_name.split("/", 1)
    if provider not in ({"openai", "anthropic", "codex", "mock"} if mock else {"openai", "anthropic", "codex"}):
        raise ValueError("Supported benchmark providers: openai, anthropic, codex (Ava also supports mock)")
    if not model or "/" in model or ":" in model or not any(c.isdigit() for c in model):
        raise ValueError("Use an exact model ID, without aliases or thinking-level suffixes")
    if effort not in EFFORTS:
        raise ValueError(f"effort must be one of {sorted(EFFORTS)}")
    if provider in {"anthropic", "mock"} and effort != "off":
        raise ValueError("The common Ava/Pi Anthropic baseline currently requires effort='off'")
    if provider == "openai" and _nonreasoning_openai(model) and effort != "off":
        raise ValueError("Non-reasoning OpenAI models require effort='off'")
    return provider, model


def ava_arguments(
    provider: str, model: str, remote: str, *, effort: str, compaction: bool,
    record_io: bool = False, system_prompt: bool = False,
) -> list[str]:
    args = [
        "/opt/ava-venv/bin/python", "-I", "-m", "ava.app.cli",
        "-p", "--provider", provider, "--model", model,
        "--session", f"{remote}/session.jsonl.zst",
    ]
    # The non-reasoning OpenAI model families reject a reasoning_effort argument,
    # including 'none'; omitting it is their explicit off configuration.
    if provider == "codex" or (provider == "openai" and not _nonreasoning_openai(model)):
        args += ["--effort", "none" if effort == "off" else effort]
    if not compaction:
        args.append("--no-compact")
    if record_io:
        args += ["--record", f"{remote}/recording.jsonl"]
    if system_prompt:
        args += ["--system-prompt-file", "/tmp/ava-system-prompt.txt"]
    return args


def pi_arguments(provider: str, model: str, remote: str, *, effort: str) -> list[str]:
    return [
        "/opt/pi-node/bin/node", "/opt/pi/node_modules/@earendil-works/pi-coding-agent/dist/cli.js",
        "--print", "--provider", "openai-codex" if provider == "codex" else provider,
        "--model", model, "--thinking", effort,
        "--tools", "read,edit,write,bash", "--session", f"{remote}/session.jsonl",
        "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes",
        "--no-approve", "--offline",
    ]


def _sum_known(values: list[Any]) -> int | float | None:
    if not values or any(type(value) not in (int, float) or value < 0 for value in values):
        return None
    return sum(cast(list[int | float], values))


def ava_usage(summary: dict[str, Any]) -> dict[str, Any]:
    tokens = summary["tokens"]
    # cache_write_1h is a subset of cache_write, never an additional amount.
    openai = summary["provider"] in {"openai", "codex"}
    input_parts = [tokens.get(key) for key in ("input", "cached_read")]
    if not openai:
        input_parts.append(tokens.get("cache_write"))
    output_parts = [tokens.get("output")]
    if openai:
        output_parts.append(tokens.get("reasoning"))
    inclusive = summary.get("inclusive_tokens", {})
    return {
        "n_input_tokens": inclusive.get("input", _sum_known(input_parts)),
        "n_output_tokens": inclusive.get("output", _sum_known(output_parts)),
        "n_cache_tokens": tokens.get("cached_read"),
        "cost_usd": None,
        "usage_accounting": {
            "input": "inclusive per-attempt total; native OpenAI has no separate cache-creation charge",
            "output": "inclusive per-attempt total; raw optional breakdowns may still be unknown",
            "cost": "unavailable; Ava does not report billed cost",
            "raw_tokens": tokens,
        },
    }


def pi_session_summary(text: str, *, provider: str, model: str, effort: str) -> dict[str, Any]:
    records = [json.loads(line) for line in text.splitlines() if line.strip()]
    usages: list[dict[str, Any]] = []
    assistants: list[dict[str, Any]] = []
    calls: Counter[str] = Counter()
    errors = compactions = 0
    for record in records:
        if record.get("type") == "model_change":
            if (record.get("provider"), record.get("modelId")) != (provider, model):
                raise ValueError("Pi selected a different model from the requested exact model ID")
        elif record.get("type") == "thinking_level_change":
            if record.get("thinkingLevel") != effort:
                raise ValueError("Pi silently changed the requested reasoning effort")
        elif record.get("type") in {"compaction", "branch_summary"}:
            compactions += 1
            usages.append(record.get("usage") or {})
        elif record.get("type") == "message":
            message = record["message"]
            if message.get("role") == "assistant":
                if (message.get("provider"), message.get("model")) != (provider, model):
                    raise ValueError("Pi assistant response used a different model")
                assistants.append(message)
                usages.append(message.get("usage") or {})
                calls.update(
                    block["name"] for block in message.get("content", [])
                    if block.get("type") == "toolCall"
                )
            elif message.get("role") == "toolResult":
                errors += bool(message.get("isError"))
    totals = {
        name: _sum_known([usage.get(name) for usage in usages])
        for name in ("input", "output", "cacheRead", "cacheWrite")
    }
    final_reason = assistants[-1].get("stopReason") if assistants else None
    return {
        "n_input_tokens": _sum_known([totals[k] for k in ("input", "cacheRead", "cacheWrite")]),
        "n_output_tokens": totals["output"],
        "n_cache_tokens": totals["cacheRead"],
        # Pi pricing is bundled catalog arithmetic, never an invoice or billed cost.
        "cost_usd": _sum_known([(usage.get("cost") or {}).get("total") for usage in usages]),
        "usage_accounting": {
            "input": "uncached + cache-read + cache-write; includes compaction usage",
            "output": "Pi provider-normalized output, including reasoning where reported",
            "cost": "estimate from pinned Pi model catalog, not billed cost",
            "raw_tokens": totals,
        },
        "model_attempts": len(assistants), "tool_calls": dict(sorted(calls.items())),
        "tool_errors": errors, "compactions": compactions,
        "last_turn_reason": final_reason,
        "completed": final_reason in {"stop", "length"},
    }
