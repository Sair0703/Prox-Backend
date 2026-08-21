# services/store_service/capabilities/store_location_resolution/selectors/nearest_distance_selector.py

from __future__ import annotations

from typing import Sequence

from services.store_service.models.base import StoreCandidate


class NearestDistanceSelector:
    """
    Select the best store candidate by distance.

    Policy:
    - smaller distance wins
    - internal candidates are preferred when distances are close enough
    """

    def __init__(
        self,
        internal_prefer_threshold_meters: float = 50.0,
    ) -> None:
        """
        Initialize the distance-based candidate selector.

        :param internal_prefer_threshold_meters: Maximum distance advantage an
            external candidate may have while still preferring the nearest
            internal candidate.
        """
        self.internal_prefer_threshold_meters = (
            internal_prefer_threshold_meters
        )

    def select(
        self,
        candidates: Sequence[StoreCandidate],
    ) -> StoreCandidate | None:
        """
        Select the best candidate using distance and locator-source preference.

        :param candidates: Store candidates available for selection.
        :return: Best candidate, or None when no candidates are available.
        """
        if not candidates:
            return None

        internals = [
            candidate
            for candidate in candidates
            if self._locator_rank(candidate.locator_type) == 0
        ]
        externals = [
            candidate
            for candidate in candidates
            if self._locator_rank(candidate.locator_type) == 1
        ]

        if internals and externals:
            best_internal = min(
                internals,
                key=lambda candidate: float(
                    candidate.distance_meters or 0.0
                ),
            )
            best_external = min(
                externals,
                key=lambda candidate: float(
                    candidate.distance_meters or 0.0
                ),
            )

            internal_distance = float(
                best_internal.distance_meters or 0.0
            )
            external_distance = float(
                best_external.distance_meters or 0.0
            )

            if internal_distance <= (
                external_distance
                + self.internal_prefer_threshold_meters
            ):
                return best_internal

        ranked = self.rank_candidates(candidates)
        return ranked[0] if ranked else None

    def rank_candidates(
        self,
        candidates: Sequence[StoreCandidate],
    ) -> list[StoreCandidate]:
        """
        Rank candidates by distance and locator-source preference.

        :param candidates: Store candidates to rank.
        :return: Ranked candidates from best to worst.
        """
        if not candidates:
            return []

        return sorted(
            candidates,
            key=self._candidate_rank_key,
        )

    def _candidate_rank_key(
        self,
        candidate: StoreCandidate,
    ) -> tuple[float, int]:
        """Build the distance and locator-source ranking key."""
        distance = float(candidate.distance_meters or 0.0)
        locator_rank = self._locator_rank(
            candidate.locator_type
        )

        return distance, locator_rank

    @staticmethod
    def _locator_rank(
        locator_type: str | None,
    ) -> int:
        """Return the ranking priority for a locator source type."""
        normalized = (
            locator_type or ""
        ).strip().lower()

        if normalized == "internal":
            return 0

        if normalized == "external":
            return 1

        return 2


__all__ = ["NearestDistanceSelector"]