from __future__ import annotations

from dataclasses import dataclass, field

from services.store_service.capabilities.store_location_repair.models import (
    StoreRepairResult,
)
from services.store_service.capabilities.store_location_verification.models import (
    StoreVerificationResult,
)
from services.store_service.models.base import (
    DetectedIssue,
    StoreCandidate,
)


@dataclass(slots=True)
class StoreValidationWorkflowResult:
    """Result of the verification and repair workflow."""

    original_candidate: StoreCandidate
    final_candidate: StoreCandidate
    initial_verification: StoreVerificationResult
    repair_result: StoreRepairResult | None
    final_verification: StoreVerificationResult
    unresolved_issues: list[DetectedIssue] = field(
        default_factory=list
    )
    status: str = "unresolved"

    @property
    def verified(self) -> bool:
        """Return whether the final candidate passed verification."""
        return self.final_verification.verified


__all__ = ["StoreValidationWorkflowResult"]