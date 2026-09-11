"""Credential readiness and model catalogs shared by the desktop and web pickers."""

from __future__ import annotations

import asyncio
import os
from dataclasses import asdict
from typing import Any

from ava.base import AvaError, ErrorKind
from ava.llm.configuration import (
    BUILTIN_PROVIDERS,
    load_provider_settings,
    load_saved_provider_settings,
    read_configuration,
)
from ava.llm.credentials import stored_api_key
from ava.llm.provider import Provider, Selection, SelectionOverride, sort_model_ids
from ava.llm.registry import provider_from_environment

PROVIDER_LABELS = {
    "anthropic": "Anthropic", "openai": "OpenAI", "deepseek": "DeepSeek",
    "codex": "Codex", "llamacpp": "llama.cpp",
}


def provider_names() -> list[str]:
    configured = (read_configuration() or {}).get("providers", {})
    if not isinstance(configured, dict):
        raise AvaError(ErrorKind.parse, "configuration field 'providers' must be an object")
    return list(dict.fromkeys([*BUILTIN_PROVIDERS, *configured]))


def credential_environment() -> dict[str, str]:
    """The environment variable each configured provider reads for its API key."""
    configured = (read_configuration() or {}).get("providers", {})
    if not isinstance(configured, dict):
        raise AvaError(ErrorKind.parse, "configuration field 'providers' must be an object")
    names = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
    }
    for provider, entry in configured.items():
        if not isinstance(entry, dict):
            continue
        api_key_env = entry.get("api_key_env")
        if isinstance(api_key_env, str) and api_key_env:
            names[provider] = api_key_env
    return names


async def open_catalog(name: str) -> tuple[dict[str, Any], Provider | None]:
    """Return a verified catalog and its owned provider, or a safe diagnostic."""
    custom = name not in BUILTIN_PROVIDERS
    entry: dict[str, Any] = {
        "id": name, "label": PROVIDER_LABELS.get(name, name),
        "provider_type": "custom" if custom else "builtin",
        "family": None, "base_url": None, "configured": False,
        "valid": False, "status": "Not configured", "message": "Add an API key to connect.",
        "credential_source": "", "has_stored_key": False, "models": [],
    }
    provider = None
    try:
        # A placeholder is only used to discover the catalog, never for inference.
        settings = load_provider_settings(SelectionOverride(), Selection(name, "default"))
        entry.update(family=settings.family, base_url=settings.base_url)
        stored = stored_api_key(name)
        entry["has_stored_key"] = bool(stored)
        source = "Environment" if settings.api_key_env and os.environ.get(settings.api_key_env) else "Saved key" if stored else ""
        entry["credential_source"] = source
        if not source and name not in ("codex", "llamacpp"):
            return entry, None
        entry["configured"] = True
        provider = provider_from_environment(resumed=Selection(name, "default"))
        provider.remembers_selection = False
        if name in ("codex", "llamacpp"):
            entry["credential_source"] = "Codex CLI login" if name == "codex" else "Local server"
        async with asyncio.timeout(5):
            models = await provider.list_models()
        models = list(dict.fromkeys([*models, *provider.model_overrides, *provider.model_aliases.values()]))
        sort_model_ids(models)
        if not models:
            entry.update(status="No models", message="Connected, but the provider returned no models.")
        else:
            entry.update(
                valid=True, status="Connected", message="Connection verified. Models are available in conversations.",
                models=[{"id": model, "effort_values": provider.capabilities(model).effort_values or []} for model in models],
            )
            return entry, provider
    except TimeoutError:
        entry.update(status="Unavailable", message="Connection check timed out. Try again.")
    except AvaError as error:
        if name == "codex" and error.kind == ErrorKind.auth:
            entry.update(configured=False, status="Sign in required", message="Run codex login in the terminal, then refresh.")
        else:
            entry.update(status="Needs attention" if error.kind == ErrorKind.auth else "Unavailable", message=error.message)
    finally:
        if provider is not None and not entry["valid"]:
            await provider.aclose()
    return entry, None


async def provider_catalog(name: str) -> dict[str, Any]:
    entry, provider = await open_catalog(name)
    if provider is not None:
        await provider.aclose()
    return entry


async def settings_payload() -> dict[str, Any]:
    entries = await asyncio.gather(*(provider_catalog(name) for name in provider_names()))
    entries.sort(key=lambda entry: (not entry["valid"], not entry["configured"]))
    try:
        default_selection = asdict(load_saved_provider_settings().selection)
    except AvaError:
        # The provider cards still render with their own diagnostics.
        default_selection = {}
    for entry in entries:
        entry["is_default"] = bool(default_selection) and entry["id"] == default_selection.get("provider")
    return {
        "providers": entries,
        "default_selection": default_selection,
        "built_in_providers": [
            {"id": name, "label": PROVIDER_LABELS[name]} for name in BUILTIN_PROVIDERS
        ],
    }


async def conversation_catalog(selection: Selection) -> dict[str, Any]:
    payload = await settings_payload()
    available = [entry for entry in payload["providers"] if entry["valid"]]
    current = next((entry for entry in available if entry["id"] == selection.provider), None)
    models = current["models"] if current else []
    profile = next((model for model in models if model["id"] == selection.model), None)
    return {
        **asdict(selection), "providers": available,
        "models": [model["id"] for model in models],
        "effort_values": profile["effort_values"] if profile else [],
        "catalog_available": current is not None,
    }
