# services/llm_service/contracts/input_contracts.py

"""Public input contracts accepted by the LLM service."""

from services.llm_service.prompts.store_prompts.detect_remaining_issues.contract import (
    DetectRemainingIssuesInput,
)
from services.llm_service.prompts.store_prompts.repair_issues.contract import (
    RepairStoreInput,
)

__all__ = [
    "DetectRemainingIssuesInput",
    "RepairStoreInput",
]