# services/store_service/capabilities/store_location_acquisition/runners/run_heb_acquisition.py

"""Executable runner for H-E-B store location acquisition."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from services.store_service.capabilities.store_location_acquisition.constants import (
    OUTPUT_ROOT,
)
from services.store_service.capabilities.store_location_acquisition.strategies.heb_acquisition_strategy import (
    HEBAcquisitionStrategy,
)


HEB_OUTPUT_ROOT = OUTPUT_ROOT / "heb"

CSV_FIELDS = [
    "retailer",
    "retailer_key",
    "store_name",
    "retailer_store_id",
    "store_number",
    "retail_format_code",
    "address",
    "city",
    "state",
    "zip_code",
    "full_address",
    "phone",
    "latitude",
    "longitude",
    "pharmacy_store",
    "area_names",
    "store_fulfillments",
    "distance_miles",
    "source",
    "source_type",
    "query_zip",
    "query_zip_all",
]


def _write_csv(
    path: Path,
    rows: list[dict],
    fieldnames: list[str],
) -> None:
    """
    Write acquisition records to a CSV file.

    :param path: Destination path for the CSV file.
    :param rows: Records to write.
    :param fieldnames: Ordered CSV column names.
    """
    with path.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def _make_checkpoint_callback(
    checkpoint_dir: Path,
):
    """
    Create a callback for persisting periodic acquisition checkpoints.

    :param checkpoint_dir: Directory where checkpoint artifacts are written.
    :return: Callback used by the acquisition strategy.
    """
    checkpoint_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    def callback(
        *,
        completed: int,
        total: int,
        records: list[dict[str, Any]],
        failed_seeds: list[dict[str, Any]],
        empty_seeds: list[dict[str, Any]],
    ) -> None:
        """
        Persist the current acquisition records and progress.

        :param completed: Number of completed ZIP queries.
        :param total: Total number of ZIP queries in the run.
        :param records: Unique store records collected so far.
        :param failed_seeds: ZIP seeds that failed acquisition.
        :param empty_seeds: ZIP seeds that returned no stores.
        """
        _write_csv(
            checkpoint_dir / "heb_checkpoint_locations.csv",
            records,
            CSV_FIELDS,
        )

        (
            checkpoint_dir / "heb_checkpoint_progress.json"
        ).write_text(
            json.dumps(
                {
                    "completed_zip_queries": completed,
                    "total_zip_queries": total,
                    "unique_stores": len(records),
                    "failed_zip_queries": len(failed_seeds),
                    "empty_zip_queries": len(empty_seeds),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    return callback


def main() -> None:
    """Run the H-E-B acquisition and write its output artifacts."""
    seed_limit = None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = HEB_OUTPUT_ROOT / timestamp

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    strategy = HEBAcquisitionStrategy(
        min_delay=0.0,
        max_delay=0.15,
        workers=24,
        checkpoint_callback=_make_checkpoint_callback(
            output_dir,
        ),
        checkpoint_every=100,
    )

    print("=" * 72)
    print("H-E-B Acquisition Strategy v3")
    print("=" * 72)
    print("Source: official H-E-B Next.js store-locations JSON")
    print("Method: requests + JSON")
    print("Hierarchy: Texas regional ZIP -> page pagination -> merge")
    print("Store ID: official storeNumber")
    print("Coordinates: official latitude / longitude")
    print("Scope: Texas / US")
    print("Workers: 24")
    print("Random delay: 0.0-0.15 seconds")
    print("Retry: max=4, backoff=(2, 4, 8, 16)")
    print("Fail-fast: terminate on stale H-E-B Next.js build ID")
    print(
        "Seed limit: "
        f"{seed_limit if seed_limit is not None else 'all regional seeds'}"
    )
    print()

    output = strategy.acquire(
        seed_limit=seed_limit,
    )

    # Build timestamped output paths for the completed acquisition.
    csv_path = (
        output_dir
        / f"{timestamp}_heb_us_locations.csv"
    )
    summary_path = (
        output_dir
        / f"{timestamp}_heb_summary.json"
    )
    failed_path = (
        output_dir
        / f"{timestamp}_heb_failed_seeds.csv"
    )
    empty_path = (
        output_dir
        / f"{timestamp}_heb_empty_seeds.csv"
    )
    seed_path = (
        output_dir
        / f"{timestamp}_heb_seed_definitions.csv"
    )

    _write_csv(
        csv_path,
        output["records"],
        CSV_FIELDS,
    )

    _write_csv(
        failed_path,
        output["failed_seeds"],
        [
            "zip_code",
            "label",
            "attempts",
            "error",
        ],
    )

    _write_csv(
        empty_path,
        output["empty_seeds"],
        [
            "zip_code",
            "label",
        ],
    )

    _write_csv(
        seed_path,
        output["seed_definitions"],
        [
            "zip_code",
            "label",
        ],
    )

    summary = {
        "retailer": output["retailer"],
        "retailer_key": output["retailer_key"],
        "source_type": output["source_type"],
        "build_id": output["build_id"],
        "validation": output["validation"],
        "failed_seeds": output["failed_seeds"],
        "empty_seeds": output["empty_seeds"],
        "notes": output["notes"],
        "output_files": {
            "csv": str(csv_path),
            "summary": str(summary_path),
            "failed_seeds": str(failed_path),
            "empty_seeds": str(empty_path),
            "seed_definitions": str(seed_path),
        },
    }

    summary_path.write_text(
        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    validation = output["validation"]

    print()
    print("=" * 72)
    print("H-E-B Acquisition Completed")
    print("=" * 72)
    print(f"Retailer: {output['retailer']}")
    print(f"Retailer key: {output['retailer_key']}")
    print(f"Source type: {output['source_type']}")
    print(f"Next.js build ID: {output['build_id']}")

    print()
    print("Validation")
    print("-" * 72)

    for key in (
        "valid",
        "total_records",
        "unique_store_ids",
        "raw_api_records",
        "duplicate_records_merged",
        "missing_store_ids",
        "missing_addresses",
        "missing_phones",
        "missing_coordinates",
        "regional_zip_seeds",
        "successful_seed_queries",
        "empty_seed_queries",
        "failed_seed_queries",
        "max_reported_seed_total",
        "stopped_early",
    ):
        print(
            f"{key}: {validation[key]}"
        )

    print()
    print("State counts")
    print("-" * 72)

    for state, count in validation["state_counts"].items():
        print(f"{state}: {count}")

    print()
    print("Retail format counts")
    print("-" * 72)

    for format_code, count in validation["retail_format_counts"].items():
        print(f"{format_code}: {count}")

    print()
    print("Notes:")

    for note in output["notes"]:
        print(f"- {note}")

    print()
    print("Output files")
    print("-" * 72)
    print(f"- csv: {csv_path}")
    print(f"- summary: {summary_path}")
    print(f"- failed seeds: {failed_path}")
    print(f"- empty seeds: {empty_path}")
    print(f"- seed definitions: {seed_path}")
    print("=" * 72)


if __name__ == "__main__":
    main()