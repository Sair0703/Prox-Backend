# services/store_service/capabilities/store_location_repair/patchers/patch_strategies/auto/normalize_text_patch_strategy.py

from __future__ import annotations

import re
from dataclasses import replace

from services.llm_service.prompts.store_prompts.repair_issues.contract import RepairChange
from services.store_service.models.base import (
    DetectedIssue,
    StoreCandidate,
)
from services.store_service.models.constants import (
    DIRECTION_ALIASES,
    STATE_ALIASES,
    STREET_SUFFIX_ALIASES,
)


class NormalizeTextPatchStrategy:
    """
    Apply deterministic text normalization to a StoreCandidate.

    Supported issues:
    - ``case_variation``
    - ``punctuation_variation``
    - ``whitespace_variation``
    - ``abbreviation_variation``
    - ``direction_alias_variation``
    """

    SUPPORTED_ISSUES = {
        "case_variation",
        "punctuation_variation",
        "whitespace_variation",
        "abbreviation_variation",
        "direction_alias_variation",
    }

    ISSUE_FIELDS: dict[str, tuple[str, ...]] = {
        "case_variation": (
            "store_name",
            "address",
            "full_address",
            "city",
            "state",
        ),
        "punctuation_variation": (
            "store_name",
            "address",
            "full_address",
            "city",
            "state",
        ),
        "whitespace_variation": (
            "store_name",
            "address",
            "full_address",
            "city",
            "state",
        ),
        "abbreviation_variation": (
            "store_name",
            "address",
            "full_address",
        ),
        "direction_alias_variation": (
            "address",
            "full_address",
        ),
    }

    def patch(
        self,
        candidate: StoreCandidate,
        issue: DetectedIssue,
    ) -> tuple[StoreCandidate, list[RepairChange]]:
        """
        Apply deterministic text normalization for a supported issue.

        :param candidate: Store candidate to repair.
        :param issue: Data-quality issue to normalize.
        :return: Updated candidate and generated repair changes.
        """
        if issue.name not in self.SUPPORTED_ISSUES:
            return candidate, []

        current = candidate
        changes: list[RepairChange] = []

        for field_name in self.ISSUE_FIELDS[issue.name]:
            before = getattr(current, field_name)
            after = self._normalize_field(
                field_name,
                before,
                issue.name,
            )

            if after == before:
                continue

            current = replace(
                current,
                **{field_name: after},
            )
            changes.append(
                RepairChange(
                    issue=issue.name,
                    fields=[field_name],
                    before=before,
                    after=after,
                    confidence=1.0,
                    repaired_by="auto",
                )
            )

        return current, changes

    def _normalize_field(
        self,
        field_name: str,
        value: object,
        issue_name: str,
    ) -> object:
        """
        Normalize one candidate field according to the reported issue.

        :param field_name: Candidate field being normalized.
        :param value: Current field value.
        :param issue_name: Issue that determines the normalization rules.
        :return: Normalized field value.
        """
        if not isinstance(value, str):
            return value

        text = value.strip()
        if not text:
            return text

        if field_name == "state":
            return self._normalize_state(text)

        text = self._collapse_whitespace(text)
        text = self._normalize_punctuation_spacing(text)

        if issue_name in {
            "abbreviation_variation",
            "direction_alias_variation",
        }:
            text = self._normalize_aliases(text)

        if issue_name == "case_variation":
            text = self._smart_case(text)
        elif field_name in {"city", "state"}:
            text = self._smart_case(text)

        return text

    @staticmethod
    def _collapse_whitespace(text: str) -> str:
        """Collapse repeated whitespace without changing token content."""
        return " ".join(text.split())

    @staticmethod
    def _normalize_punctuation_spacing(text: str) -> str:
        """Normalize spacing immediately around common punctuation."""
        text = re.sub(
            r"\s+([,.;:#/()\-])",
            r"\1",
            text,
        )
        text = re.sub(
            r"([,.;:#/()\-])(?!\s|$)",
            r"\1 ",
            text,
        )
        text = re.sub(
            r"\s{2,}",
            " ",
            text,
        )
        return text.strip()

    @staticmethod
    def _smart_case(text: str) -> str:
        """
        Apply conservative title casing while preserving short acronyms.

        :param text: Text to normalize.
        :return: Smart-cased text.
        """
        tokens = text.split()
        output: list[str] = []

        for token in tokens:
            if NormalizeTextPatchStrategy._should_preserve_acronym(token):
                output.append(token.upper())
                continue

            if "-" in token:
                output.append(
                    "-".join(
                        NormalizeTextPatchStrategy._smart_case_piece(piece)
                        for piece in token.split("-")
                    )
                )
                continue

            output.append(
                NormalizeTextPatchStrategy._smart_case_piece(token)
            )

        return " ".join(output)

    @staticmethod
    def _smart_case_piece(token: str) -> str:
        """Apply smart casing to one token."""
        if not token:
            return token

        if NormalizeTextPatchStrategy._should_preserve_acronym(token):
            return token.upper()

        return token[:1].upper() + token[1:].lower()

    @staticmethod
    def _should_preserve_acronym(token: str) -> bool:
        """Return whether a short all-uppercase token should remain uppercase."""
        compact = re.sub(
            r"[^A-Za-z0-9]",
            "",
            token,
        )
        return (
            bool(compact)
            and compact.isupper()
            and len(compact) <= 4
        )

    @staticmethod
    def _normalize_state(text: str) -> str:
        """Normalize a state name or abbreviation to the configured alias."""
        key = " ".join(text.upper().split())
        return STATE_ALIASES.get(
            key,
            STATE_ALIASES.get(
                key.replace(" ", ""),
                text.upper(),
            ),
        )

    @staticmethod
    def _normalize_aliases(text: str) -> str:
        """
        Normalize configured direction and street-suffix aliases.

        :param text: Address text containing possible aliases.
        :return: Text with aliases replaced by canonical forms.
        """
        tokens = re.split(
            r"(\W+)",
            text,
        )
        output: list[str] = []

        for token in tokens:
            if not token or re.fullmatch(r"\W+", token):
                output.append(token)
                continue

            upper = token.upper()

            if upper in DIRECTION_ALIASES:
                output.append(DIRECTION_ALIASES[upper])
                continue

            if upper in STREET_SUFFIX_ALIASES:
                output.append(STREET_SUFFIX_ALIASES[upper])
                continue

            output.append(token)

        return "".join(output)


__all__ = ["NormalizeTextPatchStrategy"]