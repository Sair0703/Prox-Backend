# services/store_service/capabilities/store_location_repair/patchers/auto_patcher.py

from __future__ import annotations

from services.llm_service.prompts.store_prompts.repair_issues.contract import RepairChange
from services.store_service.models.base import (
    DetectedIssue,
    StoreCandidate,
)
from services.store_service.capabilities.store_location_repair.patchers.patch_strategies.auto.normalize_text_patch_strategy import (
    NormalizeTextPatchStrategy,
)
from services.store_service.capabilities.store_location_repair.patchers.patch_strategies.auto.retailer_key_patch_strategy import (
    RetailerKeyPatchStrategy,
)


AUTO_ISSUE_TO_STRATEGY = {
    "case_variation": NormalizeTextPatchStrategy(),
    "punctuation_variation": NormalizeTextPatchStrategy(),
    "whitespace_variation": NormalizeTextPatchStrategy(),
    "abbreviation_variation": NormalizeTextPatchStrategy(),
    "direction_alias_variation": NormalizeTextPatchStrategy(),
    "missing_retailer_key": RetailerKeyPatchStrategy(),
    "retailer_key_mismatch": RetailerKeyPatchStrategy(),
}


class AutoPatcher:
    """
    Apply deterministic repair strategies to supported issue types.

    Unsupported issues are intentionally ignored so that the higher-level
    repair orchestrator can route them to another repair strategy.
    """

    def patch(
        self,
        candidate: StoreCandidate,
        issues: list[DetectedIssue],
    ) -> tuple[StoreCandidate, list[RepairChange]]:
        """
        Apply all supported automatic repairs in issue order.

        :param candidate: Store candidate to repair.
        :param issues: Issues routed to deterministic auto repair.
        :return: Updated candidate and all generated repair changes.
        """
        current = candidate
        changes: list[RepairChange] = []

        for issue in issues:
            strategy = AUTO_ISSUE_TO_STRATEGY.get(issue.name)
            if strategy is None:
                continue

            current, issue_changes = strategy.patch(
                current,
                issue,
            )
            changes.extend(issue_changes)

        return current, changes


__all__ = ["AutoPatcher", "AUTO_ISSUE_TO_STRATEGY"]