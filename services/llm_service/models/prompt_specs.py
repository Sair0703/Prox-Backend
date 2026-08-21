# services/llm_service/models/prompt_specs.py

"""Registry of prompt specifications available to the LLM service."""

from __future__ import annotations

from services.llm_service.models.base_models import PromptSpec

from services.llm_service.prompts.store_prompts.detect_remaining_issues.spec import (
    PROMPT_SPEC as DETECT_REMAINING_ISSUES_PROMPT_SPEC,
)
from services.llm_service.prompts.store_prompts.repair_issues.spec import (
    PROMPT_SPEC as REPAIR_ISSUES_PROMPT_SPEC,
)

PROMPT_SPECS: dict[str, PromptSpec] = {
    DETECT_REMAINING_ISSUES_PROMPT_SPEC.name: DETECT_REMAINING_ISSUES_PROMPT_SPEC,
    REPAIR_ISSUES_PROMPT_SPEC.name: REPAIR_ISSUES_PROMPT_SPEC,
}