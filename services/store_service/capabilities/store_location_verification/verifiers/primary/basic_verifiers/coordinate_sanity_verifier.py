#  services/store_service/capabilities/store_location_verification/verifiers/primary/basic_verifiers/coordinate_sanity_verifier.py

from __future__ import annotations

import math

from services.geocoding_service import is_us_coordinate
from services.store_service.capabilities.store_location_verification.verification_helper import (
    as_float,
    build_verification_result,
    confidence_from_issue_count,
    is_valid_coordinate,
)
from services.store_service.models.base import (
    DetectedIssue,
    StoreCandidate,
)
from services.store_service.capabilities.store_location_verification.models import (
    StoreVerificationResult,
)
from services.store_service.models.store_location_issues import (
    INVALID_COORDINATES,
    MISSING_COORDINATES,
    NON_US_COORDINATES,
    ZERO_COORDINATES,
)


class CoordinateSanityVerifier:
    """
    Verify that store coordinates are present, valid, and within the US.

    This verifier performs deterministic coordinate checks only. It does not
    attempt to repair invalid coordinates.
    """

    name = "coordinate_sanity_verifier"

    def verify(
        self,
        store: StoreCandidate,
    ) -> StoreVerificationResult:
        """
        Verify a store's coordinate values.

        :param store: Store candidate to verify.
        :return: Verification result containing coordinate-related issues.
        """
        issues: list[DetectedIssue] = []

        lat = as_float(store.latitude)
        lng = as_float(store.longitude)

        if lat is None or lng is None:
            issues.append(
                DetectedIssue(
                    name=MISSING_COORDINATES.name,
                    description=MISSING_COORDINATES.description,
                )
            )
        elif not math.isfinite(lat) or not math.isfinite(lng):
            issues.append(
                DetectedIssue(
                    name=INVALID_COORDINATES.name,
                    description=INVALID_COORDINATES.description,
                )
            )
        elif lat == 0.0 and lng == 0.0:
            issues.append(
                DetectedIssue(
                    name=ZERO_COORDINATES.name,
                    description=ZERO_COORDINATES.description,
                )
            )
        elif not is_valid_coordinate(lat, lng):
            issues.append(
                DetectedIssue(
                    name=INVALID_COORDINATES.name,
                    description=INVALID_COORDINATES.description,
                )
            )
        elif not is_us_coordinate(lat, lng):
            issues.append(
                DetectedIssue(
                    name=NON_US_COORDINATES.name,
                    description=NON_US_COORDINATES.description,
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


__all__ = ["CoordinateSanityVerifier"]