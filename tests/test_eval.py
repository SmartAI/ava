"""Evaluation evidence must fail on wrong artifacts and reject unmatched comparisons."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.run import ROOT, compare, run_case


async def test_grader_checks_the_workspace_not_the_final_message(tmp_path: Path):
    case_path = ROOT / "eval/cases/edit-and-test.json"
    good = await run_case(case_path, tmp_path / "good")
    assert good["correct"] is True
    case = json.loads(case_path.read_text())
    # Keep the same confident final answer, but omit the actual edit.
    case["responses"] = case["responses"][-1:]
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(case))
    bad = await run_case(broken, tmp_path / "bad")
    assert bad["correct"] is False
    assert bad["replay"]["matched"] is True
    assert bad["metrics"]["last_turn_reason"] == "completed"


def test_comparison_rejects_different_cases_and_missing_grades(tmp_path: Path):
    left, right = tmp_path / "left", tmp_path / "right"
    for directory in (left, right):
        (directory / "case").mkdir(parents=True)
    row = {
        "case_id": "case",
        "case_sha256": "same",
        "evaluator_sha256": "same",
        "suite": "ava-regressions",
        "mode": "synthetic-runtime",
        "model": "fixture-v1",
        "compaction": False,
        "correct": True,
    }
    (left / "case/result.json").write_text(json.dumps(row))
    row["correct"] = False
    (right / "case/result.json").write_text(json.dumps(row))
    assert compare(left, right)["regressions"] == ["case"]
    row["correct"] = None
    (right / "case/result.json").write_text(json.dumps(row))
    with pytest.raises(ValueError, match="ungraded"):
        compare(left, right)
    row.update(correct=True, case_sha256="changed")
    (right / "case/result.json").write_text(json.dumps(row))
    with pytest.raises(ValueError, match="case_sha256"):
        compare(left, right)
