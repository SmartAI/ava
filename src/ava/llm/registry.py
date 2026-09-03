"""Provider creation from settings or programmatic options, plus the mock provider."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from ava.base import AvaError, CancelToken, ErrorKind
from ava.base.cancel import NEVER
from ava.llm.anthropic import AnthropicProvider, AnthropicSettings
from ava.llm.codex import CodexProvider, load_codex_credential
from ava.llm.configuration import (
    CODEX_BASE_URL,
    ProviderSettings,
    load_provider_settings,
    normalized_base_url,
)
from ava.llm.credentials import stored_api_key
from ava.llm.openai import OpenAIProvider
from ava.llm.provider import (
    ModelProfile,
    Provider,
    Selection,
    SelectionOverride,
    StopReason,
    StreamEvent,
    StreamEventKind,
    StreamSink,
    is_model_alias_candidate,
)
from ava.llm.types import Context

MOCK_CONTEXT_WINDOW = 200_000
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PROVIDER_ID = re.compile(r"^[a-z][a-z0-9-]*$")


class AuthRequirement(StrEnum):
    required = "required"
    allow_missing = "allow_missing"


class ProviderFamily(StrEnum):
    anthropic = "anthropic"
    openai = "openai"
    codex = "codex"


@dataclass(slots=True)
class ProviderOptions:
    provider: str
    family: ProviderFamily | None = None
    model: str | None = None
    effort: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    models: list[ModelProfile] = field(default_factory=list)


class MockProvider(Provider):
    """A first-class provider replaying a scripted stream: ``text <delta>`` lines and ``done``."""

    def __init__(self, selection: Selection, script: Path) -> None:
        super().__init__(selection)
        self.id = "mock"
        self.display_name = "Mock"
        self.context_window = MOCK_CONTEXT_WINDOW
        self._script = script

    async def stream(
        self, context: Context, selected: Selection, sink: StreamSink, cancel: CancelToken = NEVER
    ) -> StopReason:
        try:
            lines = self._script.read_text(encoding="utf-8").splitlines()
        except OSError:
            raise AvaError(
                ErrorKind.not_found, f"cannot open mock provider script '{self._script}'"
            ) from None
        for line in lines:
            if line == "done":
                sink(StreamEvent(kind=StreamEventKind.done))
                return StopReason.end_turn
            if line.startswith("text "):
                sink(StreamEvent(kind=StreamEventKind.text_delta, text=line[5:]))
                continue
            raise AvaError(
                ErrorKind.parse, "invalid mock provider event; expected 'text <delta>' or 'done'"
            )
        raise AvaError(ErrorKind.parse, "mock provider script ended without a done event")


def _provider_from_settings(
    settings: ProviderSettings,
    auth_requirement: AuthRequirement,
    remembers_selection: bool,
    explicit_api_key: str | None,
    api_key_required: bool,
) -> Provider:
    def finish(provider: Provider) -> Provider:
        provider.selection_model_may_be_alias = settings.model_may_be_alias
        provider.model_aliases = settings.aliases
        provider.model_overrides = settings.model_overrides
        provider.remembers_selection = remembers_selection
        if provider.model_overrides:
            provider.context_window = (
                provider.capabilities(provider.selection.model).context_window_tokens or 0
            )
        return provider

    if settings.family == "mock":
        script = os.environ.get("AVA_MOCK_SCRIPT")
        if not script:
            raise AvaError(
                ErrorKind.invalid_argument, "AVA_MOCK_SCRIPT is required when AVA_PROVIDER=mock"
            )
        return finish(MockProvider(settings.selection, Path(script)))
    if settings.family == "codex":
        # Read-only reuse of the Codex CLI's ChatGPT credential; Ava never logs in or refreshes.
        return finish(CodexProvider(settings.selection, settings.base_url, load_codex_credential()))
    api_key = explicit_api_key or ""
    if not api_key and settings.api_key_env:
        if not _ENV_NAME.match(settings.api_key_env):
            raise AvaError(
                ErrorKind.invalid_argument,
                f"configuration field 'providers.{settings.selection.provider}.api_key_env' must name an "
                "environment variable, not contain a secret",
            )
        api_key = os.environ.get(settings.api_key_env) or ""
    if not api_key:
        api_key = stored_api_key(settings.selection.provider) or ""
    if (
        not api_key
        and (settings.api_key_env or api_key_required)
        and auth_requirement == AuthRequirement.required
    ):
        raise AvaError(
            ErrorKind.auth,
            f"no API key is available for provider '{settings.selection.provider}'; provide one directly, "
            "through its configured environment variable, or through Ava credential storage",
        )
    if settings.family == "openai":
        return finish(OpenAIProvider(settings.selection, settings.base_url, api_key))
    return finish(
        AnthropicProvider(
            settings.selection,
            AnthropicSettings(
                base_url=settings.base_url, api_key_env=settings.api_key_env or "ANTHROPIC_API_KEY"
            ),
            api_key,
        )
    )


def provider_from_environment(
    cli: SelectionOverride | None = None,
    resumed: Selection | None = None,
    auth_requirement: AuthRequirement = AuthRequirement.required,
) -> Provider:
    settings = load_provider_settings(cli or SelectionOverride(), resumed)
    api_key_required = settings.selection.provider != "llamacpp"
    return _provider_from_settings(settings, auth_requirement, True, None, api_key_required)


def create_provider(
    options: ProviderOptions, auth_requirement: AuthRequirement = AuthRequirement.required
) -> Provider:
    """Programmatic construction with built-in contracts and fixed credential names."""
    if not _PROVIDER_ID.match(options.provider):
        raise AvaError(
            ErrorKind.invalid_argument,
            "provider must start with a lowercase letter and contain only lowercase letters, digits, or '-'",
        )
    built_in = options.provider in ("anthropic", "openai", "deepseek", "codex")
    family = options.family
    if family is None:
        family = {
            "anthropic": ProviderFamily.anthropic,
            "openai": ProviderFamily.openai,
            "deepseek": ProviderFamily.openai,
            "codex": ProviderFamily.codex,
        }.get(options.provider)
        if family is None:
            raise AvaError(
                ErrorKind.invalid_argument,
                "a custom provider must specify its Anthropic or OpenAI protocol family",
            )
    if (family == ProviderFamily.codex) != (options.provider == "codex"):
        raise AvaError(
            ErrorKind.invalid_argument,
            "Codex OAuth is available only through the built-in 'codex' provider",
        )
    if (options.provider == "anthropic" and family != ProviderFamily.anthropic) or (
        options.provider in ("openai", "deepseek") and family != ProviderFamily.openai
    ):
        raise AvaError(
            ErrorKind.invalid_argument,
            "a built-in provider cannot select a different protocol family",
        )
    if options.model == "" or options.effort == "" or options.api_key == "":
        raise AvaError(
            ErrorKind.invalid_argument,
            "programmatic model, effort, and API key values must be non-empty when provided",
        )
    seen: set[str] = set()
    for profile in options.models:
        caps = profile.capabilities
        invalid = (
            not profile.id
            or (caps.context_window_tokens is not None and caps.context_window_tokens <= 0)
            or (caps.effort_values is not None and any(not effort for effort in caps.effort_values))
        )
        if invalid or profile.id in seen:
            raise AvaError(
                ErrorKind.invalid_argument,
                "model profiles require unique non-empty ids, non-empty effort values, and positive context windows",
            )
        seen.add(profile.id)
    if not built_in and (options.model is None or options.base_url is None):
        raise AvaError(
            ErrorKind.invalid_argument, "a custom provider must specify both model and base_url"
        )
    if family == ProviderFamily.codex:
        if options.base_url is not None:
            raise AvaError(
                ErrorKind.invalid_argument,
                "the built-in 'codex' provider does not accept a configurable base_url",
            )
        if options.api_key is not None:
            raise AvaError(
                ErrorKind.invalid_argument,
                "the built-in 'codex' provider accepts only Codex CLI OAuth credentials",
            )
    default_model = (
        "claude-sonnet-5"
        if family == ProviderFamily.anthropic
        else "deepseek-v4-flash"
        if options.provider == "deepseek"
        else "gpt-5.4"
        if family == ProviderFamily.openai
        else "default"
    )
    settings = ProviderSettings(
        selection=Selection(options.provider, options.model or default_model, options.effort),
        family=family.value,
        model_may_be_alias=is_model_alias_candidate(options.model)
        if options.model
        else family == ProviderFamily.codex,
    )
    for profile in options.models:
        settings.model_overrides[profile.id] = profile.capabilities
    settings.api_key_env = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
    }.get(options.provider)
    if family == ProviderFamily.codex:
        settings.base_url = CODEX_BASE_URL
    else:
        default_url = (
            "https://api.anthropic.com"
            if family == ProviderFamily.anthropic
            else "https://api.deepseek.com"
            if options.provider == "deepseek"
            else "https://api.openai.com/v1"
        )
        settings.base_url = normalized_base_url(options.base_url or default_url)
    return _provider_from_settings(settings, auth_requirement, False, options.api_key, not built_in)
