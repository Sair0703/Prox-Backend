# services/llm_service/contracts/output_contracts.py

"""Public output contracts returned by the LLM service."""

from services.llm_service.prompts.store_prompts.detect_remaining_issues.contract import (
    DetectRemainingIssuesOutput,
)
from services.llm_service.prompts.store_prompts.repair_issues.contract import (
    RepairStoreOutput,
)

__all__ = [
    "DetectRemainingIssuesOutput",
    "RepairStoreOutput",
]