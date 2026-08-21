# services/store_service/capabilities/store_location_verification/verifiers/primary/basic_verifiers/field_completeness_verifier.py

from __future__ import annotations

from services.store_service.capabilities.store_location_verification.verification_helper import (
    build_verification_result,
    confidence_from_issue_count,
)
from services.store_service.models.base import (
    DetectedIssue,
    StoreCandidate,
)
from services.store_service.capabilities.store_location_verification.models import (
    StoreVerificationResult,
)
from services.store_service.models.store_location_issues import (
    MISSING_ADDRESS,
    MISSING_CITY,
    MISSING_FULL_ADDRESS,
    MISSING_RETAILER_KEY,
    MISSING_STATE,
    MISSING_STORE_ID,
)


class FieldCompletenessVerifier:
    """
    Verify that required store identity and address fields are populated.

    This verifier reports missing required values and leaves any correction
    decision to the downstream correction workflow.
    """

    name = "field_completeness_verifier"

    def verify(
        self,
        store: StoreCandidate,
    ) -> StoreVerificationResult:
        """
        Verify required store fields.

        :param store: Store candidate to verify.
        :return: Verification result containing missing-field issues.
        """
        issues: list[DetectedIssue] = []

        if not (store.address or "").strip():
            issues.append(
                DetectedIssue(
                    MISSING_ADDRESS.name,
                    MISSING_ADDRESS.description,
                )
            )

        if not (store.full_address or "").strip():
            issues.append(
                DetectedIssue(
                    MISSING_FULL_ADDRESS.name,
                    MISSING_FULL_ADDRESS.description,
                )
            )

        if not (store.city or "").strip():
            issues.append(
                DetectedIssue(
                    MISSING_CITY.name,
                    MISSING_CITY.description,
                )
            )

        if not (store.state or "").strip():
            issues.append(
                DetectedIssue(
                    MISSING_STATE.name,
                    MISSING_STATE.description,
                )
            )

        if not str(store.retailer_store_id or "").strip():
            issues.append(
                DetectedIssue(
                    MISSING_STORE_ID.name,
                    MISSING_STORE_ID.description,
                )
            )

        if not (store.retailer_key or "").strip():
            issues.append(
                DetectedIssue(
                    MISSING_RETAILER_KEY.name,
                    MISSING_RETAILER_KEY.description,
                )
            )

        return build_verification_result(
            store,
            verified=not issues,
            confidence_score=confidence_from_issue_count(
                len(issues)
            ),
            issues=issues,
        )


__all__ = ["FieldCompletenessVerifier"]