# services/llm_service/prompts/store_prompts/detect_remaining_issues/spec.py

"""Prompt specification for remaining store issue detection."""

from __future__ import annotations

from pathlib import Path

from services.llm_service.models.base_models import PromptSpec


PROMPT_SPEC = PromptSpec(
    name="detect_remaining_issues",
    template_path=Path(
        "services/llm_service/prompts/store_prompts/detect_remaining_issues/prompt.txt"
    ),
    input_keys=("store_location", "candidate_issues"),
)