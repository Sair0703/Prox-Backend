# services/store_service/capabilities/store_location_verification/verification_helper.py

from __future__ import annotations

import asyncio
import math
import re
from difflib import SequenceMatcher
from typing import Any, Coroutine, Sequence, TypeVar

from services.store_service.capabilities.store_location_verification.models import (
    StoreVerificationResult,
)
from services.store_service.models.base import (
    DetectedIssue,
    StoreCandidate,
)

T = TypeVar("T")


def run_async(
    coroutine: Coroutine[Any, Any, T],
) -> T:
    """
    Run a coroutine to completion from a synchronous verification flow.

    :param coroutine: Coroutine to execute.
    :return: Result produced by the coroutine.
    """
    try:
        return asyncio.run(coroutine)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coroutine)
        finally:
            loop.close()


def normalize_text(
    value: str | None,
) -> str:
    """
    Normalize text for stable case-insensitive comparison.

    :param value: Text value to normalize.
    :return: Lowercase text with repeated whitespace collapsed.
    """
    if not value:
        return ""

    return " ".join(
        value.strip().lower().split()
    )


def as_float(
    value: Any,
) -> float | None:
    """
    Convert a value to float when possible.

    :param value: Value to convert.
    :return: Float representation, or None when conversion is not possible.
    """
    if value is None:
        return None

    try:
        return float(value)
    except Exception:
        return None


def is_valid_coordinate(
    lat: float | None,
    lng: float | None,
) -> bool:
    """
    Check whether latitude and longitude are syntactically valid.

    :param lat: Latitude value.
    :param lng: Longitude value.
    :return: True when both values fall within valid coordinate ranges.
    """
    if lat is None or lng is None:
        return False

    return (
        -90.0 <= lat <= 90.0
        and -180.0 <= lng <= 180.0
    )


def haversine_meters(
    lat1: float | None,
    lng1: float | None,
    lat2: float | None,
    lng2: float | None,
) -> float | None:
    """
    Calculate the Haversine distance between two coordinates.

    :param lat1: First latitude.
    :param lng1: First longitude.
    :param lat2: Second latitude.
    :param lng2: Second longitude.
    :return: Distance in meters, or None when either coordinate is invalid.
    """
    if (
        lat1 is None
        or lng1 is None
        or lat2 is None
        or lng2 is None
        or not is_valid_coordinate(lat1, lng1)
        or not is_valid_coordinate(lat2, lng2)
    ):
        return None

    radius = 6_371_000.0

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lng2 - lng1)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1)
        * math.cos(phi2)
        * math.sin(delta_lambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(
        math.sqrt(a),
        math.sqrt(1.0 - a),
    )

    return radius * c


def address_similarity(
    left: str | None,
    right: str | None,
) -> float:
    """
    Calculate normalized string similarity between two addresses.

    :param left: First address value.
    :param right: Second address value.
    :return: Similarity score in the range [0.0, 1.0].
    """
    return SequenceMatcher(
        None,
        normalize_text(left),
        normalize_text(right),
    ).ratio()


def confidence_from_issue_count(
    issue_count: int,
    *,
    floor: float = 0.0,
    penalty: float = 0.15,
) -> float:
    """
    Convert an issue count into a deterministic confidence score.

    :param issue_count: Number of detected verification issues.
    :param floor: Minimum confidence score that may be returned.
    :param penalty: Confidence reduction applied for each issue.
    :return: Confidence score in the configured range.
    """
    if issue_count <= 0:
        return 1.0

    return max(
        floor,
        1.0 - (issue_count * penalty),
    )


def build_verification_result(
    store: StoreCandidate,
    *,
    verified: bool,
    confidence_score: float,
    issues: Sequence[DetectedIssue] | None = None,
    canonical_store_id: int | None = None,
    retailer_store_id: str | None = None,
) -> StoreVerificationResult:
    """
    Build a StoreVerificationResult from a store candidate and verifier output.

    Candidate identifiers are preserved unless explicit replacement identifiers
    are supplied by the verifier.

    :param store: Store candidate associated with the verification result.
    :param verified: Whether the candidate passed verification.
    :param confidence_score: Confidence assigned to the verification decision.
    :param issues: Issues detected during verification.
    :param canonical_store_id: Optional canonical store ID override.
    :param retailer_store_id: Optional retailer-specific store ID override.
    :return: Constructed store verification result.
    """
    return StoreVerificationResult(
        verified=verified,
        confidence_score=confidence_score,
        issues=list(issues or []),
        canonical_store_id=(
            canonical_store_id
            if canonical_store_id is not None
            else store.canonical_store_id
        ),
        retailer_store_id=(
            retailer_store_id
            if retailer_store_id is not None
            else store.retailer_store_id
        ),
    )


def normalize_address_tokens(
    value: str | None,
) -> str:
    """
    Normalize address-like text for token-based comparison.

    :param value: Address-like text to normalize.
    :return: Lowercase alphanumeric text with punctuation removed and
        whitespace collapsed.
    """
    if not value:
        return ""

    value = value.lower().replace(
        "&",
        " and ",
    )
    value = re.sub(
        r"[^a-z0-9\s]+",
        " ",
        value,
    )

    return " ".join(value.split())


def token_overlap_score(
    left: str | None,
    right: str | None,
) -> float:
    """
    Calculate token overlap between two normalized text values.

    :param left: First text value.
    :param right: Second text value.
    :return: Jaccard token-overlap score in the range [0.0, 1.0].
    """
    left_tokens = set(
        normalize_address_tokens(left).split()
    )
    right_tokens = set(
        normalize_address_tokens(right).split()
    )

    if not left_tokens or not right_tokens:
        return 0.0

    intersection = len(
        left_tokens & right_tokens
    )
    union = len(
        left_tokens | right_tokens
    )

    return (
        intersection / union
        if union
        else 0.0
    )


def merge_issues(
    *issue_groups: Sequence[DetectedIssue] | None,
) -> list[DetectedIssue]:
    """
    Merge detected issues while preserving order and removing duplicates.

    Issues with the same issue name are treated as duplicates.

    :param issue_groups: Issue collections produced by one or more verifiers.
    :return: Deduplicated issues in first-seen order.
    """
    merged: list[DetectedIssue] = []
    seen: set[str] = set()

    for group in issue_groups:
        if not group:
            continue

        for issue in group:
            if issue.name in seen:
                continue

            seen.add(issue.name)
            merged.append(issue)

    return merged


__all__ = [
    "address_similarity",
    "as_float",
    "build_verification_result",
    "confidence_from_issue_count",
    "haversine_meters",
    "is_valid_coordinate",
    "merge_issues",
    "normalize_address_tokens",
    "normalize_text",
    "run_async",
    "token_overlap_score",
]