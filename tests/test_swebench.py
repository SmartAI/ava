"""Public adapter contracts with local manifests and a synthetic dataset loader."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from eval.integrations import swebench


def test_manifest_selection_has_an_independent_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls = []

    def load_dataset(dataset: str, *, split: str, revision: str) -> list[dict[str, str]]:
        calls.append((dataset, split, revision))
        return [{"instance_id": "example-a"}, {"instance_id": "example-b"}]

    module = ModuleType("datasets")
    monkeypatch.setattr(module, "load_dataset", load_dataset, raising=False)
    monkeypatch.setitem(sys.modules, "datasets", module)
    cache = tmp_path / "cache"
    for task_id in ("example-a", "example-b"):
        manifest = tmp_path / f"{task_id}.json"
        manifest.write_text(json.dumps({
            "dataset": "synthetic/example", "revision": "same-revision", "split": "test",
            "tasks": [{"id": task_id}],
        }))
        assert swebench.prepare(manifest, cache) == [{"instance_id": task_id}]
        assert swebench.prepare(manifest, cache) == [{"instance_id": task_id}]
    assert calls == [("synthetic/example", "test", "same-revision")] * 2
    assert len(list(cache.glob("*.json"))) == 2


def test_cli_requires_explicit_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cache = tmp_path / "cache"
    monkeypatch.setattr(sys, "argv", ["swebench", "prepare", "--cache", str(cache)])
    with pytest.raises(SystemExit) as error:
        swebench.main()
    assert error.value.code == 2
    assert not cache.exists()
