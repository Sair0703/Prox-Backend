# services/llm_service/prompts/store_prompts/repair_issues/contract.py

"""Input and output contracts for LLM-assisted store issue repair."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class RemainingIssue:
    """An unresolved store issue requiring semantic repair."""

    name: str
    reason: str


@dataclass(slots=True)
class RepairChange:
    """A change made to resolve a specific store issue."""

    issue: str
    fields: list[str]
    before: Any
    after: Any
    confidence: float
    repaired_by: str = "llm"


@dataclass(slots=True)
class RepairStoreInput:
    """Input for repairing unresolved issues in a store location."""

    store_location: dict[str, Any]
    remaining_issues: list[RemainingIssue]


@dataclass(slots=True)
class RepairStoreOutput:
    """Final store repair result produced by the LLM."""

    repair_changes: list[RepairChange] = field(default_factory=list)
    repaired_store_location: dict[str, Any] = field(default_factory=dict)
    overall_confidence: float = 0.0
    requires_manual_review: bool = False