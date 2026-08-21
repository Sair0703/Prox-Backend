# services/store_service/capabilities/store_info_backfill/protocols.py

from __future__ import annotations

from typing import Protocol, runtime_checkable

from services.store_service.models.base import (
    FlyerDeal,
    StoreCandidate,
    StoreLocationRecord,
    StoreResolution,
)


@runtime_checkable
class FlyerDealBackfillOperatorProtocol(Protocol):
    """Backfill store-resolution information into a flyer deal."""

    def backfill(
        self,
        deal: FlyerDeal,
        best_candidate: StoreCandidate,
        candidates: list[StoreCandidate],
    ) -> StoreResolution:
        """
        Backfill store information into a flyer deal.

        :param deal: Flyer deal to update.
        :param best_candidate: Selected store candidate.
        :param candidates: Candidates considered during resolution.
        :return: Store resolution used for the backfill.
        """
        ...


@runtime_checkable
class StoreLocationBackfillOperatorProtocol(Protocol):
    """Backfill store information into a store-location record."""

    def backfill(
        self,
        store_location: StoreLocationRecord,
    ) -> None:
        """
        Persist store-location information.

        :param store_location: Store-location record containing values to write.
        """
        ...


__all__ = [
    "FlyerDealBackfillOperatorProtocol",
    "StoreLocationBackfillOperatorProtocol",
]
