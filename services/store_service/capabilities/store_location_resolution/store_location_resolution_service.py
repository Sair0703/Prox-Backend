# services/store_service/capabilities/store_location_resolution/store_location_resolution_service.py

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine, Sequence
from typing import Any, TypeVar

from services.store_service.capabilities.store_info_normalization.store_info_normalization_service import (
    StoreInfoNormalizationService,
)
from services.store_service.capabilities.store_location_resolution.models import (
    StoreCandidateBuckets,
)
from services.store_service.capabilities.store_location_resolution.protocols import (
    StoreCandidateSelectorProtocol,
    StoreLocatorProtocol,
)
from services.store_service.capabilities.store_location_resolution.selectors.nearest_distance_selector import (
    NearestDistanceSelector,
)
from services.store_service.capabilities.store_location_resolution.store_locator_aggregator import (
    StoreLocatorAggregator,
)
from services.store_service.models.base import FlyerDeal, StoreCandidate

logger = logging.getLogger(__name__)

T = TypeVar("T")


class StoreLocationResolutionService:
    """
    Resolve store candidates for flyer deals.

    Responsibilities:
    - normalize retailer identity for resolution;
    - execute configured store locators;
    - merge and deduplicate internal and external candidates;
    - select the best candidate;
    - cache resolved candidate lists.

    This capability only resolves candidates. Verification, correction,
    ingestion, promotion, and writeback belong to separate Store Service
    capabilities.
    """

    def __init__(
        self,
        store_locators: Sequence[StoreLocatorProtocol],
        *,
        locator_aggregator: StoreLocatorAggregator | None = None,
        store_selector: StoreCandidateSelectorProtocol | None = None,
        store_info_normalizer: StoreInfoNormalizationService | None = None,
    ) -> None:
        """
        Initialize the store-location resolution service.

        :param store_locators: Internal and external locators used to produce
            store candidates.
        :param locator_aggregator: Optional candidate aggregator override.
        :param store_selector: Optional best-candidate selector override.
        :param store_info_normalizer: Optional shared store-info normalizer.
        """
        self.store_locators = self._normalize_locators(store_locators)
        self.locator_aggregator = (
            locator_aggregator
            or StoreLocatorAggregator()
        )
        self.store_selector = (
            store_selector
            or NearestDistanceSelector()
        )
        self.store_info_normalizer = (
            store_info_normalizer
            or StoreInfoNormalizationService()
        )

        self._candidate_cache: dict[str, list[StoreCandidate]] = {}

    def find_candidate_stores(
        self,
        deal: FlyerDeal,
    ) -> list[StoreCandidate]:
        """
        Resolve and return merged store candidates for a flyer deal.

        :param deal: Flyer deal containing retailer and location context.
        :return: Merged internal and external store candidates.
        """
        if deal is None:
            return []

        return self._run_async(
            self.find_candidate_stores_async(deal)
        )

    async def find_candidate_stores_async(
        self,
        deal: FlyerDeal,
    ) -> list[StoreCandidate]:
        """
        Resolve store candidates asynchronously across all configured locators.

        :param deal: Flyer deal containing retailer and location context.
        :return: Merged internal and external store candidates.
        """
        if deal is None:
            return []

        retailer_key = self._resolve_retailer_key(deal)
        if not retailer_key:
            return []

        normalized_zip = (deal.zip_code or "").strip()
        if not normalized_zip:
            return []

        cache_key = self._cache_key(
            retailer_key=retailer_key,
            deal=deal,
        )

        cached = self._candidate_cache.get(cache_key)
        if cached is not None:
            return list(cached)

        tasks = [
            locator.find_candidate_stores(deal)
            for locator in self.store_locators
        ]

        results = await asyncio.gather(
            *tasks,
            return_exceptions=True,
        )

        all_candidates: list[StoreCandidate] = []

        for locator, result in zip(
            self.store_locators,
            results,
            strict=False,
        ):
            if isinstance(result, BaseException):
                logger.warning(
                    "[STORE_LOCATION_RESOLUTION] locator failed "
                    "retailer_key=%s zip=%s locator=%s error=%s",
                    retailer_key,
                    normalized_zip,
                    locator.__class__.__name__,
                    result,
                )
                continue

            if not isinstance(result, list):
                logger.warning(
                    "[STORE_LOCATION_RESOLUTION] locator returned "
                    "unexpected result retailer_key=%s zip=%s locator=%s",
                    retailer_key,
                    normalized_zip,
                    locator.__class__.__name__,
                )
                continue

            all_candidates.extend(result)

        merged_candidates = self._merge_candidates(
            all_candidates
        )
        self._candidate_cache[cache_key] = list(
            merged_candidates
        )

        logger.info(
            "[STORE_LOCATION_RESOLUTION] resolved "
            "retailer_key=%s zip=%s locator_count=%d candidate_count=%d",
            retailer_key,
            normalized_zip,
            len(self.store_locators),
            len(merged_candidates),
        )

        return merged_candidates

    def find_best_store_candidate(
        self,
        deal: FlyerDeal,
    ) -> StoreCandidate | None:
        """
        Resolve candidates and select the best store candidate.

        :param deal: Flyer deal containing retailer and location context.
        :return: Best store candidate, or None when no candidate is available.
        """
        candidates = self.find_candidate_stores(deal)
        if not candidates:
            return None

        return self.store_selector.select(candidates)

    async def find_best_store_candidate_async(
        self,
        deal: FlyerDeal,
    ) -> StoreCandidate | None:
        """
        Resolve candidates asynchronously and select the best candidate.

        :param deal: Flyer deal containing retailer and location context.
        :return: Best store candidate, or None when no candidate is available.
        """
        candidates = await self.find_candidate_stores_async(
            deal
        )
        if not candidates:
            return None

        return self.store_selector.select(candidates)

    def clear_cache(self) -> None:
        """Clear all cached candidate-resolution results."""
        self._candidate_cache.clear()

    def get_candidate_cache_stats(self) -> dict[str, Any]:
        """
        Return basic candidate-cache statistics.

        :return: Cache entry, candidate, non-empty result, and locator counts.
        """
        total_requests = len(self._candidate_cache)
        total_candidates = sum(
            len(candidates)
            for candidates in self._candidate_cache.values()
        )
        non_empty_results = sum(
            1
            for candidates in self._candidate_cache.values()
            if candidates
        )

        return {
            "cached_requests": total_requests,
            "cached_candidates": total_candidates,
            "non_empty_results": non_empty_results,
            "locator_count": len(self.store_locators),
        }

    def _merge_candidates(
        self,
        candidates: Sequence[StoreCandidate],
    ) -> list[StoreCandidate]:
        """
        Merge candidates returned by internal and external locators.

        :param candidates: Raw candidates returned by all locators.
        :return: Aggregated candidate list.
        """
        if not candidates:
            return []

        buckets = self._classify_store_candidates(
            candidates
        )

        aggregation_result = self.locator_aggregator.aggregate(
            local_candidates=buckets.local_candidates,
            external_candidates=buckets.non_local_candidates,
        )

        return aggregation_result.merged_candidates

    def _classify_store_candidates(
        self,
        candidates: Sequence[StoreCandidate],
    ) -> StoreCandidateBuckets:
        """
        Split candidates into internal and external buckets.

        :param candidates: Store candidates to classify.
        :return: Internal and non-internal candidate buckets.
        """
        local_candidates: list[StoreCandidate] = []
        non_local_candidates: list[StoreCandidate] = []

        for candidate in candidates:
            if self._is_local_candidate(candidate):
                local_candidates.append(candidate)
            else:
                non_local_candidates.append(candidate)

        local_candidates.sort(
            key=self._candidate_sort_key
        )
        non_local_candidates.sort(
            key=self._candidate_sort_key
        )

        return StoreCandidateBuckets(
            local_candidates=local_candidates,
            non_local_candidates=non_local_candidates,
        )

    def _resolve_retailer_key(
        self,
        deal: FlyerDeal,
    ) -> str | None:
        """
        Resolve the retailer key used by the resolution workflow.

        Persisted retailer keys are preferred. Retailer names are normalized
        through StoreInfoNormalizationService when a key is unavailable.

        :param deal: Flyer deal containing retailer identity.
        :return: Canonical retailer key, or None when retailer identity is missing.
        """
        retailer_raw = (
            (deal.retailer_key or "").strip()
            or (deal.retailer or "").strip()
        )

        if not retailer_raw:
            return None

        retailer_key = (
            self.store_info_normalizer.normalize_retailer_key(
                retailer_raw
            )
        )
        if retailer_key:
            return retailer_key

        return self.store_info_normalizer.make_retailer_key(
            retailer_raw
        )

    @staticmethod
    def _is_local_candidate(
        candidate: StoreCandidate,
    ) -> bool:
        """
        Determine whether a candidate belongs to an internal locator.

        :param candidate: Candidate to classify.
        :return: True for internal candidates; otherwise False.
        """
        locator_type = (
            candidate.locator_type
            or ""
        ).strip().lower()

        if locator_type == "internal":
            return True

        if locator_type == "external":
            return False

        # Preserve compatibility with older candidates without locator_type.
        return candidate.canonical_store_id > 0

    @staticmethod
    def _candidate_sort_key(
        candidate: StoreCandidate,
    ) -> tuple[float, int, int]:
        """Build the deterministic candidate ordering key."""
        locator_type = (
            candidate.locator_type
            or ""
        ).strip().lower()

        if locator_type == "internal":
            locator_rank = 0
        elif locator_type == "external":
            locator_rank = 1
        else:
            locator_rank = 2

        return (
            float(candidate.distance_meters or 0.0),
            locator_rank,
            int(candidate.canonical_store_id),
        )

    @staticmethod
    def _cache_key(
        retailer_key: str,
        deal: FlyerDeal,
    ) -> str:
        """
        Build the cache key for a retailer and deal context.

        :param retailer_key: Canonical retailer key.
        :param deal: Flyer deal containing location context.
        :return: Stable cache key for the resolution request.
        """
        return "|".join(
            [
                retailer_key or "",
                (deal.zip_code or "").strip(),
                (deal.city or "").strip().lower(),
                (deal.state or "").strip().lower(),
                (deal.retailer_address or "").strip().lower(),
                (
                    str(deal.store_lat)
                    if deal.store_lat is not None
                    else ""
                ),
                (
                    str(deal.store_lng)
                    if deal.store_lng is not None
                    else ""
                ),
            ]
        )

    @staticmethod
    def _normalize_locators(
        store_locators: Sequence[StoreLocatorProtocol],
    ) -> list[StoreLocatorProtocol]:
        """
        Validate and normalize the configured locator collection.

        :param store_locators: Locator implementations configured for resolution.
        :return: Non-empty locator list.
        :raises ValueError: When no usable locator is configured.
        """
        locators = [
            locator
            for locator in store_locators
            if locator is not None
        ]

        if not locators:
            raise ValueError(
                "store_locators cannot be empty"
            )

        return locators

    @staticmethod
    def _run_async(
        coroutine: Coroutine[Any, Any, T],
    ) -> T:
        """
        Run a resolution coroutine from the synchronous service interface.

        :param coroutine: Coroutine to execute.
        :return: Coroutine result.
        """
        return asyncio.run(coroutine)


__all__ = ["StoreLocationResolutionService"]