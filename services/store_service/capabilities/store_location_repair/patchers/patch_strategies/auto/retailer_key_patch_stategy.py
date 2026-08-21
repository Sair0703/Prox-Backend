# services/store_service/capabilities/store_location_repair/patchers/patch_strategies/auto/retailer_key_patch_stategy.py

from __future__ import annotations

from dataclasses import replace

from services.llm_service.prompts.store_prompts.repair_issues.contract import RepairChange
from services.store_service.capabilities.store_info_normalization.store_info_normalization_service import (
    StoreInfoNormalizationService,
)
from services.store_service.models.base import (
    DetectedIssue,
    StoreCandidate,
)


class RetailerKeyPatchStrategy:
    """
    Regenerate retailer keys for deterministic identity-related issues.

    Supported issues:
    - ``missing_retailer_key``
    - ``retailer_key_mismatch``

    Retailer-key generation is delegated to StoreInfoNormalizationService so
    repair uses the same normalization behavior as resolution and verification.
    """

    SUPPORTED_ISSUES = {
        "missing_retailer_key",
        "retailer_key_mismatch",
    }

    def __init__(
        self,
        normalizer: StoreInfoNormalizationService | None = None,
    ) -> None:
        """
        Initialize the retailer-key repair strategy.

        :param normalizer: Optional shared store-info normalization service.
        """
        self.normalizer = (
            normalizer
            or StoreInfoNormalizationService()
        )

    def patch(
        self,
        candidate: StoreCandidate,
        issue: DetectedIssue,
    ) -> tuple[StoreCandidate, list[RepairChange]]:
        """
        Repair the retailer key when the issue is supported.

        :param candidate: Store candidate to repair.
        :param issue: Retailer-key issue being repaired.
        :return: Updated candidate and the generated repair change, or no-op
            when the issue cannot be deterministically repaired.
        """
        if issue.name not in self.SUPPORTED_ISSUES:
            return candidate, []

        expected = self._derive_retailer_key(
            candidate.retailer
        )
        if not expected:
            return candidate, []

        before = candidate.retailer_key
        if before == expected:
            return candidate, []

        updated = replace(
            candidate,
            retailer_key=expected,
        )

        return (
            updated,
            [
                RepairChange(
                    issue=issue.name,
                    fields=["retailer_key"],
                    before=before,
                    after=expected,
                    confidence=1.0,
                    repaired_by="auto",
                )
            ],
        )

    def _derive_retailer_key(
        self,
        retailer: str | None,
    ) -> str | None:
        """
        Derive a canonical retailer lookup key from a retailer name.

        :param retailer: Raw retailer name.
        :return: Canonical retailer key, or None when it cannot be derived.
        """
        if not retailer:
            return None

        return (
            self.normalizer.normalize_retailer_key(retailer)
            or self.normalizer.make_retailer_key(retailer)
        )


__all__ = ["RetailerKeyPatchStrategy"]