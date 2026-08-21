from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from services.store_service.capabilities.store_info_backfill.store_info_backfill_service import (
    StoreInfoBackfillService,
)
from services.store_service.capabilities.store_info_normalization.store_info_normalization_service import (
    StoreInfoNormalizationResult,
    StoreInfoNormalizationService,
)
from services.store_service.capabilities.store_location_acquisition.store_location_acquisition_service import (
    StoreLocationAcquisitionService,
)
from services.store_service.capabilities.store_location_acquisition.strategy_registry import (
    StoreLocationAcquisitionStrategyRegistry,
)
from services.store_service.capabilities.store_location_repair.models import (
    StoreRepairResult,
)
from services.store_service.capabilities.store_location_repair.store_location_repair_service import (
    StoreLocationRepairService,
)
from services.store_service.capabilities.store_location_resolution.store_location_resolution_service import (
    StoreLocationResolutionService,
)
from services.store_service.capabilities.store_location_verification.models import (
    StoreVerificationResult,
)
from services.store_service.capabilities.store_location_verification.store_location_verification_service import (
    StoreLocationVerificationService,
)
from services.store_service.models.base import (
    DetectedIssue,
    FlyerDeal,
    StoreCandidate,
    StoreLocationRecord,
)
from services.store_service.models.workflows import (
    StoreValidationWorkflowResult,
)


class StoreService:
    """
    Unified Store Service facade and cross-capability orchestrator.

    StoreService exposes stable store-oriented APIs and composes the
    Store Intelligence capabilities without owning their domain-specific
    implementation details.
    """

    def __init__(
        self,
        *,
        normalization_service: StoreInfoNormalizationService,
        resolution_service: StoreLocationResolutionService,
        verification_service: StoreLocationVerificationService,
        repair_service: StoreLocationRepairService,
        backfill_service: StoreInfoBackfillService | None = None,
        acquisition_registry: StoreLocationAcquisitionStrategyRegistry | None = None,
        acquisition_output_root: Path | None = None,
    ) -> None:
        """
        Initialize the Store Service facade.

        :param normalization_service: Store-information normalization capability.
        :param resolution_service: Store-location resolution capability.
        :param verification_service: Store-location verification capability.
        :param repair_service: Store-location repair capability.
        :param backfill_service: Optional store-info backfill capability.
        :param acquisition_registry: Optional retailer-to-strategy registry.
        :param acquisition_output_root: Optional acquisition output root.
        """
        self.normalization_service = normalization_service
        self.resolution_service = resolution_service
        self.verification_service = verification_service
        self.repair_service = repair_service
        self.backfill_service = backfill_service
        self.acquisition_registry = (
            acquisition_registry
            or StoreLocationAcquisitionStrategyRegistry(
                normalizer=normalization_service,
            )
        )
        self.acquisition_output_root = acquisition_output_root

    # ------------------------------------------------------------------
    # Capability facade
    # ------------------------------------------------------------------

    def acquire_store_locations(
        self,
        retailer: str,
        *,
        strategy_kwargs: Mapping[str, object] | None = None,
    ):
        """
        Acquire store locations by retailer name.

        The retailer is normalized and resolved to its registered acquisition
        strategy before the common acquisition workflow is executed.

        :param retailer: Raw retailer name supplied by the caller.
        :param strategy_kwargs: Optional constructor arguments for the strategy.
        :return: Acquisition output produced by the selected retailer strategy.
        """
        strategy = self.acquisition_registry.get_strategy(
            retailer,
            strategy_kwargs=strategy_kwargs,
        )

        service = StoreLocationAcquisitionService(
            strategy,
            output_root=self.acquisition_output_root,
        )

        return service.acquire()

    def normalize_store_location(
        self,
        store_location: StoreLocationRecord,
    ) -> StoreInfoNormalizationResult:
        """
        Normalize a potentially non-standard store-location record.

        The normalization capability owns the adapter from
        StoreLocationRecord to its raw normalization schema.

        :param store_location: Raw or partially standardized store record.
        :return: Normalized store information and diagnostics.
        """
        return self.normalization_service.normalize(
            store_location
        )

    def resolve_store(
        self,
        deal: FlyerDeal,
    ) -> list[StoreCandidate]:
        """
        Resolve a flyer deal into candidate stores.

        :param deal: Flyer deal requiring store resolution.
        :return: Merged candidate stores.
        """
        return self.resolution_service.find_candidate_stores(
            deal
        )

    def resolve_best_store(
        self,
        deal: FlyerDeal,
    ) -> StoreCandidate | None:
        """
        Resolve and select the best store candidate for a deal.

        :param deal: Flyer deal requiring store resolution.
        :return: Best store candidate, or None when no candidate is available.
        """
        return self.resolution_service.find_best_store_candidate(
            deal
        )

    def find_store_for_deal(
        self,
        deal: FlyerDeal,
    ) -> StoreCandidate | None:
        """
        Find the best store for a flyer deal and backfill the deal when configured.

        Resolution owns candidate aggregation and selection. The Store Service
        only orchestrates the resolved candidate into the optional backfill
        workflow. The backfill operator is responsible for creating a canonical
        store record when the selected candidate is external.

        :param deal: Flyer deal requiring a store.
        :return: Resolved store candidate, or None when no candidate is found.
        """
        candidate = self.resolve_best_store(
            deal
        )

        if candidate is None:
            return None

        if self.backfill_service is not None:
            self.backfill_flyer_deal_store(
                deal=deal,
                best_candidate=candidate,
                candidates=self.resolve_store(deal),
            )

        return candidate

    def verify_store(
        self,
        candidate: StoreCandidate,
    ) -> StoreVerificationResult:
        """
        Run configured primary verification.

        :param candidate: Store candidate to verify.
        :return: Verification result.
        """
        return self.verification_service.verify(
            candidate
        )

    def verify_store_enhanced(
        self,
        candidate: StoreCandidate,
    ) -> StoreVerificationResult:
        """
        Run primary verification with optional secondary enhancement.

        :param candidate: Store candidate to verify.
        :return: Enhanced verification result.
        """
        return self.verification_service.verify_enhanced(
            candidate
        )

    def repair_store(
        self,
        candidate: StoreCandidate,
        issues: Sequence[DetectedIssue],
    ) -> StoreRepairResult:
        """
        Repair a store candidate using configured repair strategies.

        :param candidate: Store candidate to repair.
        :param issues: Verification issues driving the repair.
        :return: Repair result.
        """
        return self.repair_service.repair(
            candidate,
            issues,
        )

    # ------------------------------------------------------------------
    # Cross-capability orchestration
    # ------------------------------------------------------------------

    def verify_and_repair_store(
        self,
        candidate: StoreCandidate,
        *,
        enhanced_verification: bool = False,
    ) -> StoreValidationWorkflowResult:
        """
        Verify a store candidate, repair detected issues, and re-verify.

        When the initial verification succeeds, no repair is attempted.
        When issues remain after repair and re-verification, the workflow
        returns the last candidate together with the unresolved issues.

        :param candidate: Store candidate to validate.
        :param enhanced_verification: Whether configured secondary verifiers
            should participate in both verification stages.
        :return: Verification-and-repair workflow result.
        """
        initial_verification = (
            self.verify_store_enhanced(candidate)
            if enhanced_verification
            else self.verify_store(candidate)
        )

        if initial_verification.verified:
            return StoreValidationWorkflowResult(
                original_candidate=candidate,
                final_candidate=candidate,
                initial_verification=initial_verification,
                repair_result=None,
                final_verification=initial_verification,
                unresolved_issues=[],
                status="verified",
            )

        repair_result = self.repair_store(
            candidate,
            initial_verification.issues,
        )
        final_candidate = repair_result.repaired_candidate

        final_verification = (
            self.verify_store_enhanced(final_candidate)
            if enhanced_verification
            else self.verify_store(final_candidate)
        )

        unresolved_issues = (
            []
            if final_verification.verified
            else list(final_verification.issues)
        )

        return StoreValidationWorkflowResult(
            original_candidate=candidate,
            final_candidate=final_candidate,
            initial_verification=initial_verification,
            repair_result=repair_result,
            final_verification=final_verification,
            unresolved_issues=unresolved_issues,
            status=(
                "verified"
                if final_verification.verified
                else "unresolved"
            ),
        )

    # ------------------------------------------------------------------
    # Backfill facade
    # ------------------------------------------------------------------

    def backfill_store_location(
        self,
        store_location: StoreLocationRecord,
    ) -> None:
        """
        Backfill store information into a canonical store-location record.

        :param store_location: Store-location record to persist.
        :raises ValueError: If no backfill service is configured.
        """
        self._require_backfill_service()

        self.backfill_service.backfill_store_location(
            store_location
        )

    def backfill_flyer_deal_store(
        self,
        deal: FlyerDeal,
        best_candidate: StoreCandidate,
        candidates: list[StoreCandidate],
    ):
        """
        Backfill store-related information into a flyer deal.

        :param deal: Flyer deal to update.
        :param best_candidate: Candidate selected as the resolved store.
        :param candidates: Candidates considered during resolution.
        :return: Store resolution used for the backfill.
        :raises ValueError: If no backfill service is configured.
        """
        self._require_backfill_service()

        return self.backfill_service.backfill_flyer_deal(
            deal=deal,
            best_candidate=best_candidate,
            candidates=candidates,
        )

    # ------------------------------------------------------------------
    # Reserved ingestion / promotion interfaces
    # ------------------------------------------------------------------

    def ingest_store_locations(
        self,
        *args: Any,
        **kwargs: Any,
    ):
        """
        Reserve the future store-location ingestion facade.

        :raises NotImplementedError: Until retailer-specific ingestion
            strategies and the production workflow are finalized.
        """
        raise NotImplementedError(
            "Store location ingestion orchestration is reserved until "
            "retailer-specific ingestion strategies are production-ready."
        )

    def promote_store_locations(
        self,
        *args: Any,
        **kwargs: Any,
    ):
        """
        Reserve the future staging-to-canonical promotion facade.

        :raises NotImplementedError: Until the production persistence path
            is available.
        """
        raise NotImplementedError(
            "Store location promotion orchestration is reserved until "
            "the production persistence path is available."
        )

    def acquire_ingest_promote_store_locations(
        self,
        *args: Any,
        **kwargs: Any,
    ):
        """
        Reserve the future acquisition-to-ingestion-to-promotion workflow.

        :raises NotImplementedError: Until ingestion and promotion are
            production-ready.
        """
        raise NotImplementedError(
            "Acquisition -> ingestion -> promotion orchestration is reserved "
            "until the downstream pipeline is production-ready."
        )

    def _require_backfill_service(self) -> None:
        """
        Ensure backfill capability is configured.

        :raises ValueError: If no backfill service is configured.
        """
        if self.backfill_service is None:
            raise ValueError(
                "backfill_service is not configured"
            )


__all__ = ["StoreService"]