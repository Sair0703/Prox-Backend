# services/llm_service/prompts/store_prompts/detect_remaining_issues/contract.py

"""Input and output contracts for remaining store issue detection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class CandidateIssue:
    """A candidate store issue to be verified."""

    name: str
    description: str


@dataclass(slots=True)
class DetectRemainingIssuesInput:
    """Input for determining which candidate issues still exist."""

    store_location: dict[str, Any]
    candidate_issues: list[CandidateIssue]


@dataclass(slots=True)
class RemainingIssue:
    """A candidate issue confirmed to remain in the store data."""

    name: str
    reason: str


@dataclass(slots=True)
class DetectRemainingIssuesOutput:
    """Issues that remain after LLM verification."""

    remaining_issues: list[RemainingIssue] = field(default_factory=list)