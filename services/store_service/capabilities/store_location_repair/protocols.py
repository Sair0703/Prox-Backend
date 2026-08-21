# services/store_service/capabilities/store_location_repair/protocols.py

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from services.store_service.capabilities.store_location_repair.models import (
    StoreRepairResult,
)
from services.store_service.models.base import (
    DetectedIssue,
    StoreCandidate,
)


@runtime_checkable
class StoreRepairStrategyProtocol(Protocol):
    """Patch a store candidate using a specific repair strategy."""

    def patch(
        self,
        candidate: StoreCandidate,
        issues: Sequence[DetectedIssue],
    ) -> tuple[StoreCandidate, list[object], float | None, bool]:
        """
        Apply a repair strategy.

        :param candidate: Store candidate to repair.
        :param issues: Issues routed to the strategy.
        :return: Repaired candidate, repair changes, confidence, and
            manual-review flag.
        """
        ...


@runtime_checkable
class StoreRepairServiceProtocol(Protocol):
    """Public contract for the store-location repair capability."""

    def repair(
        self,
        candidate: StoreCandidate,
        issues: Sequence[DetectedIssue],
    ) -> StoreRepairResult:
        """
        Repair the supplied candidate using the configured strategies.

        :param candidate: Store candidate to repair.
        :param issues: Verification issues driving the repair.
        :return: Repair result containing the repaired candidate and diagnostics.
        """
        ...


__all__ = [
    "StoreRepairServiceProtocol",
    "StoreRepairStrategyProtocol",
]
