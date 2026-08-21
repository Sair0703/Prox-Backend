# services/store_service/capabilities/store_location_repair/patchers/patch_strategies/llm/llm_patch_strategy.py

from __future__ import annotations

from dataclasses import replace
from typing import Any, Sequence

from services.llm_service.llm_service import LLMService
from services.llm_service.prompts.store_prompts.repair_issues.contract import (
    RepairChange,
    RepairStoreOutput,
)
from services.store_service.models.base import (
    DetectedIssue,
    StoreCandidate,
)


class LLMPatchStrategy:
    """
    Apply semantic store repair through the shared LLMService.

    The strategy sends the current store location and routed issues to the
    registered ``repair_store`` task and applies the returned final store
    representation to a new StoreCandidate.
    """

    def __init__(
        self,
        llm_service: LLMService,
        model: str,
    ) -> None:
        """
        Initialize the LLM repair strategy.

        :param llm_service: Shared LLM service used for repair execution.
        :param model: Model identifier passed to the repair task.
        """
        self.llm_service = llm_service
        self.model = model

    def patch(
        self,
        candidate: StoreCandidate,
        issues: Sequence[DetectedIssue],
    ) -> tuple[StoreCandidate, list[RepairChange], float, bool]:
        """
        Repair a candidate using the registered ``repair_store`` task.

        :param candidate: Store candidate to repair.
        :param issues: Issues requiring semantic repair.
        :return: Updated candidate, repair changes, overall confidence,
            and manual-review flag.
        """
        if not issues:
            return candidate, [], 1.0, False

        payload = {
            "store_location": self._candidate_to_payload(candidate),
            "remaining_issues": self._issues_to_payload(issues),
        }

        response = self.llm_service.execute(
            "repair_store",
            payload,
            model=self.model,
        )

        if (
            not response.success
            or not isinstance(response.parsed_output, dict)
        ):
            return candidate, [], 0.0, True

        output = self._parse_output(
            response.parsed_output
        )
        if output is None:
            return candidate, [], 0.0, True

        updated_candidate = self._apply_repaired_store_location(
            candidate,
            output.repaired_store_location,
        )

        return (
            updated_candidate,
            output.repair_changes,
            output.overall_confidence,
            output.requires_manual_review,
        )

    def _parse_output(
        self,
        raw: dict[str, Any],
    ) -> RepairStoreOutput | None:
        """
        Parse and validate the structured LLM repair response.

        :param raw: Parsed JSON payload returned by LLMService.
        :return: Repair output contract, or None when the payload is invalid.
        """
        try:
            repair_changes_raw = raw.get(
                "repair_changes",
                [],
            )
            repaired_store_location = raw.get(
                "repaired_store_location",
                {},
            )
            overall_confidence = raw.get(
                "overall_confidence",
                0.0,
            )
            requires_manual_review = bool(
                raw.get(
                    "requires_manual_review",
                    False,
                )
            )

            if not isinstance(
                repaired_store_location,
                dict,
            ):
                return None

            if not isinstance(
                repair_changes_raw,
                list,
            ):
                return None

            repair_changes: list[RepairChange] = []

            for item in repair_changes_raw:
                if not isinstance(item, dict):
                    continue

                issue = item.get("issue")
                fields = item.get("fields", [])
                before = item.get("before")
                after = item.get("after")
                confidence = item.get(
                    "confidence",
                    0.0,
                )
                repaired_by = item.get(
                    "repaired_by",
                    "llm",
                )

                if not isinstance(issue, str):
                    continue

                if not isinstance(fields, list):
                    continue

                repair_changes.append(
                    RepairChange(
                        issue=issue,
                        fields=[
                            str(field)
                            for field in fields
                        ],
                        before=before,
                        after=after,
                        confidence=float(confidence),
                        repaired_by=str(repaired_by),
                    )
                )

            return RepairStoreOutput(
                repair_changes=repair_changes,
                repaired_store_location=repaired_store_location,
                overall_confidence=float(
                    overall_confidence
                ),
                requires_manual_review=requires_manual_review,
            )
        except Exception:
            return None

    def _apply_repaired_store_location(
        self,
        candidate: StoreCandidate,
        repaired_store_location: dict[str, Any],
    ) -> StoreCandidate:
        """
        Apply supported repaired fields to a new candidate.

        Fields omitted by the LLM are left unchanged. The strategy therefore
        preserves the existing candidate for fields outside the repair scope.

        :param candidate: Original store candidate.
        :param repaired_store_location: Final repaired store payload.
        :return: Candidate containing the supported repaired values.
        """
        field_map = {
            "retailer": "retailer",
            "retailer_key": "retailer_key",
            "store_name": "store_name",
            "address": "address",
            "full_address": "full_address",
            "city": "city",
            "state": "state",
            "zip_code": "zip_code",
            "latitude": "latitude",
            "longitude": "longitude",
        }

        updates: dict[str, Any] = {}

        for payload_key, candidate_field in field_map.items():
            if payload_key not in repaired_store_location:
                continue

            new_value = repaired_store_location[
                payload_key
            ]
            current_value = getattr(
                candidate,
                candidate_field,
            )

            if new_value == current_value:
                continue

            updates[candidate_field] = self._coerce_field_value(
                candidate_field,
                new_value,
            )

        if not updates:
            return candidate

        return replace(
            candidate,
            **updates,
        )

    def _candidate_to_payload(
        self,
        candidate: StoreCandidate,
    ) -> dict[str, Any]:
        """
        Build the store payload expected by the repair task.

        :param candidate: Store candidate to serialize.
        :return: JSON-compatible store-location payload.
        """
        payload = {
            "id": candidate.canonical_store_id,
            "retailer": candidate.retailer,
            "retailer_key": candidate.retailer_key,
            "store_name": candidate.store_name,
            "address": candidate.address,
            "full_address": candidate.full_address,
            "city": candidate.city,
            "state": candidate.state,
            "zip_code": candidate.zip_code,
            "latitude": candidate.latitude,
            "longitude": candidate.longitude,
        }

        return {
            key: value
            for key, value in payload.items()
            if value is not None or key == "id"
        }

    @staticmethod
    def _issues_to_payload(
        issues: Sequence[DetectedIssue],
    ) -> list[dict[str, str]]:
        """
        Convert detected issues into the repair-task input contract.

        :param issues: Issues routed to the LLM repair strategy.
        :return: Serialized issue list for the repair prompt.
        """
        return [
            {
                "name": issue.name,
                "reason": issue.description,
            }
            for issue in issues
        ]

    @staticmethod
    def _coerce_field_value(
        field_name: str,
        value: Any,
    ) -> Any:
        """
        Convert repaired field values to their expected runtime types.

        :param field_name: Candidate field being assigned.
        :param value: Repaired value returned by the LLM.
        :return: Value converted for the candidate field where applicable.
        """
        if value is None:
            return None

        if field_name in {
            "latitude",
            "longitude",
        }:
            try:
                return float(value)
            except Exception:
                return value

        return value


__all__ = ["LLMPatchStrategy"]