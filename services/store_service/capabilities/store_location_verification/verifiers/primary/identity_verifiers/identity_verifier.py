# services/store_service/capabilities/store_location_verification/verifiers/primary/identity_verifiers/identity_verifier.py

from __future__ import annotations

from services.store_service.capabilities.store_info_normalization.store_info_normalization_service import (
    StoreInfoNormalizationService,
)
from services.store_service.capabilities.store_location_verification.models import (
    StoreVerificationResult,
)
from services.store_service.capabilities.store_location_verification.verification_helper import (
    build_verification_result,
    confidence_from_issue_count,
    normalize_address_tokens,
    token_overlap_score,
)
from services.store_service.models.base import (
    DetectedIssue,
    StoreCandidate,
)
from services.store_service.models.store_location_issues import (
    AMBIGUOUS_RETAILER_IDENTITY,
    RETAILER_KEY_MISMATCH,
    STORE_IDENTITY_CONFLICT,
)


class IdentityVerifier:
    """
    Verify retailer identity and retailer-specific store identity fields.

    Retailer-key normalization is delegated to StoreInfoNormalizationService
    so identity checks share the same lookup-key behavior as resolution.
    """

    name = "identity_verifier"

    def __init__(
        self,
        store_info_normalizer: StoreInfoNormalizationService | None = None,
    ) -> None:
        """
        Initialize the identity verifier.

        :param store_info_normalizer: Optional shared store-info normalizer.
        """
        self.store_info_normalizer = (
            store_info_normalizer
            or StoreInfoNormalizationService()
        )

    def verify(
        self,
        store: StoreCandidate,
    ) -> StoreVerificationResult:
        """
        Verify retailer-key and store-identity consistency.

        :param store: Store candidate to verify.
        :return: Verification result containing identity issues.
        """
        issues: list[DetectedIssue] = []

        retailer_raw = (store.retailer or "").strip()
        retailer_key = (store.retailer_key or "").strip()

        normalized_retailer = (
            self.store_info_normalizer.normalize_retailer_key(
                retailer_raw
            )
            if retailer_raw
            else ""
        )
        fallback_retailer = (
            self.store_info_normalizer.make_retailer_key(
                retailer_raw
            )
            if retailer_raw
            else ""
        )

        if retailer_raw and retailer_key:
            # Accept both the explicit mapping and the fallback legacy key.
            if (
                normalized_retailer
                and retailer_key
                not in {
                    normalized_retailer,
                    fallback_retailer,
                }
            ):
                issues.append(
                    DetectedIssue(
                        name=RETAILER_KEY_MISMATCH.name,
                        description=RETAILER_KEY_MISMATCH.description,
                    )
                )
            elif (
                fallback_retailer
                and retailer_key
                not in {
                    normalized_retailer,
                    fallback_retailer,
                }
            ):
                issues.append(
                    DetectedIssue(
                        name=RETAILER_KEY_MISMATCH.name,
                        description=RETAILER_KEY_MISMATCH.description,
                    )
                )

        subject_text = " ".join(
            part
            for part in [
                store.store_name,
                store.full_address,
                store.address,
                store.city,
                store.state,
            ]
            if part
        )

        retailer_hint = (
            retailer_key
            or normalized_retailer
            or fallback_retailer
        )

        if retailer_hint and subject_text:
            # Compare retailer identity with the textual store evidence.
            overlap = token_overlap_score(
                retailer_hint,
                subject_text,
            )

            if overlap == 0.0:
                issues.append(
                    DetectedIssue(
                        name=AMBIGUOUS_RETAILER_IDENTITY.name,
                        description=AMBIGUOUS_RETAILER_IDENTITY.description,
                    )
                )

            if (
                retailer_raw
                and store.store_name
                and overlap == 0.0
            ):
                issues.append(
                    DetectedIssue(
                        name=STORE_IDENTITY_CONFLICT.name,
                        description=STORE_IDENTITY_CONFLICT.description,
                    )
                )
        else:
            # Preserve the existing normalization helper path without adding
            # a new identity rule when there is no usable evidence.
            _ = normalize_address_tokens(subject_text)

        return build_verification_result(
            store,
            verified=not issues,
            confidence_score=confidence_from_issue_count(
                len(issues)
            ),
            issues=issues,
        )


__all__ = ["IdentityVerifier"]