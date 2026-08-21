# services/store_service/patchers/patch_strategies/locator/osm_patch_strategy.py

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace

from services.llm_service.prompts.store_prompts.repair_issues.contract import RepairChange
from services.store_service.capabilities.store_info_normalization.store_info_normalization_service import (
    StoreInfoNormalizationService,
)
from services.store_service.capabilities.store_location_resolution.protocols import (
    StoreLocatorProtocol,
)
from services.store_service.models.base import (
    DetectedIssue,
    FlyerDeal,
    StoreCandidate,
)
from services.store_service.models.store_location_issues import ISSUE_TYPES
from services.store_service.capabilities.store_location_verification.verification_helper import (
    as_float,
    is_valid_coordinate,
    run_async,
    token_overlap_score,
)
from services.geocoding_service import is_us_coordinate


class OSMPatchStrategy:
    """
    Apply locator-backed repair using OpenStreetMap / Nominatim evidence.

    The strategy queries an injected locator, filters obviously invalid
    candidates, chooses the nearest valid result, and copies only fields
    explicitly listed in the issue repair metadata.

    Locator repairs remain conservative: confidence is left unset and manual
    review is always required when a locator repair is attempted.
    """

    def __init__(
        self,
        locator: StoreLocatorProtocol,
        max_search_distance_meters: float = 25000.0,
        normalizer: StoreInfoNormalizationService | None = None,
    ) -> None:
        """
        Initialize the OSM repair strategy.

        :param locator: Locator used to retrieve external store candidates.
        :param max_search_distance_meters: Maximum accepted distance from the
            current store context.
        :param normalizer: Optional shared store-info normalizer used for
            retailer-key derivation.
        """
        self.locator = locator
        self.max_search_distance_meters = max_search_distance_meters
        self.normalizer = (
            normalizer
            or StoreInfoNormalizationService()
        )

    def patch(
        self,
        candidate: StoreCandidate,
        issues: Sequence[DetectedIssue],
    ) -> tuple[StoreCandidate, list[RepairChange], float | None, bool]:
        """
        Repair routed issues using an external locator.

        :param candidate: Store candidate to repair.
        :param issues: Issues whose repair fields may be sourced from locator
            evidence.
        :return: Updated candidate, repair changes, no confidence score, and
            manual-review status.
        """
        if not issues:
            return candidate, [], None, False

        deal = self._build_deal(candidate)
        if deal is None:
            return candidate, [], None, True

        try:
            candidates = run_async(
                self.locator.find_candidate_stores(deal)
            )
        except Exception:
            return candidate, [], None, True

        valid_candidates = self._filter_valid_candidates(
            candidate,
            candidates,
        )
        if not valid_candidates:
            return candidate, [], None, True

        best_candidate = min(
            valid_candidates,
            key=lambda item: (
                float(item.distance_meters or 0.0),
                int(item.canonical_store_id),
            ),
        )

        updated_candidate, changes = self._build_repair(
            current=candidate,
            best=best_candidate,
            issues=issues,
        )

        if not changes:
            return candidate, [], None, True

        return updated_candidate, changes, None, True

    def _build_deal(
        self,
        candidate: StoreCandidate,
    ) -> FlyerDeal | None:
        """
        Build the minimal location context required by the locator.

        :param candidate: Current store candidate.
        :return: Flyer deal context, or None when retailer/ZIP context is missing.
        """
        retailer_raw = (
            (candidate.retailer_key or "").strip()
            or (candidate.retailer or "").strip()
        )
        if not retailer_raw:
            return None

        retailer_key = (
            self.normalizer.normalize_retailer_key(
                retailer_raw
            )
            or self.normalizer.make_retailer_key(
                retailer_raw
            )
        )
        if not retailer_key:
            return None

        zip_code = self._extract_zip_code(
            candidate.zip_code,
            candidate.full_address,
            candidate.address,
        )
        if not zip_code:
            return None

        coords = self._usable_coordinates(candidate)

        return FlyerDeal(
            id=abs(int(candidate.canonical_store_id)) or 1,
            retailer=candidate.retailer or retailer_raw,
            retailer_key=retailer_key,
            zip_code=zip_code,
            city=candidate.city,
            state=candidate.state,
            retailer_address=(
                candidate.full_address
                or candidate.address
            ),
            store_lat=coords[0] if coords else None,
            store_lng=coords[1] if coords else None,
        )

    def _filter_valid_candidates(
        self,
        current: StoreCandidate,
        candidates: Sequence[StoreCandidate],
    ) -> list[StoreCandidate]:
        """
        Filter and sort locator candidates for repair use.

        :param current: Candidate being repaired.
        :param candidates: External candidates returned by the locator.
        :return: Valid candidates ordered by distance and stable ID.
        """
        filtered = [
            candidate
            for candidate in candidates
            if self._is_valid_candidate(
                current,
                candidate,
            )
        ]

        filtered.sort(
            key=lambda item: (
                float(item.distance_meters or 0.0),
                int(item.canonical_store_id),
            )
        )
        return filtered

    def _is_valid_candidate(
        self,
        current: StoreCandidate,
        candidate: StoreCandidate,
    ) -> bool:
        """
        Check whether a locator candidate is safe to use for repair.

        :param current: Candidate currently being repaired.
        :param candidate: External candidate under evaluation.
        :return: True when the candidate passes coordinate, distance, and
            basic retailer checks.
        """
        lat = as_float(candidate.latitude)
        lng = as_float(candidate.longitude)

        if lat is None or lng is None:
            return False
        if not is_valid_coordinate(lat, lng):
            return False
        if lat == 0.0 and lng == 0.0:
            return False
        if not is_us_coordinate(lat, lng):
            return False

        if candidate.distance_meters is None:
            return False

        if (
            float(candidate.distance_meters or 0.0)
            > self.max_search_distance_meters
        ):
            return False

        retailer_hint = self._retailer_hint(current)
        if retailer_hint and candidate.store_name:
            if (
                token_overlap_score(
                    retailer_hint,
                    candidate.store_name,
                )
                == 0.0
            ):
                return False

        return True

    def _build_repair(
        self,
        current: StoreCandidate,
        best: StoreCandidate,
        issues: Sequence[DetectedIssue],
    ) -> tuple[StoreCandidate, list[RepairChange]]:
        """
        Copy issue-authorized fields from the selected locator candidate.

        :param current: Candidate being repaired.
        :param best: Selected external locator candidate.
        :param issues: Issues whose repair metadata controls which fields may change.
        :return: Updated candidate and generated repair changes.
        """
        updates: dict[str, object] = {}
        changes: list[RepairChange] = []

        for issue in issues:
            issue_type = ISSUE_TYPES.get(issue.name)
            if issue_type is None:
                continue

            for field_name in issue_type.repair_fields:
                if (
                    not hasattr(current, field_name)
                    or not hasattr(best, field_name)
                ):
                    continue

                before = getattr(
                    current,
                    field_name,
                )
                after = getattr(
                    best,
                    field_name,
                )

                if after is None:
                    continue

                if not self._value_changed(
                    field_name,
                    before,
                    after,
                ):
                    continue

                updates[field_name] = after
                changes.append(
                    RepairChange(
                        issue=issue.name,
                        fields=[field_name],
                        before=before,
                        after=after,
                        confidence=0.0,
                        repaired_by="locator",
                    )
                )

        if not updates:
            return current, []

        return replace(
            current,
            **updates,
        ), changes

    def _usable_coordinates(
        self,
        candidate: StoreCandidate,
    ) -> tuple[float, float] | None:
        """
        Return usable candidate coordinates for locator search context.

        :param candidate: Store candidate containing optional coordinates.
        :return: Valid US coordinate pair, or None when coordinates are unusable.
        """
        lat = as_float(candidate.latitude)
        lng = as_float(candidate.longitude)

        if lat is None or lng is None:
            return None
        if not is_valid_coordinate(lat, lng):
            return None
        if lat == 0.0 and lng == 0.0:
            return None
        if not is_us_coordinate(lat, lng):
            return None

        return lat, lng

    def _retailer_hint(
        self,
        candidate: StoreCandidate,
    ) -> str:
        """
        Derive a normalized retailer hint for candidate filtering.

        :param candidate: Current store candidate.
        :return: Retailer lookup hint used for basic external-result filtering.
        """
        retailer_raw = (
            (candidate.retailer_key or "").strip()
            or (candidate.retailer or "").strip()
        )
        if not retailer_raw:
            return ""

        return (
            self.normalizer.normalize_retailer_key(
                retailer_raw
            )
            or self.normalizer.make_retailer_key(
                retailer_raw
            )
            or retailer_raw
        )

    @staticmethod
    def _extract_zip_code(
        zip_code: str | None,
        full_address: str | None,
        address: str | None,
    ) -> str | None:
        """
        Extract a five-digit ZIP code from candidate location fields.

        :param zip_code: Explicit ZIP field.
        :param full_address: Full address fallback.
        :param address: Address fallback.
        :return: Five-digit ZIP code, or None when no ZIP can be found.
        """
        for value in [
            (zip_code or "").strip(),
            full_address or "",
            address or "",
        ]:
            if not value:
                continue

            match = re.search(
                r"(\d{5})(?:-\d{4})?",
                value,
            )
            if match:
                return match.group(1)

        return None

    @staticmethod
    def _value_changed(
        field_name: str,
        before: object,
        after: object,
    ) -> bool:
        """
        Compare two field values using field-appropriate normalization.

        :param field_name: Candidate field being compared.
        :param before: Original value.
        :param after: Proposed repaired value.
        :return: True when the values represent a meaningful change.
        """
        if before is None and after is None:
            return False

        if field_name in {
            "latitude",
            "longitude",
            "distance_meters",
        }:
            before_float = as_float(before)
            after_float = as_float(after)

            if before_float is None or after_float is None:
                return before_float != after_float

            return abs(
                before_float - after_float
            ) > 1e-9

        def normalize(value: object) -> str:
            if value is None:
                return ""

            if isinstance(value, str):
                return " ".join(
                    value.strip().split()
                ).lower()

            return " ".join(
                str(value).strip().split()
            ).lower()

        return normalize(before) != normalize(after)


__all__ = ["OSMPatchStrategy"]