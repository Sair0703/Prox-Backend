# services/llm_service/prompts/prompt_renderer.py

"""Renders prompt templates from prompt specifications and structured inputs."""

from __future__ import annotations

import re
from typing import Any

from services.llm_service.models.base_models import PromptSpec


class PromptRenderer:
    """Renders validated payloads into task-specific prompt templates."""

    def render(self, spec: PromptSpec, payload: dict[str, Any]) -> str:
        """
        Render a prompt from its specification and input payload.

        :param spec: Prompt specification defining the template and required inputs.
        :param payload: Structured input values used to render the prompt.
        :return: Fully rendered prompt ready for LLM execution.
        :raises ValueError: If required inputs are missing or placeholders cannot be resolved.
        """
        context = self._build_context(spec, payload)
        template = spec.template_path.read_text(encoding="utf-8")
        return self._replace_placeholders(template, context)

    def _build_context(
        self,
        spec: PromptSpec,
        payload: dict[str, Any],
    ) -> dict[str, str]:
        """
        Validate and serialize the inputs required by a prompt.

        :param spec: Prompt specification defining the required input keys.
        :param payload: Structured input values provided for rendering.
        :return: Serialized values keyed by prompt input name.
        :raises ValueError: If any required prompt input is missing.
        """
        missing = [key for key in spec.input_keys if key not in payload]
        if missing:
            raise ValueError(f"Missing prompt inputs for {spec.name}: {missing}")

        context: dict[str, str] = {}
        for key in spec.input_keys:
            value = payload[key]
            context[key] = self._stringify(value)

        return context

    @staticmethod
    def _stringify(value: Any) -> str:
        """
        Convert a prompt input into its template representation.

        :param value: Input value to serialize.
        :return: String representation suitable for prompt rendering.
        """
        if isinstance(value, str):
            return value

        import json

        return json.dumps(value, ensure_ascii=False, indent=2)

    @staticmethod
    def _replace_placeholders(
        template: str,
        context: dict[str, str],
    ) -> str:
        """
        Replace template placeholders with rendered input values.

        :param template: Prompt template containing named placeholders.
        :param context: Rendered values keyed by placeholder name.
        :return: Prompt with all recognized placeholders replaced.
        :raises ValueError: If a placeholder has no corresponding input value.
        """
        def repl(match: re.Match[str]) -> str:
            key = match.group(1).strip()
            if key not in context:
                raise ValueError(f"Unresolved placeholder: {key}")
            return context[key]

        return re.sub(
            r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}",
            repl,
            template,
        )