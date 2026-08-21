# services/llm_service/prompts/store_prompts/repair_issues/spec.py

"""Prompt specification for LLM-assisted store issue repair."""

from __future__ import annotations

from pathlib import Path

from services.llm_service.models.base_models import PromptSpec


PROMPT_SPEC = PromptSpec(
    name="repair_store",
    template_path=Path(
        "services/llm_service/prompts/store_prompts/repair_issues/prompt.txt"
    ),
    input_keys=("store_location", "remaining_issues"),
)