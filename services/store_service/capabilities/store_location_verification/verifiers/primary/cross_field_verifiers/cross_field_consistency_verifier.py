# services/store_service/capabilities/store_location_verification/verifiers/primary/cross_field_verifiers/cross_field_consistency_verifier.py

from __future__ import annotations

from services.store_service.capabilities.store_location_verification.models import (
    StoreVerificationResult,
)
from services.store_service.capabilities.store_location_verification.verification_helper import (
    as_float,
    build_verification_result,
    confidence_from_issue_count,
    haversine_meters,
    normalize_address_tokens,
)
from services.store_service.geocoders.geocoder import Geocoder, geocode_address
from services.store_service.models.base import (
    DetectedIssue,
    StoreCandidate,
)
from services.store_service.models.store_location_issues import (
    ADDRESS_CITY_MISMATCH,
    ADDRESS_COORDINATE_MISMATCH,
    CITY_STATE_MISMATCH,
    FULL_ADDRESS_PARSE_FAILURE,
    ZIP_STATE_MISMATCH,
)


class CrossFieldConsistencyVerifier:
    """
    Verify consistency among address, location, and coordinate fields.

    When a geocoder is configured, the verifier also compares stored
    coordinates with coordinates derived from the supplied address.
    """

    name = "cross_field_consistency_verifier"

    def __init__(
        self,
        geocoder: Geocoder | None = None,
        max_coordinate_delta_meters: float = 300.0,
    ) -> None:
        """
        Initialize the cross-field consistency verifier.

        :param geocoder: Optional geocoder used to validate stored coordinates
            against the provided address.
        :param max_coordinate_delta_meters: Maximum accepted distance between
            stored coordinates and geocoded address coordinates.
        """
        self.geocoder = geocoder
        self.max_coordinate_delta_meters = max_coordinate_delta_meters

    def verify(
        self,
        store: StoreCandidate,
    ) -> StoreVerificationResult:
        """
        Verify consistency across address and coordinate fields.

        :param store: Store candidate to verify.
        :return: Verification result containing consistency issues.
        """
        issues: list[DetectedIssue] = []

        full_address = (store.full_address or "").strip()
        address = (store.address or "").strip()
        city = (store.city or "").strip()
        state = (store.state or "").strip()
        zip_code = (store.zip_code or "").strip()

        full_address_norm = normalize_address_tokens(full_address)
        address_norm = normalize_address_tokens(address)
        city_norm = normalize_address_tokens(city)
        state_norm = normalize_address_tokens(state)

        if full_address:
            parts = [
                part.strip()
                for part in full_address.split(",")
                if part.strip()
            ]
            if len(parts) < 2:
                issues.append(
                    DetectedIssue(
                        name=FULL_ADDRESS_PARSE_FAILURE.name,
                        description=FULL_ADDRESS_PARSE_FAILURE.description,
                    )
                )

        if city and full_address:
            if city_norm not in full_address_norm and city_norm not in address_norm:
                issues.append(
                    DetectedIssue(
                        name=ADDRESS_CITY_MISMATCH.name,
                        description=ADDRESS_CITY_MISMATCH.description,
                    )
                )

        if state and full_address:
            if state_norm not in full_address_norm:
                issues.append(
                    DetectedIssue(
                        name=CITY_STATE_MISMATCH.name,
                        description=CITY_STATE_MISMATCH.description,
                    )
                )

        if zip_code and full_address:
            if zip_code not in full_address:
                issues.append(
                    DetectedIssue(
                        name=ZIP_STATE_MISMATCH.name,
                        description=ZIP_STATE_MISMATCH.description,
                    )
                )

        lat = as_float(store.latitude)
        lng = as_float(store.longitude)

        if (
            self.geocoder
            and (address or full_address)
            and lat is not None
            and lng is not None
        ):
            query = full_address or ", ".join(
                part
                for part in [address, city, state, zip_code]
                if part
            )

            geocoded = geocode_address(
                address=query,
                zip_code=zip_code or None,
                city=city or None,
                state=state or None,
                geocoder=self.geocoder,
            )

            if (
                geocoded
                and geocoded.get("lat") is not None
                and geocoded.get("lng") is not None
            ):
                distance_meters = haversine_meters(
                    lat,
                    lng,
                    float(geocoded["lat"]),
                    float(geocoded["lng"]),
                )

                if (
                    distance_meters is not None
                    and distance_meters > self.max_coordinate_delta_meters
                ):
                    issues.append(
                        DetectedIssue(
                            name=ADDRESS_COORDINATE_MISMATCH.name,
                            description=ADDRESS_COORDINATE_MISMATCH.description,
                        )
                    )

        # A candidate passes only when no cross-field inconsistencies are found.
        return build_verification_result(
            store,
            verified=not issues,
            confidence_score=confidence_from_issue_count(
                len(issues)
            ),
            issues=issues,
        )


__all__ = ["CrossFieldConsistencyVerifier"]