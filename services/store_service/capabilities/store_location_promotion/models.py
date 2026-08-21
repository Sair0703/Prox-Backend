# services/store_service/capabilities/store_location_promotion/models.py

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class PromotionAuditWriter:
    """Writes promotion results, failures, and the run summary."""

    output_dir: Path
    run_id: str

    results_path: Path = field(init=False)
    failures_path: Path = field(init=False)
    summary_path: Path = field(init=False)

    fieldnames: list[str] = field(init=False, repr=False)

    _results_fp: Any = field(init=False, repr=False)
    _failures_fp: Any = field(init=False, repr=False)
    _results_writer: csv.DictWriter = field(init=False, repr=False)
    _failures_writer: csv.DictWriter = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.results_path = (
            self.output_dir / f"{self.run_id}_promotion_results.csv"
        )
        self.failures_path = (
            self.output_dir / f"{self.run_id}_failures.csv"
        )
        self.summary_path = (
            self.output_dir / f"{self.run_id}_summary.json"
        )

        self.fieldnames = [
            "run_id",
            "processed_at_utc",
            "staging_store_location_id",
            "status",
            "review_status",
            "already_promoted_store_location_v2_id",
            "retailer_raw",
            "retailer",
            "retailer_key_sent",
            "store_type",
            "store_number",
            "store_name",
            "store_url",
            "source_sitemap",
            "phone",
            "extraction_source",
            "scrape_status",
            "http_status",
            "error_message",
            "street_address",
            "address_city",
            "address_state",
            "city",
            "state",
            "zip_code",
            "address",
            "full_address",
            "geocode_address",
            "normalized_reason_codes",
            "normalization_notes",
            "latitude",
            "longitude",
            "geocode_confidence",
            "geocode_provider",
            "geocode_provider_base",
            "store_location_id",
            "failure_reason",
            "failure_details",
            "promotion_error",
            "payload_json",
            "staging_row_json",
        ]

        self._results_fp = self.results_path.open(
            "w",
            newline="",
            encoding="utf-8",
        )
        self._failures_fp = self.failures_path.open(
            "w",
            newline="",
            encoding="utf-8",
        )

        self._results_writer = csv.DictWriter(
            self._results_fp,
            fieldnames=self.fieldnames,
        )
        self._failures_writer = csv.DictWriter(
            self._failures_fp,
            fieldnames=self.fieldnames,
        )

        self._results_writer.writeheader()
        self._failures_writer.writeheader()
        self._results_fp.flush()
        self._failures_fp.flush()

    def write_result(self, row: dict[str, Any]) -> None:
        """Append one processed staging record to the result audit."""
        self._results_writer.writerow(row)
        self._results_fp.flush()

    def write_failure(self, row: dict[str, Any]) -> None:
        """Append one failed promotion record to the failure audit."""
        self._failures_writer.writerow(row)
        self._failures_fp.flush()

    def write_summary(self, summary: dict[str, Any]) -> None:
        """Write the final promotion run summary."""
        self.summary_path.write_text(
            json.dumps(
                summary,
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

    def close(self) -> None:
        """Close the promotion audit files."""
        try:
            self._results_fp.close()
        finally:
            self._failures_fp.close()