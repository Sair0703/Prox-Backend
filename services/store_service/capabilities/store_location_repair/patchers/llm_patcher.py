# services/store_service/capabilities/store_location_repair/patchers/llm_patcher.py

from __future__ import annotations

from collections.abc import Sequence

from services.llm_service.prompts.store_prompts.repair_issues.contract import RepairChange
from services.store_service.models.base import (
    DetectedIssue,
    StoreCandidate,
)
from services.store_service.capabilities.store_location_repair.patchers.patch_strategies.llm.llm_patch_strategy import (
    LLMPatchStrategy,
)


class LLMPatcher:
    """
    Route semantic store-repair issues to the configured LLM strategy.

    This class is intentionally thin: the strategy owns LLM request
    construction, response parsing, and candidate mutation.
    """

    def __init__(
        self,
        strategy: LLMPatchStrategy,
    ) -> None:
        """
        Initialize the LLM patcher.

        :param strategy: LLM repair strategy used to patch a store candidate.
        """
        self.strategy = strategy

    def patch(
        self,
        candidate: StoreCandidate,
        issues: Sequence[DetectedIssue],
    ) -> tuple[StoreCandidate, list[RepairChange], float, bool]:
        """
        Apply LLM-based repairs to a store candidate.

        :param candidate: Store candidate to repair.
        :param issues: Issues routed to semantic LLM repair.
        :return: Repaired candidate, repair changes, overall confidence,
            and manual-review flag.
        """
        return self.strategy.patch(
            candidate,
            issues,
        )


__all__ = ["LLMPatcher"]