# services/store_service/capabilities/store_location_repair/patchers/locator_pacther.py

from __future__ import annotations

from collections.abc import Sequence

from services.llm_service.prompts.store_prompts.repair_issues.contract import RepairChange
from services.store_service.models.base import (
    DetectedIssue,
    StoreCandidate,
)
from services.store_service.capabilities.store_location_repair.patchers.patch_strategies.locator.osm_patch_strategy import (
    OSMPatchStrategy,
)


class LocatorPatcher:
    """
    Route locator-backed repair issues to the configured locator strategy.

    The patcher owns no repair logic itself; OSMPatchStrategy performs the
    external lookup, candidate filtering, and field-level repair.
    """

    def __init__(
        self,
        strategy: OSMPatchStrategy,
    ) -> None:
        """
        Initialize the locator patcher.

        :param strategy: Locator-backed repair strategy.
        """
        self.strategy = strategy

    def patch(
        self,
        candidate: StoreCandidate,
        issues: Sequence[DetectedIssue],
    ) -> tuple[StoreCandidate, list[RepairChange], float | None, bool]:
        """
        Apply locator-backed repairs to a store candidate.

        :param candidate: Store candidate to repair.
        :param issues: Issues routed to locator-based repair.
        :return: Repaired candidate, repair changes, optional confidence,
            and manual-review flag.
        """
        return self.strategy.patch(
            candidate=candidate,
            issues=issues,
        )


__all__ = ["LocatorPatcher"]