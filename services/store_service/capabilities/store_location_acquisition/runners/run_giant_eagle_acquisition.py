# services/store_service/capabilities/store_location_acquisition/runners/run_giant_eagle_acquisition.py

"""Executable runner for Giant Eagle store location acquisition."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from services.store_service.capabilities.store_location_acquisition.constants import (
    OUTPUT_ROOT,
)
from services.store_service.capabilities.store_location_acquisition.strategies.giant_eagle_acquisition_strategy import (
    GiantEagleAcquisitionStrategy,
)


GIANT_EAGLE_OUTPUT_ROOT = OUTPUT_ROOT / "giant_eagle"

CSV_FIELDS = [
    "retailer",
    "retailer_key",
    "store_name",
    "retailer_store_id",
    "store_number",
    "store_slug",
    "address",
    "address2",
    "city",
    "state",
    "zip_code",
    "full_address",
    "latitude",
    "longitude",
    "pickup_available",
    "delivery_available",
    "instore_available",
    "scan_pay_go_legacy",
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


def main() -> None:
    """Run the Giant Eagle acquisition and write its output artifacts."""
    seed_limit = None

    strategy = GiantEagleAcquisitionStrategy(
        page_size=50,
        min_delay=1.0,
        max_delay=2.5,
    )

    print("=" * 72)
    print("Giant Eagle Acquisition Strategy v2")
    print("=" * 72)
    print("Source: https://core.shop.gianteagle.com/api/v2")
    print("Method: requests + JSON")
    print(
        "Hierarchy: regional ZIP -> GetStores -> "
        "cursor pagination -> filter -> merge"
    )
    print("Store ID: official Store.code")
    print("Coordinates: official address.location")
    print("States: OH, PA, WV, MD, IN")
    print("Browsing modes: pickup + delivery")
    print("Page size: 50")
    print("Workers: 1")
    print("Random delay: 1-2.5 seconds")
    print("Retry: max=4, backoff=(2, 4, 8, 16)")
    print("Fail-fast: after 3 consecutive hard seed failures")
    print(
        "Seed limit: "
        f"{seed_limit if seed_limit is not None else 'all regional seeds'}"
    )
    print()

    output = strategy.acquire(
        seed_limit=seed_limit
    )

    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    # Store each acquisition run under a timestamped retailer directory.
    output_dir = GIANT_EAGLE_OUTPUT_ROOT / timestamp
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    csv_path = (
        output_dir
        / f"{timestamp}_giant_eagle_us_locations.csv"
    )
    summary_path = (
        output_dir
        / f"{timestamp}_giant_eagle_summary.json"
    )
    failed_path = (
        output_dir
        / f"{timestamp}_giant_eagle_failed_seeds.csv"
    )
    empty_path = (
        output_dir
        / f"{timestamp}_giant_eagle_empty_seeds.csv"
    )
    excluded_path = (
        output_dir
        / f"{timestamp}_giant_eagle_excluded_records.csv"
    )
    seed_path = (
        output_dir
        / f"{timestamp}_giant_eagle_seed_definitions.csv"
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
        excluded_path,
        output["excluded_records"],
        [
            "reason",
            "query_zip",
            "store_name",
            "store_code",
            "slug",
            "state",
            "city",
            "zip_code",
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
        "validation": output["validation"],
        "failed_seeds": output["failed_seeds"],
        "empty_seeds": output["empty_seeds"],
        "excluded_records": output["excluded_records"],
        "notes": output["notes"],
        "output_files": {
            "csv": str(csv_path),
            "summary": str(summary_path),
            "failed_seeds": str(failed_path),
            "empty_seeds": str(empty_path),
            "excluded_records": str(excluded_path),
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
    print("Giant Eagle Acquisition Completed")
    print("=" * 72)
    print(f"Retailer: {output['retailer']}")
    print(f"Retailer key: {output['retailer_key']}")
    print(f"Source type: {output['source_type']}")

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
        "missing_coordinates",
        "regional_zip_seeds",
        "successful_seed_queries",
        "empty_seed_queries",
        "failed_seed_queries",
        "stopped_early",
    ):
        print(
            f"{key}: {validation[key]}"
        )

    print(
        f"excluded_records: "
        f"{len(output['excluded_records'])}"
    )

    print()
    print("State counts")
    print("-" * 72)

    for state, count in validation["state_counts"].items():
        print(f"{state}: {count}")

    print()
    print("Store name counts")
    print("-" * 72)

    for name, count in validation["store_name_counts"].items():
        print(f"{name}: {count}")

    print()
    print("Output files")
    print("-" * 72)
    print(f"- csv: {csv_path}")
    print(f"- summary: {summary_path}")
    print(f"- failed seeds: {failed_path}")
    print(f"- empty seeds: {empty_path}")
    print(f"- excluded records: {excluded_path}")
    print(f"- seed definitions: {seed_path}")
    print("=" * 72)


if __name__ == "__main__":
    main()