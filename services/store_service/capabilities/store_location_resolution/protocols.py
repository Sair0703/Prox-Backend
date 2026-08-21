# services/store_service/capabilities/store_location_resolution/protocols.py

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from services.store_service.models.base import (
    FlyerDeal,
    StoreCandidate,
)


@runtime_checkable
class StoreLocatorProtocol(Protocol):
    """Resolves candidate stores for a deal."""

    async def find_candidate_stores(
        self,
        deal: FlyerDeal,
    ) -> list[StoreCandidate]:
        ...


@runtime_checkable
class StoreCandidateSelectorProtocol(Protocol):
    """Selects the best candidate from a candidate list."""

    def select(
        self,
        candidates: Sequence[StoreCandidate],
    ) -> StoreCandidate | None:
        ...


__all__ = [
    "StoreLocatorProtocol",
    "StoreCandidateSelectorProtocol",
]