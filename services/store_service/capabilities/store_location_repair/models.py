# services/store_service/capabilities/store_location_repair/models.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from services.llm_service.prompts.store_prompts.repair_issues.contract import RepairChange
from services.store_service.models.base import (
    StoreCandidate,
)


@dataclass(slots=True)
class StoreRepairResult:
    """Contains the candidate produced by a repair attempt and its diagnostics."""

    original_candidate: StoreCandidate
    repaired_candidate: StoreCandidate
    repair_changes: list[RepairChange] = field(default_factory=list)
    repair_confidence: float | None = None
    requires_manual_review: bool = False

    @property
    def changed(self) -> bool:
        """Return whether the repair produced a different candidate."""
        return (
            self.repaired_candidate
            != self.original_candidate
        )

    @property
    def change_count(self) -> int:
        """Return the number of recorded repair changes."""
        return len(self.repair_changes)


__all__ = ["StoreRepairResult"]
