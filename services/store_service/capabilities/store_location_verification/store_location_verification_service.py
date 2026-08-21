from __future__ import annotations

from collections.abc import Sequence

from services.llm_service.prompts.store_prompts.detect_remaining_issues.contract import (
    CandidateIssue,
    DetectRemainingIssuesInput,
)
from services.store_service.capabilities.store_location_verification.models import (
    StoreVerificationResult,
)
from services.store_service.capabilities.store_location_verification.protocols import (
    SecondaryStoreVerifierProtocol,
    StoreVerifierProtocol,
)
from services.store_service.capabilities.store_location_verification.verification_helper import (
    build_verification_result,
    merge_issues,
)
from services.store_service.models.base import (
    DetectedIssue,
    StoreCandidate,
)


class StoreLocationVerificationService:
    """
    Verify store candidates through configurable verification layers.

    Primary verification is required, but the concrete primary verifier set is
    fully configurable. Secondary verification is optional and provides
    additional verification coverage, such as LLM-backed issue detection.

    The service only evaluates candidates. It does not modify candidates,
    perform repair, or decide how detected issues should be corrected.
    """

    def __init__(
        self,
        primary_verifiers: Sequence[StoreVerifierProtocol],
        secondary_verifiers: Sequence[SecondaryStoreVerifierProtocol] | None = None,
    ) -> None:
        """
        Initialize the store-location verification service.

        :param primary_verifiers: Required but configurable verifier set used
            for baseline verification.
        :param secondary_verifiers: Optional verifier set used to enhance
            verification coverage.
        :raises ValueError: If no primary verifier is configured.
        """
        self.primary_verifiers = [
            verifier
            for verifier in primary_verifiers
            if verifier is not None
        ]
        self.secondary_verifiers = [
            verifier
            for verifier in (secondary_verifiers or [])
            if verifier is not None
        ]

        if not self.primary_verifiers:
            raise ValueError(
                "primary_verifiers cannot be empty"
            )

    def verify(
        self,
        store: StoreCandidate,
    ) -> StoreVerificationResult:
        """
        Run the configured primary verification set.

        This is the default verification entry point. The concrete primary
        verifiers are supplied when the service is assembled, allowing
        production and test configurations to use different verifier sets.

        :param store: Store candidate to verify.
        :return: Combined primary verification result.
        """
        return self.verify_primary(store)

    def verify_primary(
        self,
        store: StoreCandidate,
    ) -> StoreVerificationResult:
        """
        Run all configured primary verifiers.

        A candidate is verified only when every configured primary verifier
        succeeds. Issues are merged by issue name, and the lowest verifier
        confidence is used as the combined confidence score.

        :param store: Store candidate to verify.
        :return: Combined primary verification result.
        """
        results = [
            verifier.verify(store)
            for verifier in self.primary_verifiers
        ]

        return self._combine_results(
            store=store,
            results=results,
        )

    def verify_secondary(
        self,
        store: StoreCandidate,
        candidate_issues: Sequence[DetectedIssue],
    ) -> StoreVerificationResult:
        """
        Run the configured optional secondary verifiers.

        Secondary verification enhances the baseline verification result with
        deeper or more expensive checks, such as LLM-backed issue detection.
        It does not perform repair and does not assume that repair has occurred.

        :param store: Store candidate to evaluate.
        :param candidate_issues: Issues identified by primary verification and
            supplied as context for enhanced verification.
        :return: Combined secondary verification result.
        :raises ValueError: If no secondary verifier is configured.
        """
        if not self.secondary_verifiers:
            raise ValueError(
                "secondary_verifiers are not configured"
            )

        request = self._build_secondary_request(
            store=store,
            candidate_issues=candidate_issues,
        )

        results = [
            verifier.verify(request)
            for verifier in self.secondary_verifiers
        ]

        return self._combine_results(
            store=store,
            results=results,
        )

    def verify_enhanced(
        self,
        store: StoreCandidate,
    ) -> StoreVerificationResult:
        """
        Run primary verification with optional secondary enhancement.

        Primary verification always runs. When secondary verifiers are
        configured, they receive the primary issues as additional context and
        their results are combined with the primary result.

        :param store: Store candidate to verify.
        :return: Combined verification result across configured verification layers.
        """
        primary_result = self.verify_primary(store)

        if not self.secondary_verifiers:
            return primary_result

        secondary_result = self.verify_secondary(
            store=store,
            candidate_issues=primary_result.issues,
        )

        return self._combine_results(
            store=store,
            results=[
                primary_result,
                secondary_result,
            ],
        )

    def _combine_results(
        self,
        store: StoreCandidate,
        results: Sequence[StoreVerificationResult],
    ) -> StoreVerificationResult:
        """
        Combine verifier results into one verification result.

        :param store: Store candidate associated with the verification results.
        :param results: Verification results to combine.
        :return: Combined verification result.
        """
        issues = merge_issues(
            *(result.issues for result in results)
        )
        verified = all(
            result.verified
            for result in results
        )
        confidence_score = min(
            (
                result.confidence_score
                for result in results
            ),
            default=0.0,
        )

        return build_verification_result(
            store,
            verified=verified,
            confidence_score=confidence_score,
            issues=issues,
        )

    def _build_secondary_request(
        self,
        store: StoreCandidate,
        candidate_issues: Sequence[DetectedIssue],
    ) -> DetectRemainingIssuesInput:
        """
        Build the enhanced-verification request payload.

        :param store: Store candidate being evaluated.
        :param candidate_issues: Primary issues supplied as secondary-verifier context.
        :return: Structured input for secondary verifiers.
        """
        return DetectRemainingIssuesInput(
            store_location=self._serialize_store_candidate(
                store
            ),
            candidate_issues=[
                CandidateIssue(
                    name=issue.name,
                    description=issue.description,
                )
                for issue in candidate_issues
            ],
        )

    @staticmethod
    def _serialize_store_candidate(
        store: StoreCandidate,
    ) -> dict[str, object]:
        """
        Serialize candidate fields used by enhanced verification.

        :param store: Store candidate to serialize.
        :return: Dictionary representation used by secondary verifier contracts.
        """
        return {
            "canonical_store_id": store.canonical_store_id,
            "retailer": store.retailer,
            "retailer_store_id": store.retailer_store_id,
            "retailer_key": store.retailer_key,
            "store_name": store.store_name,
            "address": store.address,
            "full_address": store.full_address,
            "city": store.city,
            "state": store.state,
            "zip_code": store.zip_code,
            "latitude": store.latitude,
            "longitude": store.longitude,
            "geocode_source": store.geocode_source,
            "geocode_confidence": store.geocode_confidence,
            "osm_id": store.osm_id,
            "locator_type": store.locator_type,
            "locator_name": store.locator_name,
            "show_on_map": store.show_on_map,
            "distance_meters": store.distance_meters,
        }


__all__ = ["StoreLocationVerificationService"]