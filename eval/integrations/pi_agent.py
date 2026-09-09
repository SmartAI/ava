"""Pinned Pi CLI baseline with only read/edit/write/bash and isolated configuration."""

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

from eval.integrations.agent_config import PI_VERSION, model_parts, pi_arguments, pi_session_summary
from eval.integrations.codex_auth import codex_environment

ASSETS = Path(__file__).with_name("pi")
PREFLIGHT = "/opt/pi/node_modules/@earendil-works/pi-coding-agent/ava-model-info.mjs"


class PiAgent(BaseInstalledAgent):
    def __init__(
        self, *args: Any, effort: str = "off", compaction: bool = False, **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.provider, self.model = model_parts(self.model_name, effort)
        if type(compaction) is not bool:
            raise ValueError("compaction must be a JSON boolean")
        self.effort, self.compaction = effort, compaction

    @staticmethod
    def name() -> str:
        return "pi-baseline"

    def version(self) -> str:
        return PI_VERSION

    async def install(self, environment: BaseEnvironment) -> None:
        self._lock_sha256 = hashlib.sha256((ASSETS / "package-lock.json").read_bytes()).hexdigest()
        for name in ("package.json", "package-lock.json", "install.sh"):
            await environment.upload_file(ASSETS / name, f"/tmp/pi-{name}")
        await self.exec_as_root(environment, command="sh /tmp/pi-install.sh", timeout_sec=600)
        await environment.upload_file(ASSETS / "model-info.mjs", PREFLIGHT)

    @with_prompt_template
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext,
    ) -> None:
        async with AsyncExitStack() as stack:
            remote = str(self.environment_logs_dir)
            env = {
                "PI_CODING_AGENT_DIR": f"{remote}/pi-home", "PI_OFFLINE": "1", "PI_TELEMETRY": "0",
                "PATH": "/opt/pi-node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            }
            native_provider = "openai-codex" if self.provider == "codex" else self.provider
            if self.provider == "codex":
                env.update(await stack.enter_async_context(codex_environment(self, environment, kind="pi")))
            # Do not contact a provider until exact model and effort are known to be supported.
            info = await self.exec_as_agent(environment, command=shlex.join([
                "/opt/pi-node/bin/node", PREFLIGHT, native_provider, self.model, self.effort,
            ]), env=env)
            model_info = json.loads(info.stdout)
            if self.provider != "codex":
                key = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}[self.provider]
                if not os.environ.get(key):
                    raise ValueError(f"{key} is required for a live benchmark run")
                env[key] = os.environ[key]
            await self.exec_as_agent(
                environment, command=shlex.join(["mkdir", "-p", env["PI_CODING_AGENT_DIR"]]),
            )
            await self._upload_config_text(
                environment,
                content=json.dumps({
                    "compaction": {"enabled": self.compaction},
                    "retry": {"enabled": False, "provider": {"maxRetries": 0}},
                    "enableInstallTelemetry": False,
                }),
                remote_path=f"{env['PI_CODING_AGENT_DIR']}/settings.json", filename="settings.json",
            )
            await self._upload_config_text(
                environment, content=instruction, remote_path="/tmp/pi-instruction.txt",
                filename="instruction.txt",
            )
            context.metadata = {
                "package": "@earendil-works/pi-coding-agent", "version": PI_VERSION,
                "lock_sha256": self._lock_sha256, "mode": "live", "model": model_info,
                "effort": self.effort, "compaction": self.compaction,
                "tools": ["read", "edit", "write", "bash"],
                "project_resources": False, "project_instructions": True, "provider_retries": 0,
                "authentication": "codex-oauth" if self.provider == "codex" else self.provider,
                "agent_failed": False,
            }
            failure = None
            try:
                args = pi_arguments(self.provider, self.model, remote, effort=self.effort)
                await self.exec_as_agent(
                    environment, command=shlex.join(args) + " < /tmp/pi-instruction.txt", env=env,
                )
            except BaseException as error:
                failure = error
                context.metadata["agent_failed"] = True
                raise
            finally:
                try:
                    session_path = self.logs_dir / "session.jsonl"
                    await environment.download_file(f"{remote}/session.jsonl", session_path)
                    summary = pi_session_summary(
                        session_path.read_text(encoding="utf-8"), provider=native_provider,
                        model=self.model, effort=self.effort,
                    )
                    (self.logs_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
                    for field in ("n_input_tokens", "n_output_tokens", "n_cache_tokens", "cost_usd"):
                        setattr(context, field, summary[field])
                    context.metadata.update(session=summary, usage_accounting=summary["usage_accounting"])
                    context.metadata["cache_write_tokens"] = summary["usage_accounting"]["raw_tokens"].get("cacheWrite")
                    if not summary["completed"]:
                        context.metadata["agent_failed"] = True
                        raise ValueError(f"Pi did not complete its turn: {summary['last_turn_reason']}")
                except Exception as error:
                    context.metadata["harvest_error"] = str(error)
                    if failure is None:
                        raise
