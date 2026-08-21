# services/store_service/capabilities/store_location_resolution/models.py

from __future__ import annotations

from dataclasses import dataclass, field

from services.store_service.models.base import StoreCandidate


@dataclass(slots=True)
class StoreCandidateBuckets:
    """Groups resolution candidates by internal and external ownership."""

    local_candidates: list[StoreCandidate] = field(default_factory=list)
    non_local_candidates: list[StoreCandidate] = field(default_factory=list)


@dataclass(slots=True)
class AggregationMatch:
    """Represents an external candidate matched to an internal candidate."""

    local_candidate: StoreCandidate
    external_candidate: StoreCandidate
    similarity_score: float
    reason_codes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class LocatorAggregationResult:
    """Contains merged candidates and locator-aggregation diagnostics."""

    merged_candidates: list[StoreCandidate]
    local_candidates: list[StoreCandidate]
    external_candidates: list[StoreCandidate]
    matched_pairs: list[AggregationMatch] = field(default_factory=list)
    dropped_external_candidates: list[StoreCandidate] = field(default_factory=list)


__all__ = [
    "AggregationMatch",
    "LocatorAggregationResult",
    "StoreCandidateBuckets",
]