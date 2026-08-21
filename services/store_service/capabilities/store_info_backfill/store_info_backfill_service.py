# services/store_service/capabilities/store_info_backfill/store_info_backfill_service.py

from __future__ import annotations

from services.store_service.capabilities.store_info_backfill.operators.flyer_deal_backfill_operator import (
    FlyerDealBackfillOperator,
)
from services.store_service.capabilities.store_info_backfill.operators.store_location_backfill_operator import (
    StoreLocationBackfillOperator,
)
from services.store_service.models.base import (
    FlyerDeal,
    StoreCandidate,
    StoreLocationRecord,
    StoreResolution,
)


class StoreInfoBackfillService:
    """
    Provide a unified interface for store-related persistence and backfill.

    The service delegates target-specific persistence to backfill operators.
    It can update canonical store-location information or store-related fields
    on flyer deals without coupling callers to the underlying persistence path.
    """

    def __init__(
        self,
        store_location_operator: StoreLocationBackfillOperator | None = None,
        flyer_deal_operator: FlyerDealBackfillOperator | None = None,
    ) -> None:
        """
        Initialize the store-info backfill service.

        :param store_location_operator: Optional store-location backfill operator.
        :param flyer_deal_operator: Optional flyer-deal backfill operator.
        """
        self.store_location_operator = (
            store_location_operator
            or StoreLocationBackfillOperator()
        )
        self.flyer_deal_operator = (
            flyer_deal_operator
            or FlyerDealBackfillOperator()
        )

    def backfill_store_location(
        self,
        store_location: StoreLocationRecord,
    ) -> None:
        """
        Backfill store information into a store-location record.

        :param store_location: Store-location record containing values to persist.
        """
        self.store_location_operator.backfill(
            store_location
        )

    def backfill_flyer_deal(
        self,
        deal: FlyerDeal,
        best_candidate: StoreCandidate,
        candidates: list[StoreCandidate],
    ) -> StoreResolution:
        """
        Backfill resolved store information into a flyer deal.

        :param deal: Flyer deal whose store-related fields should be updated.
        :param best_candidate: Candidate selected as the resolved store.
        :param candidates: Candidate set considered during resolution.
        :return: Store resolution used for the flyer-deal backfill.
        """
        return self.flyer_deal_operator.backfill(
            deal=deal,
            best_candidate=best_candidate,
            candidates=candidates,
        )


__all__ = ["StoreInfoBackfillService"]
