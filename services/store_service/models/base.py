# services/store_service/models/base.py

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

MILES_PER_METER = 0.000621371


@dataclass(slots=True)
class StoreCandidate:
    """Shared store candidate representation used across Store Service capabilities."""

    canonical_store_id: int
    retailer: str | None = None
    retailer_store_id: str | None = None
    retailer_key: str | None = None
    store_name: str | None = None
    address: str | None = None
    full_address: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    geocode_source: str | None = None
    geocode_confidence: str | None = None
    geocoded_at: datetime | None = None
    osm_id: str | None = None
    locator_type: str | None = None
    locator_name: str | None = None
    show_on_map: bool | None = None
    distance_meters: float = 0.0

    @property
    def distance_miles(self) -> float:
        """Return candidate distance in miles."""
        return self.distance_meters * MILES_PER_METER


@dataclass(slots=True)
class StoreLocationRecord:
    """Shared canonical store-location record representation."""

    id: int
    retailer: str
    store_id: str | None
    latitude: float | None = None
    longitude: float | None = None
    address: str | None = None
    zip_code: str | None = None
    full_address: str | None = None
    retailer_key: str | None = None
    geocode_source: str | None = None
    geocode_confidence: str | None = None
    geocoded_at: str | None = None
    osm_id: str | None = None
    source: str | None = None
    store_name: str | None = None
    city: str | None = None
    state: str | None = None
    show_on_map: bool | None = None


@dataclass(slots=True)
class DetectedIssue:
    """Shared issue representation produced by verification and consumed downstream."""

    name: str
    description: str


@dataclass(slots=True)
class StoreResolution:
    """Shared store-resolution payload consumed by downstream store workflows."""

    store_id: int | None
    match_confidence: str
    candidate_store_count: int
    matched_by: str
    store_lat: float | None = None
    store_lng: float | None = None
    canonical_match_stage: str | None = None
    candidate_store_ids: list[int] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        """Serialize the resolution into the persistence payload shape."""
        return {
            "store_id": self.store_id,
            "store_lat": self.store_lat,
            "store_lng": self.store_lng,
            "match_confidence": self.match_confidence,
            "candidate_store_count": self.candidate_store_count,
            "matched_by": self.matched_by,
            "candidate_store_ids": (
                self.candidate_store_ids
                or None
            ),
            "canonical_match_stage": self.canonical_match_stage,
        }


@dataclass(slots=True)
class FlyerDeal:
    """Shared flyer-deal context used by Store Service store workflows."""

    id: int
    retailer: str
    retailer_key: str | None
    zip_code: str
    city: str | None = None
    state: str | None = None
    retailer_address: str | None = None
    store_lat: float | None = None
    store_lng: float | None = None


@dataclass(frozen=True, slots=True)
class IssueType:
    """Shared definition used to describe and route store-location issues."""

    name: str
    category: str
    description: str
    repair_hint: str
    route_to: str
    repair_fields: tuple[str, ...]