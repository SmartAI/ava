"""The context report: every model-visible byte attributed to one kind, summing to the estimate."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from ava.agent import Agent, CompactionOptions
from ava.llm import Item, Role, Usage, make_file_text_block, make_image_block, make_text_block
from ava.session.context_report import context_report
from tests.conftest import ScriptedProvider, message, text_response, tool_call_response

PNG_2X3 = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAIAAAAD")


async def test_report_attributes_every_kind_and_sums_to_the_estimate(home: Path, project: Path):
    (project / "AGENTS.md").write_text("Always run the tests.\n")
    skill = project / ".agents/skills/deploy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\ndescription: Ship it\n---\n")
    provider = ScriptedProvider(
        [
            tool_call_response("c1", "bash", json.dumps({"command": "echo hi"}), text="Running."),
            text_response("done"),
        ]
    )
    agent = Agent.create(provider, project)
    item = Item(
        role=Role.user,
        blocks=[
            make_image_block("shot.png", PNG_2X3, "image/png"),
            make_file_text_block("notes.txt", "alpha beta"),
            make_text_block("look at these"),
        ],
    )
    await agent.followup(item)
    await agent.drive()
    report = context_report(agent.state.session, 10_000, 85)
    kinds = [section.kind for section in report.sections]
    assert kinds == [
        "system",
        "environment",
        "agents_md",
        "skills",
        "tools",
        "user_text",
        "attachment_files",
        "attachment_images",
        "assistant_text",
        "tool_calls",
        "tool_results",
        "framing",
    ]
    by_kind = {section.kind: section for section in report.sections}
    assert by_kind["tools"].count == 4 and by_kind["attachment_images"].bytes == len(PNG_2X3)
    assert by_kind["attachment_images"].tokens >= 1200
    assert by_kind["agents_md"].bytes >= len("Always run the tests.")
    assert "Ship it" not in "" and by_kind["skills"].count == 1
    assert by_kind["framing"].count == 4  # user, assistant, tool, assistant
    assert report.estimated_tokens == sum(section.tokens for section in report.sections)
    assert report.measured_input_tokens is None and not report.compacted
    assert (report.context_window, report.threshold_percent) == (10_000, 85)
    await agent.aclose()


async def test_report_reflects_the_compacted_window(home: Path, project: Path):
    provider = ScriptedProvider(
        [
            text_response("h" * 4000, usage=Usage(input=9000, output=10)),
            text_response("## Goal\n- keep going\n\n## Next Steps\n- more\n"),
            text_response("after", usage=Usage(input=300, cached_read=50)),
        ]
    )
    agent = Agent.create(provider, project, CompactionOptions(threshold_percent=50))
    await agent.followup(message("first " + "x" * 3000))
    await agent.drive()
    await agent.followup(message("second"))
    await agent.drive()
    report = context_report(agent.state.session, provider.context_window, 50)
    by_kind = {section.kind: section for section in report.sections}
    assert report.compacted and "compaction_seed" in by_kind
    # The summarized prefix is gone from the window: only the post-seed exchange remains.
    assert by_kind["user_text"].count == 1 and "h" * 4000 not in ""
    assert report.measured_input_tokens == 350
    await agent.aclose()
