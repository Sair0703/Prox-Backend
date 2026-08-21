# services/store_service/capabilities/store_location_repair/store_location_repair_service.py

from __future__ import annotations

from collections.abc import Sequence

from services.llm_service.prompts.store_prompts.repair_issues.contract import RepairChange
from services.store_service.capabilities.store_location_repair.models import (
    StoreRepairResult,
)
from services.store_service.capabilities.store_location_repair.patchers.auto_patcher import (
    AutoPatcher,
)
from services.store_service.capabilities.store_location_repair.patchers.llm_patcher import (
    LLMPatcher,
)
from services.store_service.capabilities.store_location_repair.patchers.locator_pacther import (
    LocatorPatcher,
)
from services.store_service.models.base import (
    DetectedIssue,
    StoreCandidate,
)
from services.store_service.models.store_location_issues import (
    ISSUE_TYPES,
)

CONFIDENCE_THRESHOLD = 0.80

ISSUE_WEIGHTS = {
    "missing_address": 1.00,
    "missing_full_address": 0.95,
    "missing_coordinates": 1.00,
    "invalid_coordinates": 1.00,
    "zero_coordinates": 1.00,
    "address_coordinate_mismatch": 1.00,
    "non_us_coordinates": 0.90,
    "implausible_address": 0.90,
    "ambiguous_retailer_identity": 0.90,
    "store_identity_conflict": 1.00,
    "missing_city": 0.80,
    "missing_state": 0.80,
    "address_city_mismatch": 0.85,
    "city_state_mismatch": 0.75,
    "zip_state_mismatch": 0.75,
    "full_address_parse_failure": 0.60,
    "missing_store_id": 0.25,
}


class StoreLocationRepairService:
    """
    Repair store candidates from verification issues.

    The service owns issue routing and repair-strategy orchestration. Concrete
    patchers perform the actual mutation and return a new StoreCandidate.

    Repair does not perform verification, correction orchestration, or backfill.
    Those responsibilities belong to downstream or higher-level workflows.
    """

    def __init__(
        self,
        auto_patcher: AutoPatcher | None = None,
        llm_patcher: LLMPatcher | None = None,
        locator_patcher: LocatorPatcher | None = None,
        *,
        confidence_threshold: float = CONFIDENCE_THRESHOLD,
    ) -> None:
        """
        Initialize the store-location repair service.

        :param auto_patcher: Deterministic patcher for auto-routable issues.
        :param llm_patcher: Optional semantic LLM patcher.
        :param locator_patcher: Optional locator-backed fallback patcher.
        :param confidence_threshold: Minimum LLM-derived confidence required
            to avoid automatic manual-review escalation.
        """
        self.auto_patcher = auto_patcher or AutoPatcher()
        self.llm_patcher = llm_patcher
        self.locator_patcher = locator_patcher
        self.confidence_threshold = confidence_threshold

    def repair(
        self,
        candidate: StoreCandidate,
        issues: Sequence[DetectedIssue],
    ) -> StoreRepairResult:
        """
        Repair a candidate using the configured strategy chain.

        Routing is driven by ISSUE_TYPES. Auto issues are patched first. Remaining
        issues are sent to the configured LLM patcher when available; otherwise
        the locator patcher is used as a conservative fallback.

        :param candidate: Store candidate to repair.
        :param issues: Verification issues that require repair.
        :return: Repaired candidate and repair diagnostics.
        """
        if not issues:
            return StoreRepairResult(
                original_candidate=candidate,
                repaired_candidate=candidate,
                repair_changes=[],
                repair_confidence=1.0,
                requires_manual_review=False,
            )

        auto_issues, routed_issues, requires_manual_review = (
            self._partition_issues(
                issues,
                llm_enabled=self.llm_patcher is not None,
            )
        )

        current, auto_changes = self.auto_patcher.patch(
            candidate,
            auto_issues,
        )

        if self.llm_patcher is not None:
            return self._repair_with_llm(
                original=candidate,
                current=current,
                auto_changes=auto_changes,
                routed_issues=routed_issues,
                requires_manual_review=requires_manual_review,
            )

        return self._repair_with_locator(
            original=candidate,
            current=current,
            auto_changes=auto_changes,
            routed_issues=routed_issues,
            requires_manual_review=requires_manual_review,
        )

    def _repair_with_llm(
        self,
        *,
        original: StoreCandidate,
        current: StoreCandidate,
        auto_changes: list[RepairChange],
        routed_issues: Sequence[DetectedIssue],
        requires_manual_review: bool,
    ) -> StoreRepairResult:
        """
        Apply the configured LLM repair path.

        :param original: Candidate before any repair attempt.
        :param current: Candidate after deterministic auto repair.
        :param auto_changes: Changes produced by deterministic repair.
        :param routed_issues: Remaining issues routed to LLM repair.
        :param requires_manual_review: Manual-review flag already established
            during issue routing.
        :return: Combined repair result.
        """
        if not routed_issues:
            return StoreRepairResult(
                original_candidate=original,
                repaired_candidate=current,
                repair_changes=auto_changes,
                repair_confidence=1.0,
                requires_manual_review=requires_manual_review,
            )

        (
            repaired,
            llm_changes,
            llm_confidence,
            llm_manual_review,
        ) = self.llm_patcher.patch(
            current,
            routed_issues,
        )

        overall_confidence = self._calculate_weighted_confidence(
            repair_changes=llm_changes,
            overall_confidence=llm_confidence,
        )

        requires_manual_review = (
            requires_manual_review
            or llm_manual_review
            or overall_confidence < self.confidence_threshold
        )

        return StoreRepairResult(
            original_candidate=original,
            repaired_candidate=repaired,
            repair_changes=[
                *auto_changes,
                *llm_changes,
            ],
            repair_confidence=overall_confidence,
            requires_manual_review=requires_manual_review,
        )

    def _repair_with_locator(
        self,
        *,
        original: StoreCandidate,
        current: StoreCandidate,
        auto_changes: list[RepairChange],
        routed_issues: Sequence[DetectedIssue],
        requires_manual_review: bool,
    ) -> StoreRepairResult:
        """
        Apply locator-backed repair when no LLM patcher is configured.

        Locator repair is intentionally conservative and always requires manual
        review when it is attempted.

        :param original: Candidate before any repair attempt.
        :param current: Candidate after deterministic auto repair.
        :param auto_changes: Changes produced by deterministic repair.
        :param routed_issues: Remaining issues routed to locator repair.
        :param requires_manual_review: Existing manual-review flag.
        :return: Combined repair result.
        :raises ValueError: If locator repair is required but no locator patcher
            is configured.
        """
        if not routed_issues:
            return StoreRepairResult(
                original_candidate=original,
                repaired_candidate=current,
                repair_changes=auto_changes,
                repair_confidence=None,
                requires_manual_review=requires_manual_review,
            )

        if self.locator_patcher is None:
            raise ValueError(
                "locator_patcher must be provided when llm_patcher is None"
            )

        (
            repaired,
            locator_changes,
            _,
            _,
        ) = self.locator_patcher.patch(
            current,
            routed_issues,
        )

        return StoreRepairResult(
            original_candidate=original,
            repaired_candidate=repaired,
            repair_changes=[
                *auto_changes,
                *locator_changes,
            ],
            repair_confidence=None,
            requires_manual_review=True,
        )

    @staticmethod
    def _partition_issues(
        issues: Sequence[DetectedIssue],
        *,
        llm_enabled: bool,
    ) -> tuple[
        list[DetectedIssue],
        list[DetectedIssue],
        bool,
    ]:
        """
        Partition issues by their configured repair route.

        :param issues: Verification issues to classify.
        :param llm_enabled: Whether semantic LLM repair is available.
        :return: Auto issues, remaining routed issues, and an initial
            manual-review flag.
        """
        auto_issues: list[DetectedIssue] = []
        routed_issues: list[DetectedIssue] = []
        requires_manual_review = False

        for issue in issues:
            issue_type = ISSUE_TYPES.get(
                issue.name
            )

            if issue_type is None:
                routed_issues.append(issue)
                requires_manual_review = True
                continue

            route_to = (
                issue_type.route_to
                or ""
            ).strip().lower()

            if route_to == "auto":
                auto_issues.append(issue)
                continue

            if llm_enabled:
                if route_to == "llm":
                    routed_issues.append(issue)
                else:
                    requires_manual_review = True
            else:
                routed_issues.append(issue)

        return (
            auto_issues,
            routed_issues,
            requires_manual_review,
        )

    @staticmethod
    def _calculate_weighted_confidence(
        repair_changes: Sequence[RepairChange],
        overall_confidence: float,
    ) -> float:
        """
        Combine issue-level and LLM-reported confidence conservatively.

        Auto repairs are excluded because they are deterministic. The final
        score is bounded by both the weighted repair-change confidence and the
        LLM's overall confidence.

        :param repair_changes: Semantic repair changes returned by the LLM.
        :param overall_confidence: Overall confidence reported by the LLM.
        :return: Conservative combined confidence in [0.0, 1.0].
        """
        if not repair_changes:
            return max(
                0.0,
                min(
                    1.0,
                    float(overall_confidence),
                ),
            )

        weighted_sum = 0.0
        total_weight = 0.0

        for change in repair_changes:
            weight = ISSUE_WEIGHTS.get(
                change.issue,
                1.0,
            )
            weighted_sum += (
                change.confidence
                * weight
            )
            total_weight += weight

        if total_weight == 0.0:
            return max(
                0.0,
                min(
                    1.0,
                    float(overall_confidence),
                ),
            )

        weighted_average = (
            weighted_sum
            / total_weight
        )

        return max(
            0.0,
            min(
                1.0,
                min(
                    weighted_average,
                    float(overall_confidence),
                ),
            ),
        )


__all__ = [
    "CONFIDENCE_THRESHOLD",
    "ISSUE_WEIGHTS",
    "StoreLocationRepairService",
]
