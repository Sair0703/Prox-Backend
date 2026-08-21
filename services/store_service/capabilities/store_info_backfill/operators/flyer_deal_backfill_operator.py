# services/store_service/capabilities/store_info_backfill/operators/flyer_deal_backfill_operator.py

from __future__ import annotations

from services.store_service.models.base import (
    FlyerDeal,
    StoreCandidate,
    StoreResolution,
)


class FlyerDealBackfillOperator:
    """
    Backfill store-resolution information into a flyer deal.

    This operator owns persistence operations that affect store-related fields
    on flyer deals. External candidates may require canonical store insertion
    before the flyer deal can reference them.

    The concrete persistence methods remain placeholders in the current demo
    implementation.
    """

    def backfill(
        self,
        deal: FlyerDeal,
        best_candidate: StoreCandidate,
        candidates: list[StoreCandidate],
    ) -> StoreResolution:
        """
        Backfill a flyer deal from the selected store candidate.

        :param deal: Flyer deal whose store-related fields should be updated.
        :param best_candidate: Candidate selected as the resolved store.
        :param candidates: Candidate set considered during resolution.
        :return: Store resolution written to the flyer deal.
        """
        if best_candidate.locator_type == "external":
            best_candidate.canonical_store_id = (
                self.insert_store_location(
                    best_candidate
                )
            )

        resolution = self._build_resolution(
            best_candidate=best_candidate,
            candidates=candidates,
        )

        self.update_flyer_deal_store(
            deal.id,
            resolution,
        )

        return resolution

    def insert_store_location(
        self,
        candidate: StoreCandidate,
    ) -> int:
        """
        Insert an external candidate as a canonical store location.

        :param candidate: External store candidate to persist.
        :return: Canonical store-location ID assigned to the inserted record.
        """
        ...

    def update_flyer_deal_store(
        self,
        deal_id: int,
        resolution: StoreResolution,
    ) -> None:
        """
        Update store-related flyer-deal fields from a resolution.

        :param deal_id: Flyer deal ID to update.
        :param resolution: Resolved store information to backfill.
        """
        ...

    @staticmethod
    def _build_resolution(
        best_candidate: StoreCandidate,
        candidates: list[StoreCandidate],
    ) -> StoreResolution:
        """
        Build the store-resolution payload used for flyer-deal backfill.

        :param best_candidate: Selected store candidate.
        :param candidates: Candidate set considered during resolution.
        :return: Store resolution describing the selected candidate.
        """
        return StoreResolution(
            store_id=best_candidate.canonical_store_id,
            store_lat=best_candidate.latitude,
            store_lng=best_candidate.longitude,
            match_confidence="high",
            candidate_store_count=len(candidates),
            matched_by="store_service",
            candidate_store_ids=[
                candidate.canonical_store_id
                for candidate in candidates
            ],
            canonical_match_stage="locator",
        )


__all__ = ["FlyerDealBackfillOperator"]
