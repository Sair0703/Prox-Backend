from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tqdm import tqdm

from config.supabase import get_supabase_client
from services.store_service.capabilities.store_location_ingestion.protocols import (
    StagingStoreLocation,
    StoreLocationIngestionStrategy,
)
from services.store_service.capabilities.store_location_ingestion.strategies.legacy_store_location_ingestion_strategy import (
    LegacyStoreLocationIngestionStrategy,
)


STAGING_TABLE = "staging_store_locations"
MANUAL_REVIEW_TABLE = "staging_store_locations_manual_review"

INPUT_ROOT = Path(__file__).resolve().parent / "input"
OUTPUT_ROOT = Path(__file__).resolve().parent / "_output"

LEGACY_INPUTS: dict[str, tuple[str, str]] = {
    "albertsons": ("Albertsons", "_albertsons_us_locations.csv"),
    "aldi": ("ALDI", "_aldi_us_locations.csv"),
    "costco": ("Costco", "_costco_us_locations.csv"),
    "food_lion": ("Food Lion", "_food_lion_us_locations.csv"),
    "kroger": ("Kroger", "_kroger_us_locations.csv"),
    "publix": ("Publix", "_publix_us_locations.json"),
    "target": ("Target", "_target_us_locations.csv"),
    "trader_joes": ("Trader Joe's", "_trader_joes_us_locations.csv"),
    "walmart": ("Walmart", "_walmart_us_locations.csv"),
}


class StoreLocationIngestionService:
    """Ingest retailer-specific legacy sources into staging store locations."""

    def __init__(
        self,
        strategy: StoreLocationIngestionStrategy,
        *,
        supabase: Any | None = None,
        output_root: Path | None = None,
    ) -> None:
        """
        Initialize the ingestion service.

        :param strategy: Retailer-specific input strategy.
        :param supabase: Optional Supabase client override.
        :param output_root: Root directory for timestamped ingestion artifacts.
        """
        self.strategy = strategy
        self._supabase = supabase or get_supabase_client()
        self.output_root = output_root or OUTPUT_ROOT

    def ingest(self) -> dict[str, Any]:
        """Run the ingestion workflow for the configured source."""
        run_started_at = datetime.now(timezone.utc)
        run_id = run_started_at.strftime("%Y%m%d_%H%M%S")
        run_output_dir = self.output_root / run_id
        run_output_dir.mkdir(parents=True, exist_ok=True)

        results_path = run_output_dir / f"{run_id}_ingestion_results.csv"
        failures_path = run_output_dir / f"{run_id}_failures.csv"
        summary_path = run_output_dir / f"{run_id}_summary.json"

        existing_staging_records = self._load_existing_staging_records()
        existing_manual_reviews = self._load_existing_manual_reviews()

        inserted = 0
        skipped = 0
        manual_review_created = 0
        failed = 0
        status_counts: Counter[str] = Counter()

        fieldnames = [
            "run_id",
            "processed_at_utc",
            "retailer_key",
            "retailer",
            "source_file",
            "status",
            "staging_store_location_id",
            "manual_review_reason",
            "error_message",
            "staging_row_json",
        ]

        with (
            results_path.open("w", newline="", encoding="utf-8") as results_file,
            failures_path.open("w", newline="", encoding="utf-8") as failures_file,
        ):
            results_writer = csv.DictWriter(results_file, fieldnames=fieldnames)
            failures_writer = csv.DictWriter(failures_file, fieldnames=fieldnames)
            results_writer.writeheader()
            failures_writer.writeheader()

            raw_records = list(self.strategy.read_raw_records())

            for raw_record in tqdm(
                raw_records,
                desc=f"Ingesting {self.strategy.retailer_key}",
                unit="row",
            ):
                processed_at = datetime.now(timezone.utc).isoformat()

                try:
                    staging_record = self.strategy.to_staging_record(raw_record)

                    key = (
                        staging_record.retailer,
                        staging_record.store_number,
                    )

                    if (
                        staging_record.retailer is not None
                        and staging_record.store_number is not None
                        and key in existing_staging_records
                    ):
                        skipped += 1
                        status = "SKIPPED_ALREADY_INGESTED"
                        status_counts[status] += 1

                        results_writer.writerow(
                            self._build_audit_row(
                                run_id=run_id,
                                processed_at=processed_at,
                                status=status,
                                staging_record=staging_record,
                                source_path=self.strategy.input_path,
                                staging_store_location_id=existing_staging_records[key],
                            )
                        )
                        continue

                    response = (
                        self._supabase
                        .table(STAGING_TABLE)
                        .insert(staging_record.to_staging_payload())
                        .execute()
                    )

                    inserted_rows = response.data or []
                    if not inserted_rows:
                        raise RuntimeError(
                            "staging_store_locations insert returned no rows."
                        )

                    staging_id = int(inserted_rows[0]["id"])
                    inserted += 1

                    review_reason = self._build_manual_review_reason(staging_record)

                    if (
                        review_reason is not None
                        and staging_id not in existing_manual_reviews
                    ):
                        (
                            self._supabase
                            .table(MANUAL_REVIEW_TABLE)
                            .insert(
                                {
                                    "staging_store_location_id": staging_id,
                                    "reason": review_reason,
                                    "review_status": "pending",
                                    "review_notes": None,
                                }
                            )
                            .execute()
                        )
                        existing_manual_reviews.add(staging_id)
                        manual_review_created += 1

                    if (
                        staging_record.retailer is not None
                        and staging_record.store_number is not None
                    ):
                        existing_staging_records[
                            (
                                staging_record.retailer,
                                staging_record.store_number,
                            )
                        ] = staging_id

                    status = "SUCCESS"
                    status_counts[status] += 1
                    results_writer.writerow(
                        self._build_audit_row(
                            run_id=run_id,
                            processed_at=processed_at,
                            status=status,
                            staging_record=staging_record,
                            source_path=self.strategy.input_path,
                            staging_store_location_id=staging_id,
                            manual_review_reason=review_reason,
                        )
                    )

                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    status = "FAILED"
                    status_counts[status] += 1

                    failure_row = self._build_audit_row(
                        run_id=run_id,
                        processed_at=processed_at,
                        status=status,
                        staging_record=None,
                        source_path=self.strategy.input_path,
                        staging_store_location_id=None,
                        error_message=str(exc),
                        raw_record=raw_record,
                    )
                    results_writer.writerow(failure_row)
                    failures_writer.writerow(failure_row)

            results_file.flush()
            failures_file.flush()

        run_finished_at = datetime.now(timezone.utc)
        summary = {
            "run_id": run_id,
            "run_started_at_utc": run_started_at.isoformat(),
            "run_finished_at_utc": run_finished_at.isoformat(),
            "elapsed_seconds": round(
                (run_finished_at - run_started_at).total_seconds(),
                3,
            ),
            "retailer_key": self.strategy.retailer_key,
            "source_file": str(self.strategy.input_path),
            "output_dir": str(run_output_dir),
            "output_files": {
                "results_csv": str(results_path),
                "failures_csv": str(failures_path),
                "summary_json": str(summary_path),
            },
            "inserted": inserted,
            "skipped": skipped,
            "manual_review_created": manual_review_created,
            "failed": failed,
            "status_counts": dict(status_counts),
        }

        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return summary

    @staticmethod
    def _build_manual_review_reason(
        record: StagingStoreLocation,
    ) -> str | None:
        reasons: list[str] = []

        if record.store_number is None:
            reasons.append("MISSING_STORE_NUMBER")
        if record.street_address is None:
            reasons.append("MISSING_STREET_ADDRESS")
        if record.full_address is None:
            reasons.append("MISSING_FULL_ADDRESS")
        if record.address_city is None:
            reasons.append("MISSING_CITY")
        if record.address_state is None:
            reasons.append("MISSING_STATE")
        if record.zip_code is None:
            reasons.append("MISSING_ZIP_CODE")

        return ",".join(reasons) if reasons else None

    def _load_existing_staging_records(self) -> dict[tuple[str, str], int]:
        response = (
            self._supabase
            .table(STAGING_TABLE)
            .select("id, retailer, store_number")
            .execute()
        )

        existing: dict[tuple[str, str], int] = {}
        for row in response.data or []:
            retailer = self._clean(row.get("retailer"))
            store_number = self._clean(row.get("store_number"))

            if retailer is None or store_number is None:
                continue

            existing[(retailer, store_number)] = int(row["id"])

        return existing

    def _load_existing_manual_reviews(self) -> set[int]:
        response = (
            self._supabase
            .table(MANUAL_REVIEW_TABLE)
            .select("staging_store_location_id")
            .execute()
        )
        return {
            int(row["staging_store_location_id"])
            for row in response.data or []
        }

    @staticmethod
    def _build_audit_row(
        *,
        run_id: str,
        processed_at: str,
        status: str,
        staging_record: StagingStoreLocation | None,
        source_path: Path,
        staging_store_location_id: int | None,
        manual_review_reason: str | None = None,
        error_message: str | None = None,
        raw_record: Any = None,
    ) -> dict[str, Any]:
        audit_record = (
            raw_record
            if raw_record is not None
            else (
                staging_record.to_staging_payload()
                if staging_record is not None
                else None
            )
        )

        return {
            "run_id": run_id,
            "processed_at_utc": processed_at,
            "retailer_key": (
                staging_record.retailer
                if staging_record is not None
                else None
            ),
            "retailer": (
                staging_record.retailer
                if staging_record is not None
                else None
            ),
            "source_file": str(source_path),
            "status": status,
            "staging_store_location_id": staging_store_location_id,
            "manual_review_reason": manual_review_reason,
            "error_message": error_message,
            "staging_row_json": json.dumps(
                audit_record,
                ensure_ascii=False,
                default=str,
            ),
        }

    @staticmethod
    def _clean(value: Any) -> str | None:
        if value is None:
            return None

        text = str(value).strip()
        return text or None


def build_legacy_strategy(
    retailer_key: str,
    *,
    input_root: Path | None = None,
) -> LegacyStoreLocationIngestionStrategy:
    """Build the example strategy for one configured legacy retailer."""
    if retailer_key not in LEGACY_INPUTS:
        raise ValueError(f"Unknown legacy retailer: {retailer_key}")

    retailer_name, file_name = LEGACY_INPUTS[retailer_key]
    source_root = input_root or INPUT_ROOT

    return LegacyStoreLocationIngestionStrategy(
        retailer_key=retailer_key,
        retailer_name=retailer_name,
        input_path=source_root / file_name,
    )


def main() -> None:
    """Run the example legacy ingestion for Walmart."""
    strategy = build_legacy_strategy("walmart")
    service = StoreLocationIngestionService(strategy)
    summary = service.ingest()

    print()
    print("=" * 72)
    print("Store Location Ingestion Completed")
    print("=" * 72)
    print(f"Retailer: {strategy.retailer_key}")
    print(f"Inserted: {summary['inserted']}")
    print(f"Skipped: {summary['skipped']}")
    print(f"Manual Review Created: {summary['manual_review_created']}")
    print(f"Failed: {summary['failed']}")
    print(f"Output directory: {summary['output_dir']}")


if __name__ == "__main__":
    main()