"""Prompt assembly: verified environment facts, AGENTS.md, and the skills catalog."""

from __future__ import annotations

from pathlib import Path

import pytest

from ava.agent.prompt import discover_skills, make_system_prompt


def _skill(root: Path, name: str, description: str) -> None:
    (root / name).mkdir(parents=True)
    (root / name / "SKILL.md").write_text(f"---\ndescription: {description}\n---\n# {name}\n")


def test_skills_are_discovered_with_project_shadowing_global(
    home: Path, project: Path, tmp_path: Path
):
    _skill(project / ".agents/skills", "deploy", "Ship the thing")
    _skill(project / ".agents/skills", "Bad_Name", "ignored: invalid name")
    user_home = Path(__import__("os").environ["HOME"])
    _skill(user_home / ".codex/skills", "deploy", "global version, shadowed")
    _skill(user_home / ".codex/skills", "review", "Review a diff")
    skills = discover_skills(project)
    assert [(skill.name, skill.scope, skill.description) for skill in skills] == [
        ("deploy", "project", "Ship the thing"),
        ("review", "global", "Review a diff"),
    ]
    prompt = make_system_prompt(project)
    assert "- deploy [project]: Ship the thing (" in prompt
    assert "- review [global]: Review a diff (" in prompt
    assert "Bad_Name" not in prompt


def test_prompt_states_only_verified_facts(home: Path, project: Path):
    (project / "AGENTS.md").write_text("Always run the tests.\n")
    prompt = make_system_prompt(project, scratchpad=project / "missing-scratch")
    assert f"- Working directory: {project}" in prompt
    assert "- Git repository: no" in prompt
    assert "Scratchpad" not in prompt  # a directory that does not exist is never advertised
    assert "Always run the tests." in prompt
    with pytest.raises(RuntimeError):
        __import__("ava.agent.prompt", fromlist=["_replace_field"])._replace_field(
            "no field", "{{x}}", ""
        )
