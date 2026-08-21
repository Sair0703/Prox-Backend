# services/store_service/capabilities/store_location_acquisition/store_location_acquisition_service.py

"""Orchestrates retailer store-location acquisition through strategy implementations."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import csv
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from services.store_service.capabilities.store_location_acquisition.protocals import (
    AcquisitionArtifact,
    AcquisitionOutput,
    AcquisitionSourceInfo,
    AcquisitionValidationResult,
    StoreLocationAcquisitionStrategy,
)
from services.store_service.capabilities.store_location_acquisition.strategy_registry import (
    StoreLocationAcquisitionStrategyRegistry,
)


CSV_FIELDNAMES: list[str] = [
    "retailer",
    "store_type",
    "store_number",
    "city_slug",
    "state",
    "store_url",
    "source_sitemap",
    "street_address",
    "address_city",
    "address_state",
    "zip_code",
    "full_address",
    "phone",
    "extraction_source",
    "scrape_status",
    "http_status",
    "error_message",
    "scraped_at_utc",
]


class StoreLocationAcquisitionService:
    """
    Orchestrates store location acquisition for a single retailer strategy.

    Supports initial acquisition for new retailers and repeated acquisition
    runs that provide evidence for the freshness of existing store data.
    """

    def __init__(
        self,
        strategy: StoreLocationAcquisitionStrategy,
        *,
        output_root: Path | None = None,
        normalizer=None,
    ) -> None:
        """
        Initialize the acquisition service.

        :param strategy: Retailer-specific strategy used for the acquisition workflow.
        :param output_root: Root directory for acquisition output files.
        :param normalizer: Optional shared retailer normalizer used by
            retailer-based factory methods.
        """
        self._strategy = strategy
        self._output_root = (
            output_root
            if output_root is not None
            else Path(__file__).resolve().parent / "_output"
        )
        self._normalizer = normalizer

    @classmethod
    def for_retailer(
        cls,
        retailer: str,
        *,
        output_root: Path | None = None,
        normalizer=None,
        strategy_registry: StoreLocationAcquisitionStrategyRegistry | None = None,
        strategy_kwargs: Mapping[str, Any] | None = None,
    ) -> "StoreLocationAcquisitionService":
        """
        Build an acquisition service from a retailer name.

        The retailer is normalized first, then resolved through the shared
        strategy registry. Existing runner code can continue to inject a
        concrete strategy directly through the constructor.

        :param retailer: Raw retailer name supplied by the caller.
        :param output_root: Optional acquisition output root.
        :param normalizer: Optional shared retailer normalizer.
        :param strategy_registry: Optional preconfigured strategy registry.
        :param strategy_kwargs: Optional constructor arguments for the strategy.
        :return: Acquisition service configured for the retailer.
        """
        registry = (
            strategy_registry
            or StoreLocationAcquisitionStrategyRegistry(
                normalizer=normalizer,
            )
        )
        strategy = registry.get_strategy(
            retailer,
            strategy_kwargs=strategy_kwargs,
        )

        return cls(
            strategy,
            output_root=output_root,
            normalizer=normalizer,
        )

    @classmethod
    def acquire_retailer(
        cls,
        retailer: str,
        *,
        output_root: Path | None = None,
        normalizer=None,
        strategy_registry: StoreLocationAcquisitionStrategyRegistry | None = None,
        strategy_kwargs: Mapping[str, Any] | None = None,
    ) -> AcquisitionOutput:
        """
        Acquire store locations for a retailer by name.

        :param retailer: Raw retailer name supplied by the caller.
        :param output_root: Optional acquisition output root.
        :param normalizer: Optional shared retailer normalizer.
        :param strategy_registry: Optional preconfigured strategy registry.
        :param strategy_kwargs: Optional constructor arguments for the strategy.
        :return: Acquisition output produced by the retailer strategy.
        """
        service = cls.for_retailer(
            retailer,
            output_root=output_root,
            normalizer=normalizer,
            strategy_registry=strategy_registry,
            strategy_kwargs=strategy_kwargs,
        )
        return service.acquire()

    def acquire(self) -> AcquisitionOutput:
        """
        Run the complete retailer acquisition workflow.

        The workflow discovers the source, fetches raw artifacts, extracts
        store payloads, validates the results, and writes acquisition outputs.

        :return: Acquisition results, validation data, and generated output files.
        """
        run_started_at = datetime.now(timezone.utc)
        run_id = run_started_at.strftime("%Y%m%d_%H%M%S")

        source_info = self._strategy.discover_source()
        raw_artifacts = self._strategy.fetch_raw_artifacts()
        store_payloads = self._strategy.extract_store_payloads(raw_artifacts)
        validation = self._strategy.validate_store_payloads(store_payloads)

        output_dir = self._build_output_dir(
            retailer_key=source_info.retailer_key,
            run_id=run_id,
        )
        output_files = self._write_outputs(
            output_dir=output_dir,
            run_id=run_id,
            source_info=source_info,
            raw_artifacts=raw_artifacts,
            store_payloads=store_payloads,
            validation=validation,
            run_started_at=run_started_at,
        )

        return AcquisitionOutput(
            source_info=source_info,
            raw_artifacts=raw_artifacts,
            store_payloads=store_payloads,
            normalized_records=[],
            validation=validation,
            output_files=output_files,
        )

    def _build_output_dir(self, *, retailer_key: str, run_id: str) -> Path:
        """Build and create the output directory for an acquisition run."""
        slug = self._slugify(retailer_key)
        output_dir = self._output_root / slug / run_id
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    def _write_outputs(
        self,
        *,
        output_dir: Path,
        run_id: str,
        source_info: AcquisitionSourceInfo,
        raw_artifacts: Sequence[AcquisitionArtifact],
        store_payloads: Sequence[Mapping[str, Any]],
        validation: AcquisitionValidationResult,
        run_started_at: datetime,
    ) -> dict[str, str]:
        """Write the normalized CSV and acquisition summary."""
        retailer_slug = self._slugify(source_info.retailer_key)
        timestamp = run_id

        csv_path = output_dir / f"{timestamp}_{retailer_slug}_us_locations.csv"
        summary_path = output_dir / f"{timestamp}_{retailer_slug}_summary.json"

        source_url_to_artifact: dict[str, AcquisitionArtifact] = {
            artifact.source_url: artifact for artifact in raw_artifacts
        }

        normalized_rows: list[dict[str, Any]] = []
        for payload in store_payloads:
            row = self._build_csv_row(
                payload=payload,
                source_info=source_info,
                source_url_to_artifact=source_url_to_artifact,
            )
            normalized_rows.append(row)

        self._write_csv(csv_path, normalized_rows)
        self._write_json(
            summary_path,
            {
                "retailer_key": source_info.retailer_key,
                "retailer_name": source_info.retailer_name,
                "run_id": run_id,
                "run_started_at_utc": run_started_at.isoformat(),
                "run_finished_at_utc": datetime.now(timezone.utc).isoformat(),
                "source_info": asdict(source_info),
                "validation": asdict(validation),
                "store_payload_count": len(store_payloads),
                "raw_artifact_count": len(raw_artifacts),
                "output_files": {
                    "csv": str(csv_path),
                    "summary": str(summary_path),
                },
                "run_notes": list(self._strategy.build_run_notes()),
            },
        )

        return {
            "csv": str(csv_path),
            "summary": str(summary_path),
        }

    def _build_csv_row(
        self,
        *,
        payload: Mapping[str, Any],
        source_info: AcquisitionSourceInfo,
        source_url_to_artifact: Mapping[str, AcquisitionArtifact],
    ) -> dict[str, Any]:
        """Convert a retailer payload into the shared acquisition CSV schema."""
        source_url = self._clean_text(payload.get("source_url")) or ""
        artifact = source_url_to_artifact.get(source_url)
        metadata = artifact.metadata if artifact is not None else {}

        retailer = self._clean_text(payload.get("retailer")) or source_info.retailer_name
        store_type = self._clean_text(payload.get("store_type"))
        store_number = self._clean_text(payload.get("retailer_store_id"))
        store_name = self._clean_text(payload.get("store_name"))
        city = self._clean_text(payload.get("city"))
        state = self._clean_text(payload.get("state"))
        zip_code = self._clean_text(payload.get("zip_code"))
        address = self._clean_text(payload.get("address"))
        full_address = self._clean_text(payload.get("full_address"))
        phone = self._clean_text(payload.get("phone"))
        extraction_source = self._clean_text(
            payload.get("extraction_source")
        ) or self._clean_text(payload.get("provider"))
        scraped_at_utc = self._clean_text(
            payload.get("scraped_at_utc")
        ) or self._clean_text(metadata.get("retrieved_at_utc"))

        return {
            "retailer": retailer,
            "store_type": store_type or "Regular",
            "store_number": store_number,
            "city_slug": self._slugify(city or store_name or store_number),
            "state": state,
            "store_url": self._clean_text(payload.get("store_url")),
            "source_sitemap": self._clean_text(payload.get("source_sitemap")),
            "street_address": address,
            "address_city": city,
            "address_state": state,
            "zip_code": zip_code,
            "full_address": full_address,
            "phone": phone,
            "extraction_source": (
                extraction_source
                or source_url
                or source_info.endpoint_url
            ),
            "scrape_status": "success",
            "http_status": metadata.get("http_status"),
            "error_message": None,
            "scraped_at_utc": (
                scraped_at_utc
                or datetime.now(timezone.utc).isoformat()
            ),
        }

    def _write_csv(
        self,
        path: Path,
        rows: Sequence[Mapping[str, Any]],
    ) -> None:
        """Write acquisition records to CSV."""
        with path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(
                file,
                fieldnames=CSV_FIELDNAMES,
                extrasaction="ignore",
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(dict(row))

    def _write_json(self, path: Path, payload: Any) -> None:
        """Write acquisition metadata to a JSON file."""
        path.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _clean_text(value: Any) -> str | None:
        """Normalize a value into trimmed text."""
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip()
            return text or None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _slugify(value: Any) -> str:
        """Convert a value into a filesystem-safe lowercase slug."""
        text = StoreLocationAcquisitionService._clean_text(value) or ""
        text = text.lower()
        text = re.sub(r"[^a-z0-9]+", "_", text)
        text = re.sub(r"_+", "_", text).strip("_")
        return text or "unknown"


__all__ = [
    "StoreLocationAcquisitionService",
    "CSV_FIELDNAMES",
]