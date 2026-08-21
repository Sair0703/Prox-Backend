from __future__ import annotations

import logging
import math
import re
from typing import Sequence


from services.store_service.capabilities.store_location_resolution.models import (
    AggregationMatch,
    LocatorAggregationResult,
)
from services.store_service.models.base import StoreCandidate
from services.store_service.models.constants import DIRECTION_ALIASES, STREET_SUFFIX_ALIASES, STATE_ALIASES

logger = logging.getLogger(__name__)


class StoreLocatorAggregator:
    """Merge and deduplicate candidates returned by multiple store locators."""

    def __init__(
        self,
        similarity_threshold: float = 0.82,
    ) -> None:
        """
        Initialize the locator aggregator.

        :param similarity_threshold: Minimum similarity score required to treat
            an external candidate as the same store as an internal candidate.
        """
        self.similarity_threshold = similarity_threshold

    def aggregate(
        self,
        local_candidates: Sequence[StoreCandidate],
        external_candidates: Sequence[StoreCandidate],
    ) -> LocatorAggregationResult:
        """
        Merge local and external locator candidates.

        Internal candidates are deduplicated first. Each external candidate is
        then compared against the best local match. External candidates that
        meet the similarity threshold are dropped in favor of the local record;
        unmatched external candidates remain in the merged result.

        :param local_candidates: Candidates returned by internal locators.
        :param external_candidates: Candidates returned by external locators.
        :return: Aggregated candidates and matching diagnostics.
        """
        local_map = self._dedupe_local(local_candidates)
        local_list = list(local_map.values())

        matched_pairs: list[AggregationMatch] = []
        dropped_external: list[StoreCandidate] = []
        kept_external: list[StoreCandidate] = []

        for external in self._dedupe_external(external_candidates):
            best_local, score, reasons = self._best_local_match(
                external,
                local_list,
            )

            if best_local is not None and score >= self.similarity_threshold:
                matched_pairs.append(
                    AggregationMatch(
                        local_candidate=best_local,
                        external_candidate=external,
                        similarity_score=score,
                        reason_codes=reasons,
                    )
                )
                dropped_external.append(external)

                logger.info(
                    "[LOCATOR_AGG] merged external into local "
                    "local_id=%s external_id=%s score=%.3f reasons=%s",
                    best_local.canonical_store_id,
                    external.canonical_store_id,
                    score,
                    ",".join(reasons),
                )
            else:
                kept_external.append(external)

        # Keep this debug output for the existing resolution diagnostics.
        print("\n========== LocatorAggregator ==========")

        print("\nLocal candidates:")
        for candidate in local_list:
            print(
                f"id={candidate.canonical_store_id}, "
                f"locator={candidate.locator_type}, "
                f"distance={candidate.distance_meters}"
            )

        print("\nKept external:")
        for candidate in kept_external:
            print(
                f"id={candidate.canonical_store_id}, "
                f"locator={candidate.locator_type}, "
                f"distance={candidate.distance_meters}"
            )

        print("\nDropped external:")
        for candidate in dropped_external:
            print(
                f"id={candidate.canonical_store_id}, "
                f"locator={candidate.locator_type}, "
                f"distance={candidate.distance_meters}"
            )

        merged_candidates = local_list + kept_external
        merged_candidates.sort(key=self._candidate_sort_key)

        print("\nMerged candidates:")
        for candidate in merged_candidates:
            print(
                f"id={candidate.canonical_store_id}, "
                f"locator={candidate.locator_type}, "
                f"distance={candidate.distance_meters}"
            )

        print("=======================================\n")

        return LocatorAggregationResult(
            merged_candidates=merged_candidates,
            local_candidates=local_list,
            external_candidates=kept_external,
            matched_pairs=matched_pairs,
            dropped_external_candidates=dropped_external,
        )

    def _best_local_match(
        self,
        external: StoreCandidate,
        locals_: Sequence[StoreCandidate],
    ) -> tuple[StoreCandidate | None, float, list[str]]:
        """
        Find the highest-scoring internal match for an external candidate.

        :param external: External candidate being matched.
        :param locals_: Internal candidates available for comparison.
        :return: Best local candidate, score, and similarity reasons.
        """
        best_local: StoreCandidate | None = None
        best_score = 0.0
        best_reasons: list[str] = []

        for local in locals_:
            score, reasons = self._similarity_score(
                local,
                external,
            )

            if score > best_score:
                best_score = score
                best_local = local
                best_reasons = reasons

        return best_local, best_score, best_reasons

    def _similarity_score(
        self,
        local: StoreCandidate,
        external: StoreCandidate,
    ) -> tuple[float, list[str]]:
        """
        Compute the weighted similarity between two candidates.

        :param local: Internal candidate.
        :param external: External candidate.
        :return: Similarity score and the signals contributing to the score.
        """
        reasons: list[str] = []

        local_address = local.full_address or local.address
        external_address = external.full_address or external.address

        address_score = self._token_prefix_similarity(
            local_address,
            external_address,
        )
        name_score = self._token_prefix_similarity(
            local.store_name,
            external.store_name,
        )

        local_city = self._norm(local.city)
        external_city = self._norm(external.city)
        city_score = (
            1.0
            if local_city and local_city == external_city
            else 0.0
        )

        local_state = self._norm_state(local.state)
        external_state = self._norm_state(external.state)
        state_score = (
            1.0
            if local_state and local_state == external_state
            else 0.0
        )

        zip_score = self._zip_score(
            local.zip_code,
            external.zip_code,
        )
        geo_score = self._geo_score(
            local,
            external,
        )

        if address_score >= 0.70:
            reasons.append("address_match")
        if name_score >= 0.70:
            reasons.append("name_match")
        if city_score >= 1.0:
            reasons.append("city_match")
        if state_score >= 1.0:
            reasons.append("state_match")
        if zip_score >= 0.80:
            reasons.append("zip_match")
        if geo_score >= 0.70:
            reasons.append("geo_match")

        if local_city and external_city and local_city != external_city:
            reasons.append("city_mismatch")

        if local_state and external_state and local_state != external_state:
            reasons.append("state_mismatch")

        score = (
            0.35 * address_score
            + 0.20 * name_score
            + 0.15 * city_score
            + 0.15 * state_score
            + 0.10 * zip_score
            + 0.05 * geo_score
        )

        return max(0.0, min(1.0, score)), reasons

    def _dedupe_local(
        self,
        candidates: Sequence[StoreCandidate],
    ) -> dict[int, StoreCandidate]:
        """
        Deduplicate internal candidates by canonical store ID.

        :param candidates: Internal candidates to deduplicate.
        :return: Best candidate for each canonical store ID.
        """
        deduped: dict[int, StoreCandidate] = {}

        for candidate in candidates:
            existing = deduped.get(
                candidate.canonical_store_id
            )

            if (
                existing is None
                or self._candidate_sort_key(candidate)
                < self._candidate_sort_key(existing)
            ):
                deduped[candidate.canonical_store_id] = candidate

        return deduped

    def _dedupe_external(
        self,
        candidates: Sequence[StoreCandidate],
    ) -> list[StoreCandidate]:
        """
        Deduplicate external candidates by source and identifying fields.

        :param candidates: External candidates to deduplicate.
        :return: Deduplicated external candidates.
        """
        seen: set[str] = set()
        deduped: list[StoreCandidate] = []

        for candidate in candidates:
            key = "|".join(
                [
                    (candidate.locator_name or "").strip().lower(),
                    (candidate.osm_id or "").strip(),
                    (candidate.retailer_store_id or "").strip(),
                    self._norm(candidate.store_name),
                    self._norm(
                        candidate.full_address
                        or candidate.address
                    ),
                ]
            )

            if key in seen:
                continue

            seen.add(key)
            deduped.append(candidate)

        return deduped

    @staticmethod
    def _candidate_sort_key(
        candidate: StoreCandidate,
    ) -> tuple[float, int, int]:
        """Build the candidate ordering key used by resolution."""
        locator_type = (
            candidate.locator_type
            or ""
        ).strip().lower()

        if locator_type == "internal":
            locator_rank = 0
        elif locator_type == "external":
            locator_rank = 1
        else:
            locator_rank = 2

        return (
            float(candidate.distance_meters or 0.0),
            locator_rank,
            int(candidate.canonical_store_id),
        )

    @staticmethod
    def _token_prefix_similarity(
        left: str | None,
        right: str | None,
    ) -> float:
        """Compare two values using token-level prefix similarity."""
        left_tokens = StoreLocatorAggregator._tokens(left)
        right_tokens = StoreLocatorAggregator._tokens(right)

        if not left_tokens or not right_tokens:
            return 0.0

        matched = 0.0
        used: set[int] = set()

        for token in left_tokens:
            best = 0.0
            best_idx = None

            for i, other in enumerate(right_tokens):
                if i in used:
                    continue

                score = StoreLocatorAggregator._prefix_score(
                    token,
                    other,
                )

                if score > best:
                    best = score
                    best_idx = i

            if best_idx is not None and best > 0:
                used.add(best_idx)
                matched += best

        return matched / max(
            len(left_tokens),
            len(right_tokens),
        )

    @staticmethod
    def _prefix_score(
        a: str,
        b: str,
    ) -> float:
        """Score two normalized tokens using exact, alias, and prefix matches."""
        if a == b:
            return 1.0

        a_norm = StoreLocatorAggregator._normalize_token(a)
        b_norm = StoreLocatorAggregator._normalize_token(b)

        if not a_norm or not b_norm:
            return 0.0

        if a_norm == b_norm:
            return 1.0

        if (
            DIRECTION_ALIASES.get(a_norm) == DIRECTION_ALIASES.get(b_norm)
            and a_norm in DIRECTION_ALIASES
            and b_norm in DIRECTION_ALIASES
        ):
            return 1.0

        if (
            STREET_SUFFIX_ALIASES.get(a_norm)
            == STREET_SUFFIX_ALIASES.get(b_norm)
            and a_norm in STREET_SUFFIX_ALIASES
            and b_norm in STREET_SUFFIX_ALIASES
        ):
            return 1.0

        if len(a_norm) < 2 or len(b_norm) < 2:
            return 0.0

        if a_norm.startswith(b_norm) or b_norm.startswith(a_norm):
            return (
                min(len(a_norm), len(b_norm))
                / max(len(a_norm), len(b_norm))
            )

        return 0.0

    @staticmethod
    def _zip_score(
        left: str | None,
        right: str | None,
    ) -> float:
        """Score ZIP codes by exact five-digit or shared three-digit prefix."""
        left_zip = StoreLocatorAggregator._zip5(left)
        right_zip = StoreLocatorAggregator._zip5(right)

        if not left_zip or not right_zip:
            return 0.0

        if left_zip == right_zip:
            return 1.0

        if left_zip[:3] == right_zip[:3]:
            return 0.5

        return 0.0

    @staticmethod
    def _geo_score(
        local: StoreCandidate,
        external: StoreCandidate,
    ) -> float:
        """Score candidate proximity using geographic distance."""
        if (
            local.latitude is None
            or local.longitude is None
            or external.latitude is None
            or external.longitude is None
        ):
            return 0.0

        distance_meters = StoreLocatorAggregator._haversine_meters(
            local.latitude,
            local.longitude,
            external.latitude,
            external.longitude,
        )

        if distance_meters is None:
            return 0.0

        if distance_meters <= 25:
            return 1.0
        if distance_meters <= 75:
            return 0.85
        if distance_meters <= 150:
            return 0.70
        if distance_meters <= 300:
            return 0.45
        if distance_meters <= 1000:
            return 0.15

        return 0.0

    @staticmethod
    def _haversine_meters(
        lat1: float,
        lng1: float,
        lat2: float,
        lng2: float,
    ) -> float | None:
        """Calculate the Haversine distance between two coordinates."""
        try:
            radius = 6371000.0
            point1 = math.radians(lat1)
            point2 = math.radians(lat2)
            delta_lat = math.radians(lat2 - lat1)
            delta_lng = math.radians(lng2 - lng1)

            a = (
                math.sin(delta_lat / 2.0) ** 2
                + math.cos(point1)
                * math.cos(point2)
                * math.sin(delta_lng / 2.0) ** 2
            )
            c = 2.0 * math.atan2(
                math.sqrt(a),
                math.sqrt(1.0 - a),
            )
            return radius * c
        except Exception:
            return None

    @staticmethod
    def _tokens(
        text: str | None,
    ) -> list[str]:
        """Split text into normalized alphanumeric tokens."""
        if not text:
            return []

        text = re.sub(
            r"[^A-Z0-9]+",
            " ",
            text.upper(),
        )
        return [
            token
            for token in text.split()
            if token
        ]

    @staticmethod
    def _normalize_token(
        token: str | None,
    ) -> str:
        """Normalize an individual comparison token."""
        if not token:
            return ""

        return re.sub(
            r"[^A-Z0-9]+",
            "",
            token.upper(),
        )

    @staticmethod
    def _norm(
        text: str | None,
    ) -> str:
        """Normalize text for equality comparisons."""
        return " ".join(
            StoreLocatorAggregator._tokens(text)
        ).strip().lower()

    @staticmethod
    def _zip5(
        text: str | None,
    ) -> str | None:
        """Extract the first five-digit ZIP code from text."""
        if not text:
            return None

        match = re.search(
            r"(\d{5})",
            str(text),
        )
        return match.group(1) if match else None

    @staticmethod
    def _norm_state(
        state: str | None,
    ) -> str | None:
        """Normalize a state name or abbreviation using shared aliases."""
        if not state:
            return None

        normalized = StoreLocatorAggregator._norm(state)

        return STATE_ALIASES.get(
            normalized,
            STATE_ALIASES.get(
                normalized.replace(" ", ""),
                normalized[:2].upper()
                if len(normalized) >= 2
                else None,
            ),
        )


__all__ = ["StoreLocatorAggregator"]