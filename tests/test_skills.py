"""Skills reach real agent requests; availability persists without deleting shared files."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from ava.agent import Agent
from ava.agent.skills import create, detail, discover_skills, inventory, set_state
from ava.session import PromptResolved, ToolResult
from tests.conftest import ScriptedProvider, message, text_response, tool_call_response


def test_inventory_yaml_shadowing_removal_and_restart(home, project, tmp_path):
    shared = Path(os.environ["HOME"]) / ".codex/skills/review"
    shared.mkdir(parents=True)
    content = "---\nname: review\ndescription: >-\n  Review shared\n  changes.\n---\n# Shared\n"
    (shared / "SKILL.md").write_text(content)
    own = create(project, "review", "Project checks", "# Checklist\n\n**Check tests**", "project")
    assert [s.description for s in discover_skills(project)] == ["Project checks"]
    set_state(project, own["id"], "disabled")
    assert not discover_skills(project), "Disabled overrides cannot silently enable shared instructions"
    set_state(project, own["id"], "removed")
    assert [s.description for s in discover_skills(project)] == ["Review shared changes."]
    shared_row = next(r for r in inventory(project) if r["source"] == "Shared")
    set_state(project, shared_row["id"], "removed")
    assert (shared / "SKILL.md").read_text() == content
    result = subprocess.run([sys.executable, "-c", "from pathlib import Path; from ava.agent.skills import discover_skills; import sys; print(len(discover_skills(Path(sys.argv[1]))))", str(project)], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "0", "A new process must retain removed and disabled settings"
    set_state(project, own["id"], "enabled")
    assert "**Check tests**" in detail(project, own["id"])["body"]
    other = tmp_path / "other"
    other.mkdir()
    assert not discover_skills(other), "Project skills must stay scoped to their project"
    personal = create(project, "prepare-release", "Package releases", "Run the checks.", "global")
    assert [s.name for s in discover_skills(other)] == [personal["name"]]
    assert create(project, "prepare-release", "Package releases", "Run the checks.", "global") == personal


async def test_skill_changes_apply_at_step_seam_and_keep_tool_results(home, project):
    own = create(project, "review", "Sapphire checklist", "# Checklist\n\nInspect actual changes.", "project")
    provider = ScriptedProvider([tool_call_response("read-skill", "read", json.dumps({"path": own["path"]})), text_response("Checked")])
    provider.gate = asyncio.Event()
    agent = Agent.create(provider, project)
    try:
        await agent.followup(message("Review this project"))
        task = asyncio.create_task(agent.drive())
        await provider.started.wait()
        assert "Sapphire checklist" in provider.contexts[0].system_prompt
        set_state(project, own["id"], "disabled")
        provider.gate.set()
        await task
        assert provider.calls == 2
        assert "Sapphire checklist" not in provider.contexts[1].system_prompt
        results = [event.payload for event in agent.state.session.events if isinstance(event.payload, ToolResult)]
        assert len(results) == 1
        assert "Inspect actual changes" in str(results[0])
        assert len([e for e in agent.state.session.events if isinstance(e.payload, PromptResolved)]) == 2
    finally:
        await agent.aclose()




def test_large_skill_preview_is_bounded_and_catalog_does_not_read_body(home, project):
    import tracemalloc

    folder = project / ".agents/skills/large-notes"
    folder.mkdir(parents=True)
    with (folder / "SKILL.md").open("wb") as source:
        source.write(b"---\nname: large-notes\ndescription: Read only the needed resources.\n---\n# Notes\n")
        for _ in range(64):
            source.write(b"A" * (1024 * 1024))
    tracemalloc.start()
    try:
        rows = inventory(project)
        preview = detail(project, rows[0]["id"])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert rows[0]["effective"] and preview["truncated"]
    assert len(preview["body"].encode()) <= 65536
    assert peak < 2 * 1024 * 1024, "Opening a skill must not read its entire large body into memory"
