# services/store_service/capabilities/store_location_verification/models.py

from __future__ import annotations

from dataclasses import dataclass, field

from services.store_service.models.base import (
    DetectedIssue,
)


@dataclass(slots=True)
class StoreVerificationResult:
    """Contains the verification decision and issues for a store candidate."""

    verified: bool
    confidence_score: float
    issues: list[DetectedIssue] = field(default_factory=list)
    canonical_store_id: int | None = None
    retailer_store_id: str | None = None

    @property
    def reason_codes(self) -> list[str]:
        """Return the unique issue names associated with the result."""
        return [issue.name for issue in self.issues]


__all__ = ["StoreVerificationResult"]