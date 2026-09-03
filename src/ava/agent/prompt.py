"""Verified environment discovery, instruction loading, and system prompt assembly.

Every fact stated in the prompt is verified at assembly time; a fact that cannot be verified is
omitted rather than guessed.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ava.base import find_project_root

MAX_AGENTS_BYTES = 32 * 1024
_SKILL_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


@dataclass(frozen=True, slots=True)
class CommandProbe:
    name: str
    replaces: str = ""


COMMAND_PROBES = (
    CommandProbe("rg", "grep -r"),
    CommandProbe("fd", "find"),
    CommandProbe("jq"),
    CommandProbe("gh"),
    CommandProbe("python3", "python"),
)


def _read_prefix(path: Path, limit: int) -> str | None:
    if not path.is_file():
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data:
        return None
    content = data[: limit + 1].decode("utf-8", "replace")
    if len(data) > limit:
        content = data[:limit].decode("utf-8", "replace") + "\n[truncated]\n"
    return content


class _InstructionAppender:
    def __init__(self) -> None:
        self.remaining = MAX_AGENTS_BYTES
        self.started = False

    def append(self, prompt: list[str], path: Path) -> None:
        if self.remaining == 0:
            return
        content = _read_prefix(path, self.remaining)
        if content is None:
            return
        if not self.started:
            prompt.append(
                "\n\n# Agent instructions\n\nInstructions are ordered from broadest to most "
                "specific. Later instructions take precedence when they conflict.\n"
            )
            self.started = True
        prompt.append(f"\n## {path}\n\n{content}")
        if not content.endswith("\n"):
            prompt.append("\n")
        self.remaining -= min(self.remaining, len(content.encode("utf-8")))


def _append_agent_instructions(prompt: list[str], cwd: Path) -> None:
    appender = _InstructionAppender()
    home = os.environ.get("HOME")
    if home:
        appender.append(prompt, Path(home) / ".codex/AGENTS.md")
    project_root = find_project_root(cwd)
    directories: list[Path] = []
    directory = cwd
    while True:
        directories.append(directory)
        if directory == project_root or directory.parent == directory:
            break
        directory = directory.parent
    appender.remaining = MAX_AGENTS_BYTES
    for directory in reversed(directories):
        appender.append(prompt, directory / "AGENTS.md")


def _skill_description(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    if not lines or lines[0].rstrip("\r") != "---":
        return None
    description: str | None = None
    for line in lines[1:]:
        if line.rstrip("\r") == "---":
            return description
        if description is not None or not line.startswith("description:"):
            continue
        value = line[len("description:") :].strip(" \t\r")
        if not value:
            return None
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not value or value in (">", "|"):
            return None
        description = value[:1024]
    return None


def _collect_skills(skills: dict[str, tuple[str, Path, str]], root: Path, scope: str) -> None:
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return
    for entry in entries:
        path = entry / "SKILL.md"
        name = entry.name
        description = _skill_description(path)
        if description and _SKILL_NAME.match(name) and name not in skills:
            skills[name] = (description, path, scope)


def _append_skills(prompt: list[str], project_root: Path) -> None:
    skills: dict[str, tuple[str, Path, str]] = {}
    _collect_skills(skills, project_root / ".agents/skills", "project")
    home = os.environ.get("HOME")
    if home:
        _collect_skills(skills, Path(home) / ".codex/skills", "global")
    if not skills:
        return
    prompt.append(
        "\n\n# Skills\n\nSkills use progressive disclosure. This catalog contains metadata only. "
        "When a task matches a skill, use `read` to load its complete SKILL.md before acting. "
        "Resolve relative references from the directory containing SKILL.md.\n\n"
    )
    for name in sorted(skills):
        description, path, scope = skills[name]
        prompt.append(f"- {name} [{scope}]: {description} ({path})\n")


def _platform_description() -> str:
    system = platform.system()
    release = platform.release()
    return f"{system} {release}".strip()


def load_system_prompt_template() -> str:
    candidates: list[Path] = []
    configured = os.environ.get("AVA_AGENT_SYSTEM_PROMPT_PATH")
    if configured:
        candidates.append(Path(configured))
    candidates.append(Path(__file__).parent / "prompts" / "system.md")
    for candidate in candidates:
        try:
            return candidate.read_text(encoding="utf-8")
        except OSError:
            continue
    raise RuntimeError("cannot load agent system prompt")


def _replace_field(prompt: str, field: str, value: str) -> str:
    if field not in prompt:
        raise RuntimeError("agent system prompt is missing a required field")
    return prompt.replace(field, value, 1)


def today_date() -> str:
    # The local date, not UTC: the model reasons about "today" in the user's terms.
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def make_system_prompt(cwd: Path, scratchpad: Path | None = None) -> str:
    project_root = find_project_root(cwd)
    in_git_repository = (project_root / ".git").exists()
    template = load_system_prompt_template()

    environment = [
        f"- Working directory: {cwd}\n",
        f"- Git repository: {'yes' if in_git_repository else 'no'}\n",
    ]
    description = _platform_description()
    if description:
        environment.append(f"- Platform: {description}\n")
    environment.append(f"- Today's date: {today_date()}\n")
    has_scratchpad = scratchpad is not None and scratchpad.is_dir()
    if has_scratchpad:
        environment.append(f"- Scratchpad directory: {scratchpad}\n")
    available = [probe for probe in COMMAND_PROBES if shutil.which(probe.name)]
    if available:
        line = (
            "- Available command-line tools: "
            + ", ".join(f"`{probe.name}`" for probe in available)
            + "."
        )
        replacements = [probe for probe in available if probe.replaces]
        if replacements:
            phrases = [f"`{probe.name}` to `{probe.replaces}`" for probe in replacements]
            joined = (
                phrases[0] if len(phrases) == 1 else ", ".join(phrases[:-1]) + " and " + phrases[-1]
            )
            line += f" Prefer {joined}."
        environment.append(line + "\n")
    prompt = _replace_field(template, "{{environment}}", "".join(environment))

    scratchpad_section = ""
    if has_scratchpad:
        scratchpad_section = (
            "\n# Scratchpad\n\n"
            "Use the scratchpad directory above for all temporary and intermediate files, "
            "including temporary scripts, generated data, and command output that is not a "
            "project deliverable. Keep deliverables in the working directory. Do not put "
            "disposable files in the project or use `/tmp` directly or another temporary "
            "location unless the user explicitly requests it.\n"
        )
    prompt = _replace_field(prompt, "{{scratchpad}}", scratchpad_section)
    parts = [prompt]
    _append_agent_instructions(parts, cwd)
    _append_skills(parts, project_root)
    return "".join(parts)
