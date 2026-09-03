"""Settings parsing, selection precedence, provider validation, and selection persistence.

Resolution order: CLI flag > resumed session > environment > settings file > built-in default.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from ava.base import AvaError, ErrorKind, ava_home
from ava.llm.provider import (
    ModelCapabilities,
    Selection,
    SelectionOverride,
    is_model_alias_candidate,
)

CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
BUILTIN_PROVIDERS = ("anthropic", "openai", "deepseek", "codex", "llamacpp")


@dataclass(slots=True)
class ProviderSettings:
    selection: Selection
    family: str
    base_url: str = ""
    api_key_env: str | None = None
    aliases: dict[str, str] = field(default_factory=dict)
    model_overrides: dict[str, ModelCapabilities] = field(default_factory=dict)
    model_may_be_alias: bool = False


@dataclass(slots=True)
class SettingsPath:
    path: Path
    required: bool = False


def settings_path() -> SettingsPath:
    override = os.environ.get("AVA_CONFIG")
    if override:
        return SettingsPath(Path(override), required=True)
    home = ava_home()
    path = home / "settings.json"
    if path.exists() or os.environ.get("AVA_HOME"):
        return SettingsPath(path)
    legacy: list[Path] = []
    user_home = os.environ.get("HOME")
    config_home = os.environ.get("XDG_CONFIG_HOME")
    state_home = os.environ.get("XDG_STATE_HOME")
    if config_home:
        legacy.append(Path(config_home) / "ava/config.json")
    elif user_home:
        legacy.append(Path(user_home) / ".config/ava/config.json")
    if state_home:
        legacy.append(Path(state_home) / "ava/state.json")
    elif user_home:
        legacy.append(Path(user_home) / ".local/state/ava/state.json")
    found = [str(candidate) for candidate in legacy if candidate.exists()]
    if found:
        raise AvaError(
            ErrorKind.invalid_argument,
            "legacy settings require manual migration",
            "merge "
            + ", ".join(f"'{item}'" for item in found)
            + f" into '{path}'; do not copy API keys",
        )
    return SettingsPath(path)


def _validate_optional_string(value: object, location: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value == "":
        raise AvaError(
            ErrorKind.parse, f"configuration field '{location}' must be a non-empty string"
        )
    return value


def read_configuration() -> dict | None:
    resolved = settings_path()
    if not resolved.required and not resolved.path.exists():
        return None
    try:
        text = resolved.path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise AvaError(
            ErrorKind.not_found, f"cannot open settings file '{resolved.path}'"
        ) from None
    except OSError as error:
        raise AvaError(
            ErrorKind.io, f"cannot read settings file '{resolved.path}'", str(error)
        ) from error
    try:
        config = json.loads(text)
    except json.JSONDecodeError as error:
        raise AvaError(
            ErrorKind.parse, f"cannot parse settings file '{resolved.path}': {error}"
        ) from None
    if not isinstance(config, dict):
        raise AvaError(
            ErrorKind.parse, f"cannot parse settings file '{resolved.path}': not an object"
        )
    for name in ("provider", "model", "effort"):
        _validate_optional_string(config.get(name), name)
    return config


@dataclass(slots=True)
class _Flags:
    model_may_be_alias: bool = False
    configured_aliases_allowed: bool = False


def _apply_layer(
    selection: Selection,
    flags: _Flags,
    provider: str | None,
    model: str | None,
    effort: str | None,
    *,
    model_may_be_alias: bool,
    configured_aliases_allowed: bool,
) -> None:
    if provider is not None and provider != selection.provider:
        selection.provider = provider
        selection.model = ""
        selection.effort = None
        flags.model_may_be_alias = False
        flags.configured_aliases_allowed = False
    if model is not None:
        selection.model = model
        flags.model_may_be_alias = model_may_be_alias
        flags.configured_aliases_allowed = configured_aliases_allowed
    if effort is not None:
        selection.effort = effort


def _resolve_selection(
    config: dict | None, cli: SelectionOverride, resumed: Selection | None
) -> tuple[Selection, _Flags]:
    selection = Selection(provider="anthropic", model="claude-sonnet-5")
    flags = _Flags()
    if config is not None:
        model = config.get("model")
        _apply_layer(
            selection,
            flags,
            config.get("provider"),
            model,
            config.get("effort"),
            model_may_be_alias=isinstance(model, str) and is_model_alias_candidate(model),
            configured_aliases_allowed=model is not None,
        )
    env_provider = os.environ.get("AVA_PROVIDER") or None
    env_model = os.environ.get("AVA_MODEL") or None
    env_effort = os.environ.get("AVA_EFFORT") or None
    _apply_layer(
        selection,
        flags,
        env_provider,
        env_model,
        env_effort,
        model_may_be_alias=env_model is not None and is_model_alias_candidate(env_model),
        configured_aliases_allowed=env_model is not None,
    )
    if resumed is not None:
        selection = Selection(resumed.provider, resumed.model, resumed.effort)
        flags = _Flags(
            model_may_be_alias=is_model_alias_candidate(resumed.model),
            configured_aliases_allowed=True,
        )
    _apply_layer(
        selection,
        flags,
        cli.provider,
        cli.model,
        cli.effort,
        model_may_be_alias=cli.model is not None and is_model_alias_candidate(cli.model),
        configured_aliases_allowed=cli.model is not None,
    )
    return selection, flags


def _is_builtin(provider: str) -> bool:
    return provider in BUILTIN_PROVIDERS


def _resolve_provider_config(config: dict | None, selection: Selection) -> dict | None:
    providers = config.get("providers") if config is not None else None
    provider_config: dict | None = None
    if isinstance(providers, dict):
        provider_config = providers.get(selection.provider)
        if provider_config is None and selection.provider != "llamacpp":
            raise AvaError(
                ErrorKind.parse, f"configuration field 'providers.{selection.provider}' is required"
            )
        if provider_config is not None and not isinstance(provider_config, dict):
            raise AvaError(
                ErrorKind.parse,
                f"configuration field 'providers.{selection.provider}' must be an object",
            )
    elif not _is_builtin(selection.provider):
        raise AvaError(
            ErrorKind.parse, f"configuration field 'providers.{selection.provider}' is required"
        )
    if provider_config is None:
        return None
    prefix = f"providers.{selection.provider}"
    for name in ("family", "base_url", "api_key_env"):
        _validate_optional_string(provider_config.get(name), f"{prefix}.{name}")
    if selection.provider == "codex":
        for name in ("family", "base_url", "api_key_env"):
            if provider_config.get(name) is not None:
                raise AvaError(
                    ErrorKind.invalid_argument,
                    f"the built-in 'codex' provider does not accept {'a configurable ' + name if name != 'api_key_env' else name}",
                )
    if not _is_builtin(selection.provider):
        if provider_config.get("family") is None:
            raise AvaError(
                ErrorKind.parse,
                f"configuration field '{prefix}.family' is required for a custom provider",
            )
        if provider_config.get("base_url") is None:
            raise AvaError(
                ErrorKind.parse,
                f"configuration field '{prefix}.base_url' is required for a custom provider",
            )
        if not selection.model:
            raise AvaError(
                ErrorKind.parse,
                f"configuration field 'model' is required when selecting custom provider '{selection.provider}'",
            )
    return provider_config


def _provider_family(selection: Selection, provider_config: dict | None) -> str:
    if provider_config is not None and provider_config.get("family") is not None:
        return str(provider_config["family"])
    if selection.provider in ("llamacpp", "deepseek"):
        return "openai"
    return selection.provider


def _default_settings(selection: Selection, flags: _Flags, family: str) -> ProviderSettings:
    settings = ProviderSettings(
        selection=selection, family=family, model_may_be_alias=flags.model_may_be_alias
    )
    custom = not _is_builtin(selection.provider)
    if family == "anthropic":
        if not selection.model:
            selection.model = "claude-sonnet-5"
            settings.model_may_be_alias = False
            flags.configured_aliases_allowed = False
        if not custom:
            settings.base_url = "https://api.anthropic.com"
            settings.api_key_env = "ANTHROPIC_API_KEY"
    elif family == "openai":
        if not selection.model:
            if selection.provider == "llamacpp":
                selection.model = "default"
                settings.model_may_be_alias = True
            elif selection.provider == "deepseek":
                selection.model = "deepseek-v4-flash"
                settings.model_may_be_alias = False
                flags.configured_aliases_allowed = False
            else:
                selection.model = "gpt-5.4"
                settings.model_may_be_alias = False
                flags.configured_aliases_allowed = False
        if not custom:
            settings.base_url = {
                "llamacpp": "http://127.0.0.1:8081/v1",
                "deepseek": "https://api.deepseek.com",
            }.get(selection.provider, "https://api.openai.com/v1")
        if selection.provider == "openai":
            settings.api_key_env = "OPENAI_API_KEY"
        elif selection.provider == "deepseek":
            settings.api_key_env = "DEEPSEEK_API_KEY"
    elif family == "codex":
        if selection.provider != "codex":
            raise AvaError(
                ErrorKind.invalid_argument,
                "Codex credentials are available only through the built-in 'codex' provider",
            )
        if not selection.model:
            selection.model = "default"
            settings.model_may_be_alias = True
            flags.configured_aliases_allowed = False
        settings.base_url = CODEX_BASE_URL
    else:
        raise AvaError(
            ErrorKind.provider,
            f"provider '{selection.provider}' uses unsupported family '{family}'; this build supports "
            "anthropic, openai, codex, and mock",
        )
    return settings


def _apply_provider_config(settings: ProviderSettings, config: dict, flags: _Flags) -> None:
    if config.get("base_url") is not None:
        settings.base_url = str(config["base_url"])
    if config.get("api_key_env") is not None:
        settings.api_key_env = str(config["api_key_env"])
    aliases = config.get("aliases")
    if aliases is not None:
        if not isinstance(aliases, dict) or any(
            not isinstance(alias, str) or not alias or not isinstance(model, str) or not model
            for alias, model in aliases.items()
        ):
            raise AvaError(
                ErrorKind.parse,
                f"configuration field 'providers.{settings.selection.provider}.aliases' must map "
                "non-empty aliases to concrete model ids",
            )
        settings.aliases = dict(aliases)
        settings.model_may_be_alias = settings.model_may_be_alias or (
            flags.configured_aliases_allowed and settings.selection.model in settings.aliases
        )
    models = config.get("models")
    if models is not None:
        if not isinstance(models, dict):
            raise AvaError(
                ErrorKind.parse,
                f"configuration field 'providers.{settings.selection.provider}.models' must be an object",
            )
        for model, configured in models.items():
            context_window = (
                configured.get("context_window") if isinstance(configured, dict) else None
            )
            effort_values = (
                configured.get("effort_values") if isinstance(configured, dict) else None
            )
            invalid = (
                not model
                or not isinstance(configured, dict)
                or (
                    context_window is not None
                    and (not isinstance(context_window, int) or context_window <= 0)
                )
                or (
                    effort_values is not None
                    and (
                        not isinstance(effort_values, list)
                        or any(
                            not isinstance(effort, str) or not effort for effort in effort_values
                        )
                    )
                )
            )
            if invalid:
                raise AvaError(
                    ErrorKind.parse,
                    f"configuration field 'providers.{settings.selection.provider}.models' requires "
                    "non-empty model ids and effort values, and positive context windows",
                )
            settings.model_overrides[model] = ModelCapabilities(
                context_window_tokens=context_window,
                supports_tools=configured.get("supports_tools"),
                effort_values=list(effort_values) if effort_values is not None else None,
            )


def normalized_base_url(base_url: str) -> str:
    base_url = base_url.rstrip("/")
    secure = base_url.startswith("https://")
    loopback = base_url.startswith("http://127.0.0.1:") or base_url.startswith("http://localhost:")
    if not secure and not loopback:
        raise AvaError(
            ErrorKind.invalid_argument,
            "provider base_url must use HTTPS, except for a loopback test endpoint",
        )
    return base_url


def load_provider_settings(cli: SelectionOverride, resumed: Selection | None) -> ProviderSettings:
    config = read_configuration()
    selection, flags = _resolve_selection(config, cli, resumed)
    if selection.provider == "mock":
        if not selection.model:
            selection.model = "mock"
        return ProviderSettings(
            selection=selection, family="mock", model_may_be_alias=flags.model_may_be_alias
        )
    provider_config = _resolve_provider_config(config, selection)
    family = _provider_family(selection, provider_config)
    settings = _default_settings(selection, flags, family)
    if provider_config is not None:
        _apply_provider_config(settings, provider_config, flags)
    settings.base_url = normalized_base_url(settings.base_url)
    return settings


def remember_selection(selection: Selection) -> None:
    """Update the top-level selection fields atomically, preserving provider definitions."""
    resolved = settings_path()
    if not selection.provider or not selection.model:
        raise AvaError(ErrorKind.io, "cannot remember an incomplete selection")
    path = resolved.path
    document: dict = {}
    exists = path.exists()
    if exists:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise AvaError(
                ErrorKind.parse, f"cannot update invalid settings file '{path}'"
            ) from None
        if not isinstance(document, dict):
            raise AvaError(ErrorKind.parse, f"cannot update invalid settings file '{path}'")
    document["provider"] = selection.provider
    document["model"] = selection.model
    if selection.effort is not None:
        document["effort"] = selection.effort
    else:
        document.pop("effort", None)
    encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    parent = path.parent
    created = not parent.exists()
    try:
        parent.mkdir(parents=True, exist_ok=True)
        if created and not resolved.required:
            os.chmod(parent, 0o700)
    except OSError as error:
        raise AvaError(
            ErrorKind.io, f"cannot create settings directory '{parent}': {error}"
        ) from error
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        with open(temporary, "w", encoding="utf-8") as output:
            output.write(encoded)
        mode = path.stat().st_mode & 0o777 if exists else 0o600
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise AvaError(ErrorKind.io, f"cannot replace settings file '{path}': {error}") from error
