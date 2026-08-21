# services/store_service/capabilities/store_location_acquisition/protocals.py

"""Contracts and data models for store location acquisition."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from services.store_service.models.base import StoreLocationRecord


StorePayload = Mapping[str, Any]


@dataclass(slots=True)
class AcquisitionSourceInfo:
    """Describes the retailer source and acquisition mechanism."""

    retailer_key: str
    retailer_name: str
    official_website_url: str | None = None
    store_locator_url: str | None = None
    endpoint_url: str | None = None
    source_type: str | None = None
    provider: str | None = None
    notes: str | None = None


@dataclass(slots=True)
class AcquisitionArtifact:
    """Represents a raw artifact collected during acquisition."""

    artifact_type: str
    source_url: str
    file_path: Path | None = None
    content: Any | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AcquisitionValidationResult:
    """Summarizes validation results for an acquired dataset."""

    is_valid: bool
    total_records: int = 0
    unique_store_ids: int = 0
    missing_store_ids: int = 0
    missing_coordinates: int = 0
    non_us_records: int = 0
    duplicate_store_ids: list[str] = field(default_factory=list)
    issue_counts: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AcquisitionOutput:
    """Contains the results and artifacts produced by an acquisition run."""

    source_info: AcquisitionSourceInfo
    raw_artifacts: list[AcquisitionArtifact] = field(default_factory=list)
    store_payloads: list[StorePayload] = field(default_factory=list)
    normalized_records: list[StoreLocationRecord] = field(default_factory=list)
    validation: AcquisitionValidationResult | None = None
    output_files: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class StoreLocationAcquisitionStrategy(Protocol):
    """Defines the acquisition contract implemented by each retailer strategy."""

    retailer_key: str
    retailer_name: str

    def discover_source(self) -> AcquisitionSourceInfo:
        """
        Identify the retailer source and acquisition mechanism.

        :return: Metadata describing the source used for acquisition.
        """
        ...

    def fetch_raw_artifacts(self) -> list[AcquisitionArtifact]:
        """
        Fetch the raw artifacts needed to acquire the retailer location dataset.

        :return: Raw artifacts collected from the retailer source.
        """
        ...

    def extract_store_payloads(
        self,
        artifacts: Sequence[AcquisitionArtifact],
    ) -> list[StorePayload]:
        """
        Extract retailer-specific store payloads from raw artifacts.

        :param artifacts: Raw artifacts collected during acquisition.
        :return: Store payloads extracted from the source data.
        """
        ...

    def validate_store_payloads(
        self,
        payloads: Sequence[StorePayload],
    ) -> AcquisitionValidationResult:
        """
        Validate the completeness and quality of acquired store data.

        :param payloads: Extracted store payloads to validate.
        :return: Validation result describing dataset quality.
        """
        ...

    def build_run_notes(self) -> list[str]:
        """
        Build human-readable notes describing the acquisition run.

        :return: Notes for execution auditing and documentation.
        """
        ...