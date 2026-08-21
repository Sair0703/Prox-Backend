# services/store_service/capabilities/store_location_verification/protocols.py

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from services.llm_service.prompts.store_prompts.detect_remaining_issues.contract import (
    DetectRemainingIssuesInput,
)
from services.store_service.capabilities.store_location_verification.models import (
    StoreVerificationResult,
)
from services.store_service.models.base import (
    DetectedIssue,
    StoreCandidate,
)


@runtime_checkable
class StoreVerifierProtocol(Protocol):
    """Verify a store candidate and return a verification result."""

    def verify(
        self,
        store: StoreCandidate,
    ) -> StoreVerificationResult:
        """
        Verify a store candidate.

        :param store: Store candidate to verify.
        :return: Verification result produced by the verifier.
        """
        ...


@runtime_checkable
class SecondaryStoreVerifierProtocol(Protocol):
    """Evaluate issues that remain after an earlier verification stage."""

    def verify(
        self,
        request: DetectRemainingIssuesInput,
    ) -> StoreVerificationResult:
        """
        Evaluate a structured secondary-verification request.

        :param request: Store data and candidate issues to evaluate.
        :return: Verification result containing remaining issues.
        """
        ...


__all__ = [
    "SecondaryStoreVerifierProtocol",
    "StoreVerifierProtocol",
]