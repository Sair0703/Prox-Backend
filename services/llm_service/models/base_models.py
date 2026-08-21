# services/llm_service/models/base.py

"""Core data models shared across the LLM service."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """Provider-independent request for an LLM execution."""

    task_name: str
    model: str
    prompt: str
    temperature: float = 0.0
    max_tokens: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LLMUsage:
    """Token usage reported by an LLM provider."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """Provider-independent result returned from an LLM execution."""

    success: bool
    provider: str
    model: str
    raw_text: str
    parsed_output: dict[str, Any] | None = None
    error: str | None = None
    usage: LLMUsage | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PromptSpec:
    """Defines a prompt template and the inputs required to render it."""

    name: str
    template_path: Path
    input_keys: tuple[str, ...]