# services/store_service/capabilities/store_location_verification/verifiers/secondary/llm_store_verifier.py

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from services.llm_service.llm_service import LLMService
from services.llm_service.prompts.store_prompts.detect_remaining_issues.contract import (
    DetectRemainingIssuesInput,
)
from services.store_service.capabilities.store_location_verification.models import (
    StoreVerificationResult,
)
from services.store_service.capabilities.store_location_verification.verification_helper import (
    build_verification_result,
    confidence_from_issue_count,
)
from services.store_service.models.base import (
    DetectedIssue,
    StoreCandidate,
)


class LLMStoreVerifier:
    """
    Provide optional LLM-backed enhanced store verification.

    The verifier delegates model execution to the shared LLMService and uses
    the registered ``detect_remaining_issues`` task to evaluate candidate issues
    with broader contextual reasoning.

    It only performs verification. It does not repair or mutate store data.
    """

    name = "llm_store_verifier"

    def __init__(
        self,
        llm_service: LLMService,
        *,
        task_name: str = "detect_remaining_issues",
        model: str,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> None:
        """
        Initialize the optional LLM-backed verifier.

        :param llm_service: Shared LLMService used to execute the registered
            verification task.
        :param task_name: Registered LLM task used for enhanced issue detection.
        :param model: Model identifier passed to LLMService.
        :param temperature: Sampling temperature passed to LLMService.
        :param max_tokens: Optional output-token limit passed to LLMService.
        """
        self.llm_service = llm_service
        self.task_name = task_name
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    def verify(
        self,
        request: DetectRemainingIssuesInput,
    ) -> StoreVerificationResult:
        """
        Perform enhanced verification for a store and its candidate issues.

        The request supplies the store context together with issues detected by
        the primary verification layer. LLMService executes the configured task,
        and this verifier converts the structured response into a
        StoreVerificationResult.

        :param request: Structured store context and candidate issues for
            LLM-backed verification.
        :return: Verification result containing issues the LLM still considers
            applicable, or an unverifiable result when execution/output fails.
        """
        response = self.llm_service.execute(
            task_name=self.task_name,
            payload=asdict(request),
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )

        store = self._build_store_proxy(
            request.store_location
        )

        if not response.success:
            return build_verification_result(
                store=store,
                verified=False,
                confidence_score=0.0,
                issues=[
                    DetectedIssue(
                        name="unverifiable",
                        description=(
                            response.error
                            or "LLM request failed"
                        ),
                    )
                ],
            )

        parsed = response.parsed_output or {}
        remaining_issues_raw = parsed.get(
            "remaining_issues",
            [],
        )

        if not isinstance(remaining_issues_raw, list):
            return build_verification_result(
                store=store,
                verified=False,
                confidence_score=0.0,
                issues=[
                    DetectedIssue(
                        name="unverifiable",
                        description="Invalid LLM output",
                    )
                ],
            )

        candidate_description_by_name = {
            issue.name: issue.description
            for issue in request.candidate_issues
        }

        issues: list[DetectedIssue] = []

        for item in remaining_issues_raw:
            if not isinstance(item, dict):
                continue

            name = str(
                item.get("name", "")
            ).strip()

            if not name:
                continue

            description = candidate_description_by_name.get(
                name,
                name,
            )

            issues.append(
                DetectedIssue(
                    name=name,
                    description=description,
                )
            )

        verified = len(issues) == 0
        confidence_score = (
            1.0
            if verified
            else confidence_from_issue_count(
                len(issues)
            )
        )

        return build_verification_result(
            store=store,
            verified=verified,
            confidence_score=confidence_score,
            issues=issues,
        )

    @staticmethod
    def _build_store_proxy(
        store_location: dict[str, Any],
    ) -> StoreCandidate:
        """
        Reconstruct the store candidate represented by the verification request.

        :param store_location: Serialized store-location context supplied to the
            LLM verification task.
        :return: StoreCandidate used as the subject of the verification result.
        """
        return StoreCandidate(
            canonical_store_id=int(
                store_location.get(
                    "canonical_store_id"
                )
                or 0
            ),
            retailer=store_location.get("retailer"),
            retailer_store_id=store_location.get(
                "retailer_store_id"
            ),
            retailer_key=store_location.get(
                "retailer_key"
            ),
            store_name=store_location.get(
                "store_name"
            ),
            address=store_location.get("address"),
            full_address=store_location.get(
                "full_address"
            ),
            city=store_location.get("city"),
            state=store_location.get("state"),
            zip_code=store_location.get("zip_code"),
            latitude=store_location.get("latitude"),
            longitude=store_location.get("longitude"),
            geocode_source=store_location.get(
                "geocode_source"
            ),
            geocode_confidence=store_location.get(
                "geocode_confidence"
            ),
            osm_id=store_location.get("osm_id"),
            locator_type=store_location.get(
                "locator_type"
            ),
            locator_name=store_location.get(
                "locator_name"
            ),
            show_on_map=store_location.get(
                "show_on_map"
            ),
            distance_meters=(
                store_location.get(
                    "distance_meters"
                )
                or 0.0
            ),
        )


__all__ = ["LLMStoreVerifier"]