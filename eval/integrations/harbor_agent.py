"""Run the selected Ava wheel inside Harbor's isolated task environment."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from eval.integrations.agent_config import ava_arguments, ava_usage, model_parts
from eval.integrations.codex_auth import codex_environment

ROOT = Path(__file__).resolve().parents[1]


class AvaAgent(BaseInstalledAgent):
    def __init__(
        self, *args: Any, effort: str = "off", compaction: bool = False,
        wheel_path: str | None = None, system_prompt_path: str | None = None,
        record_io: bool = False, **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.provider, self.model = model_parts(self.model_name, effort, mock=True)
        if type(compaction) is not bool or type(record_io) is not bool:
            raise ValueError("compaction and record_io must be JSON booleans")
        self.effort, self.compaction, self.record_io = effort, compaction, record_io
        self.wheel = Path(
            wheel_path or os.environ.get("AVA_EVAL_WHEEL", "")
            or ROOT / "cache/wheels/ava-0.1.0-py3-none-any.whl"
        ).resolve()
        self.system_prompt = Path(system_prompt_path).resolve() if system_prompt_path else None

    @staticmethod
    def name() -> str:
        return "ava"

    def version(self) -> str:
        return "0.1.0"

    async def install(self, environment: BaseEnvironment) -> None:
        if not self.wheel.is_file():
            raise ValueError("Build Ava with: uv build --wheel --out-dir eval/cache/wheels")
        self._wheel_sha256 = hashlib.sha256(self.wheel.read_bytes()).hexdigest()
        self._prompt_sha256 = None
        if self.system_prompt:
            content = self.system_prompt.read_text(encoding="utf-8")
            if not content.strip():
                raise ValueError("The candidate system prompt must not be empty")
            self._prompt_sha256 = hashlib.sha256(content.encode()).hexdigest()
            await self._upload_config_text(
                environment, content=content, remote_path="/tmp/ava-system-prompt.txt",
                filename="system-prompt.txt",
            )
        await environment.upload_file(self.wheel, "/tmp/ava-0.1.0-py3-none-any.whl")
        await environment.upload_file(ROOT / "constraints.txt", "/tmp/ava-constraints.txt")
        await environment.upload_file(ROOT / "integrations/install.sh", "/tmp/ava-install.sh")
        await self.exec_as_root(environment, command="sh /tmp/ava-install.sh", timeout_sec=1800)

    @with_prompt_template
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext,
    ) -> None:
        async with AsyncExitStack() as stack:
            remote = str(self.environment_logs_dir)
            env = {"AVA_HOME": f"{remote}/ava-home"}
            if self.provider == "codex":
                env.update(await stack.enter_async_context(codex_environment(self, environment, kind="ava")))
            elif self.provider == "mock":
                await self._upload_config_text(
                    environment, content="text Ava benchmark plumbing smoke test.\ndone\n",
                    remote_path="/tmp/ava-mock.txt", filename="mock.txt",
                )
                env["AVA_MOCK_SCRIPT"] = "/tmp/ava-mock.txt"
            else:
                key = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}[self.provider]
                if not os.environ.get(key):
                    raise ValueError(f"{key} is required for a live benchmark run")
                env[key] = os.environ[key]
            await self._upload_config_text(
                environment, content=instruction, remote_path="/tmp/ava-instruction.txt",
                filename="instruction.txt",
            )
            args = ava_arguments(
                self.provider, self.model, remote, effort=self.effort, compaction=self.compaction,
                record_io=self.record_io, system_prompt=self.system_prompt is not None,
            )
            context.metadata = {
                "wheel_sha256": self._wheel_sha256, "system_prompt_sha256": self._prompt_sha256,
                "mode": "smoke" if self.provider == "mock" else "live",
                "effort": self.effort, "compaction": self.compaction,
                "record_io": self.record_io,
                "authentication": "codex-oauth" if self.provider == "codex" else self.provider,
                "project_instructions": True,
                "agent_failed": False,
                "provider_api": {"openai": "chat-completions", "anthropic": "messages", "codex": "codex-responses"}.get(self.provider),
                "tools": ["read", "edit", "write", "bash"],
            }
            failure = None
            try:
                await self.exec_as_agent(
                    environment, command=shlex.join(args) + " < /tmp/ava-instruction.txt", env=env,
                )
            except BaseException as error:
                failure = error
                context.metadata["agent_failed"] = True
                raise
            finally:
                try:
                    result = await environment.exec(command=shlex.join([
                        "/opt/ava-venv/bin/python", "-I", "-m", "ava.app.cli",
                        "session", "inspect", f"{remote}/session.jsonl.zst",
                    ]))
                    if result.return_code != 0 or not result.stdout:
                        raise ValueError("Ava did not leave a readable session summary")
                    summary = json.loads(result.stdout)
                    (self.logs_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
                    usage = ava_usage(summary)
                    for field in ("n_input_tokens", "n_output_tokens", "n_cache_tokens", "cost_usd"):
                        setattr(context, field, usage[field])
                    context.metadata.update(session=summary, usage_accounting=usage["usage_accounting"])
                    context.metadata["cache_write_tokens"] = (
                        0 if self.provider in {"openai", "codex"} else summary["tokens"].get("cache_write")
                    )
                except Exception as error:
                    context.metadata["harvest_error"] = str(error)
                    if failure is None:
                        raise
