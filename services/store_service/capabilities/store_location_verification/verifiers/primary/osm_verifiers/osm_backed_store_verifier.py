# services/store_service/capabilities/store_location_verification/verifiers/primary/osm_verifiers/osm_backed_store_verifier.py

from __future__ import annotations

from services.store_service.capabilities.store_location_resolution.locators.external.osm_store_locator import (
    OSMStoreLocator,
)
from services.store_service.capabilities.store_location_verification.models import (
    StoreVerificationResult,
)
from services.store_service.capabilities.store_location_verification.verification_helper import (
    address_similarity,
    as_float,
    build_verification_result,
    haversine_meters,
    run_async,
)
from services.store_service.models.base import (
    DetectedIssue,
    FlyerDeal,
    StoreCandidate,
)
from services.store_service.models.store_location_issues import (
    ADDRESS_COORDINATE_MISMATCH,
    UNVERIFIABLE,
)


class OSMBackedStoreVerifier:
    """
    Verify a store against an external OpenStreetMap candidate.

    The OSM locator is provided by Store Location Resolution. This verifier
    only evaluates the returned external evidence and never modifies the
    candidate itself.
    """

    name = "osm_backed_store_verifier"

    def __init__(
        self,
        locator: OSMStoreLocator,
        max_coordinate_delta_meters: float = 300.0,
        min_address_similarity: float = 0.70,
    ) -> None:
        """
        Initialize the OSM-backed verifier.

        :param locator: OSM locator used to obtain external store candidates.
        :param max_coordinate_delta_meters: Maximum accepted coordinate
            difference between the stored candidate and the OSM candidate.
        :param min_address_similarity: Minimum address similarity required
            for address-based verification.
        """
        self.locator = locator
        self.max_coordinate_delta_meters = max_coordinate_delta_meters
        self.min_address_similarity = min_address_similarity

    def verify(
        self,
        store: StoreCandidate,
    ) -> StoreVerificationResult:
        """
        Verify a store using OSM-backed evidence.

        :param store: Store candidate to verify.
        :return: Verification result containing OSM-backed evidence.
        """
        retailer_identity = (
            store.retailer_key
            or store.retailer
            or ""
        ).strip()
        zip_code = (store.zip_code or "").strip()

        # OSM verification requires enough retailer and ZIP context to build
        # an independent external lookup.
        if not retailer_identity or not zip_code:
            return build_verification_result(
                store,
                verified=False,
                confidence_score=0.0,
                issues=[
                    DetectedIssue(
                        name=UNVERIFIABLE.name,
                        description=UNVERIFIABLE.description,
                    )
                ],
            )

        # Do not pass the candidate's coordinates to OSM; they may be the
        # values being verified.
        deal = self._build_deal(store)
        candidates = run_async(
            self.locator.find_candidate_stores(deal)
        )

        if not candidates:
            return build_verification_result(
                store,
                verified=False,
                confidence_score=0.0,
                issues=[
                    DetectedIssue(
                        name=UNVERIFIABLE.name,
                        description=UNVERIFIABLE.description,
                    )
                ],
            )

        top = candidates[0]

        address_similarity_score = max(
            address_similarity(
                store.full_address,
                top.full_address,
            ),
            address_similarity(
                store.address,
                top.address,
            ),
            address_similarity(
                store.full_address,
                top.address,
            ),
            address_similarity(
                store.address,
                top.full_address,
            ),
        )

        store_lat = as_float(store.latitude)
        store_lng = as_float(store.longitude)
        top_lat = as_float(top.latitude)
        top_lng = as_float(top.longitude)

        distance_meters = haversine_meters(
            store_lat,
            store_lng,
            top_lat,
            top_lng,
        )
        coords_available = (
            store_lat is not None
            and store_lng is not None
            and top_lat is not None
            and top_lng is not None
        )

        coords_ok = (
            coords_available
            and distance_meters is not None
            and distance_meters <= self.max_coordinate_delta_meters
        )
        address_ok = (
            address_similarity_score
            >= self.min_address_similarity
        )

        # Either strong coordinate agreement or strong address agreement is
        # sufficient to establish external verification.
        verified = coords_ok or address_ok

        issues: list[DetectedIssue] = []
        if not verified:
            issues.append(
                DetectedIssue(
                    name=ADDRESS_COORDINATE_MISMATCH.name,
                    description=ADDRESS_COORDINATE_MISMATCH.description,
                )
            )

        if coords_ok and address_ok:
            confidence_score = 0.95
        elif verified:
            confidence_score = 0.75
        else:
            confidence_score = 0.20

        return build_verification_result(
            store,
            verified=verified,
            confidence_score=confidence_score,
            issues=issues,
            canonical_store_id=top.canonical_store_id,
            retailer_store_id=top.retailer_store_id,
        )

    @staticmethod
    def _build_deal(
        store: StoreCandidate,
    ) -> FlyerDeal:
        """
        Build the minimal deal context used by the OSM locator.

        The candidate's existing coordinates are intentionally not passed to
        the locator so OSM verification does not reuse potentially bad input
        coordinates as its search origin.

        :param store: Store candidate being verified.
        :return: Flyer deal context for the OSM lookup.
        """
        return FlyerDeal(
            id=abs(int(store.canonical_store_id)) or 1,
            retailer=(
                store.retailer
                or store.retailer_key
                or ""
            ),
            retailer_key=store.retailer_key,
            zip_code=(store.zip_code or "").strip(),
            city=store.city,
            state=store.state,
            retailer_address=(
                store.address
                or store.full_address
            ),
            store_lat=None,
            store_lng=None,
        )


__all__ = ["OSMBackedStoreVerifier"]