"""The provider seam: normalized stream events, selection, capabilities, and alias resolution."""

from __future__ import annotations

import base64
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from ava.base import AvaError, CancelToken, ErrorKind, ascii_lower
from ava.base.cancel import NEVER
from ava.llm.types import ContentBlock, Context, ToolParamType


class StreamEventKind(StrEnum):
    text_delta = "text_delta"
    reasoning_item = "reasoning_item"
    tool_call_start = "tool_call_start"
    tool_call_delta = "tool_call_delta"
    tool_call_end = "tool_call_end"
    usage = "usage"
    done = "done"


class StopReason(StrEnum):
    end_turn = "end_turn"
    max_tokens = "max_tokens"
    tool_use = "tool_use"


@dataclass(slots=True)
class Usage:
    """Per-category optionals keep an unreported measurement distinct from a measured zero."""

    input: int | None = None
    cached_read: int | None = None
    cache_write: int | None = None
    cache_write_1h: int | None = None
    output: int | None = None
    reasoning: int | None = None

    def any(self) -> bool:
        return any(
            value is not None
            for value in (
                self.input,
                self.cached_read,
                self.cache_write,
                self.cache_write_1h,
                self.output,
                self.reasoning,
            )
        )


@dataclass(slots=True)
class StreamEvent:
    kind: StreamEventKind
    text: str = ""
    id: str = ""
    name: str = ""
    summary: str = ""
    usage: Usage = field(default_factory=Usage)


StreamSink = Callable[[StreamEvent], None]


@dataclass(slots=True)
class Selection:
    provider: str
    model: str
    effort: str | None = None


@dataclass(slots=True)
class SelectionOverride:
    provider: str | None = None
    model: str | None = None
    effort: str | None = None


@dataclass(slots=True)
class ModelCapabilities:
    context_window_tokens: int | None = None
    supports_tools: bool | None = None
    effort_values: list[str] | None = None


@dataclass(slots=True)
class ModelProfile:
    id: str
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)


def merge_model_capabilities(
    base: ModelCapabilities, overlay: ModelCapabilities
) -> ModelCapabilities:
    return ModelCapabilities(
        context_window_tokens=(
            overlay.context_window_tokens
            if overlay.context_window_tokens is not None
            else base.context_window_tokens
        ),
        supports_tools=overlay.supports_tools
        if overlay.supports_tools is not None
        else base.supports_tools,
        effort_values=list(overlay.effort_values)
        if overlay.effort_values is not None
        else base.effort_values,
    )


def builtin_model_capabilities(provider: str, model: str) -> ModelCapabilities:
    if provider == "anthropic":
        return ModelCapabilities(
            context_window_tokens=200_000, supports_tools=True, effort_values=[]
        )
    if provider == "deepseek" and model in ("deepseek-v4-flash", "deepseek-v4-pro"):
        return ModelCapabilities(
            context_window_tokens=1_000_000,
            supports_tools=True,
            effort_values=["off", "high", "max"],
        )
    if provider != "openai":
        return ModelCapabilities()
    if model == "gpt-5.4" or model.startswith("gpt-5.4-2026-") or model == "gpt-5.6-sol":
        return ModelCapabilities(
            context_window_tokens=1_050_000,
            supports_tools=True,
            effort_values=["none", "low", "medium", "high", "xhigh"],
        )
    if (
        model == "gpt-5.4-mini"
        or model.startswith("gpt-5.4-mini-2026-")
        or model == "gpt-5.4-nano"
        or model.startswith("gpt-5.4-nano-2026-")
    ):
        return ModelCapabilities(
            context_window_tokens=400_000,
            supports_tools=True,
            effort_values=["none", "low", "medium", "high", "xhigh"],
        )
    return ModelCapabilities()


class Provider:
    """One interface. Everything above it is provider-independent."""

    id: str = ""
    display_name: str = ""

    def __init__(self, selection: Selection) -> None:
        self.selection = selection
        self.model_aliases: dict[str, str] = {}
        self.model_overrides: dict[str, ModelCapabilities] = {}
        self.selection_model_may_be_alias = False
        self.remembers_selection = False
        self.context_window = 0

    async def stream(
        self, context: Context, selected: Selection, sink: StreamSink, cancel: CancelToken = NEVER
    ) -> StopReason:
        raise NotImplementedError

    async def list_models(self, cancel: CancelToken = NEVER) -> list[str]:
        raise AvaError(
            ErrorKind.provider, f"provider '{self.id}' does not support model enumeration"
        )

    def capabilities(self, model: str) -> ModelCapabilities:
        resolved = builtin_model_capabilities(self.id, model)
        configured = self.model_overrides.get(model)
        if configured is not None:
            resolved = merge_model_capabilities(resolved, configured)
        return merge_model_capabilities(resolved, self.discovered_capabilities(model))

    def discovered_capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities()

    async def aclose(self) -> None:
        return None


def encode_base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@dataclass(slots=True)
class RequestLimits:
    max_images: int = 20
    max_serialized_bytes: int = 32_000_000


REQUEST_LIMITS = RequestLimits()


def check_request_limits(
    image_count: int, serialized_bytes: int, limits: RequestLimits = REQUEST_LIMITS
) -> None:
    if image_count > limits.max_images:
        raise AvaError(
            ErrorKind.invalid_argument,
            f"request image count {image_count} exceeds the limit of {limits.max_images}",
        )
    if serialized_bytes > limits.max_serialized_bytes:
        raise AvaError(
            ErrorKind.invalid_argument,
            f"serialized request size {serialized_bytes} bytes exceeds the limit of "
            f"{limits.max_serialized_bytes} bytes",
        )


def is_context_overflow(error: AvaError) -> bool:
    """The provider's own rejection is the only authoritative context-overflow signal."""
    if error.kind != ErrorKind.provider:
        return False
    message = ascii_lower(error.message)
    detail = ascii_lower(error.detail)
    rejections = ("prompt is too long", "maximum context length", "context_length_exceeded")
    return any(rejection in message or rejection in detail for rejection in rejections)


def request_file_text(block: ContentBlock) -> str:
    path = (
        block.display_path.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    return f'<file path="{path}">\n{block.text}\n</file>'


def request_schema_type(param_type: ToolParamType) -> str:
    return param_type.value


# ---- Model ordering and alias resolution -------------------------------------------------------

_PART = re.compile(r"[0-9]+|[A-Za-z]+")


def _model_parts(model: str) -> list[tuple[str, bool]]:
    return [(part, part[0].isdigit()) for part in _PART.findall(model)]


def _numeric_before(left: str, right: str) -> int:
    left_version = len(left) <= 3
    right_version = len(right) <= 3
    if left_version != right_version:
        return -1 if left_version else 1
    left = left.lstrip("0") or "0"
    right = right.lstrip("0") or "0"
    if len(left) != len(right):
        return -1 if len(left) > len(right) else 1
    if left != right:
        return -1 if left > right else 1
    return 0


def _model_before(left: str, right: str) -> bool:
    left_parts = _model_parts(left)
    right_parts = _model_parts(right)
    common = min(len(left_parts), len(right_parts))
    for index in range(common):
        (ltext, lnum), (rtext, rnum) = left_parts[index], right_parts[index]
        if lnum != rnum:
            return lnum
        if ltext != rtext:
            if lnum:
                order = _numeric_before(ltext, rtext)
                return ltext < rtext if order == 0 else order < 0
            return ltext < rtext
    if len(left_parts) != len(right_parts):
        extra = left_parts[common] if len(left_parts) > common else right_parts[common]
        extra_is_version = extra[1] and len(extra[0]) <= 3
        return extra_is_version if len(left_parts) > common else not extra_is_version
    return left < right


def sort_model_ids(models: list[str]) -> None:
    """Version components sort newest first; an undated id precedes its dated snapshots."""
    import functools

    def compare(left: str, right: str) -> int:
        if left == right:
            return 0
        return -1 if _model_before(left, right) else 1

    models.sort(key=functools.cmp_to_key(compare))


def is_model_alias_candidate(model: str) -> bool:
    return bool(model) and model.isalpha() and model.isascii()


def _alias_matches(model: str, alias: str) -> bool:
    if not alias:
        return False
    position = model.find(alias)
    while position != -1:
        end = position + len(alias)
        bounded_left = position == 0 or not model[position - 1].isalnum()
        bounded_right = end == len(model) or not model[end].isalnum()
        if bounded_left and bounded_right:
            return True
        position = model.find(alias, position + 1)
    return False


def resolve_model_alias(requested: str, models: list[str], overrides: dict[str, str]) -> str:
    if requested in overrides:
        return overrides[requested]
    models = list(models)
    sort_model_ids(models)
    if requested in models:
        return requested
    # A local provider uses "default" only long enough to discover a single-model server.
    if requested == "default" and len(models) == 1:
        return models[0]
    for model in models:
        if _alias_matches(model, requested):
            return model
    return requested


async def resolve_selection_model(provider: Provider, cancel: CancelToken = NEVER) -> None:
    """Alias candidates resolve once before a request can log or consume the selection."""
    known = provider.capabilities(provider.selection.model)
    effort_needs_catalog = provider.selection.effort is not None and known.effort_values is None
    if not provider.selection_model_may_be_alias and not effort_needs_catalog:
        return
    catalog_resolved = False
    if provider.selection.model in provider.model_aliases:
        provider.selection.model = provider.model_aliases[provider.selection.model]
        catalog_resolved = True
    else:
        try:
            models = await provider.list_models(cancel)
        except AvaError as error:
            if error.kind == ErrorKind.cancelled:
                return
        else:
            provider.selection.model = resolve_model_alias(
                provider.selection.model, models, provider.model_aliases
            )
            catalog_resolved = True
    if catalog_resolved:
        capabilities = provider.capabilities(provider.selection.model)
        provider.context_window = capabilities.context_window_tokens or 0
    provider.selection_model_may_be_alias = False


def validate_effort(provider: Provider, selected: Selection) -> None:
    if selected.effort is None:
        return
    capabilities = provider.capabilities(selected.model)
    if capabilities.effort_values is None or selected.effort not in capabilities.effort_values:
        raise AvaError(
            ErrorKind.invalid_argument,
            f"provider '{selected.provider}' model '{selected.model}' does not advertise "
            f"reasoning effort '{selected.effort}'",
        )


async def stream(
    provider: Provider, context: Context, sink: StreamSink, cancel: CancelToken = NEVER
) -> StopReason:
    """Standalone entry point: alias resolution and effort validation, then the adapter."""
    requires_default_resolution = (
        provider.selection_model_may_be_alias and provider.selection.model == "default"
    )
    await resolve_selection_model(provider, cancel)
    cancel.raise_if_cancelled()
    if requires_default_resolution and provider.selection.model == "default":
        raise AvaError(
            ErrorKind.provider,
            f"provider '{provider.id}' could not resolve model alias 'default'; select a concrete "
            "model or check model catalog access",
        )
    selected = Selection(
        provider.selection.provider, provider.selection.model, provider.selection.effort
    )
    validate_effort(provider, selected)
    return await provider.stream(context, selected, sink, cancel)


def parse_model_ids(body: str, provider_name: str) -> list[str]:
    import json

    try:
        response = json.loads(body)
    except json.JSONDecodeError:
        response = None
    if not isinstance(response, dict) or not isinstance(response.get("data"), list):
        raise AvaError(
            ErrorKind.parse, f"{provider_name} model list is invalid; check endpoint compatibility"
        )
    if response.get("has_more") is True:
        raise AvaError(
            ErrorKind.provider, f"{provider_name} model list exceeds the 1000-model discovery limit"
        )
    models: list[str] = []
    for model in response["data"]:
        if isinstance(model, dict) and isinstance(model.get("id"), str) and model["id"]:
            models.append(model["id"])
    return models


def response_error(provider_name: str, status: int, detail: str) -> AvaError:
    kind = ErrorKind.provider
    action = "check the endpoint and provider status"
    if status in (401, 403):
        kind = ErrorKind.auth
        action = "check the API key"
    elif status == 429:
        kind = ErrorKind.rate_limited
        action = "wait and retry"
    return AvaError(kind, f"{provider_name} request returned HTTP {status}; {action}", detail)
