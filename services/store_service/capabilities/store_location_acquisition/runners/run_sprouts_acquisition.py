# services/store_service/capabilities/store_location_acquisition/runners/run_whole_foods_acquisition.py

"""Executable runner for Whole Foods store location acquisition."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from services.store_service.capabilities.store_location_acquisition.constants import (
    OUTPUT_ROOT,
)
from services.store_service.capabilities.store_location_acquisition.strategies.whole_foods_acquisition_strategy import (
    WholeFoodsAcquisitionStrategy,
)


WHOLE_FOODS_OUTPUT_ROOT = OUTPUT_ROOT / "whole_foods"

CSV_FIELDS = [
    # Canonical Whole Foods acquisition schema.
    "retailer",
    "retailer_key",
    "store_name",
    "retailer_store_id",
    "store_number",
    "source_location_id",
    "address",
    "city",
    "state",
    "zip_code",
    "full_address",
    "phone",
    "latitude",
    "longitude",
    "whole_foods_market_folder",
    "location_type",
    "brand",
    "seed_state",
    "seed_county",
    "seed_zip",
    "seed_state_all",
    "seed_county_all",
    "seed_zip_all",
    "source",
    "source_type",
]


def _write_csv(
    path: Path,
    records: list[dict],
) -> None:
    """Write normalized Whole Foods records to a CSV file."""
    with path.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=CSV_FIELDS,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(records)


def _write_seeds(
    path: Path,
    seeds: list[dict],
) -> None:
    """Write the county/ZIP coverage seeds used by the acquisition."""
    with path.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "state",
                "county",
                "zip_code",
            ],
        )
        writer.writeheader()
        writer.writerows(seeds)


def _write_failed(
    path: Path,
    failed: list[dict],
) -> None:
    """Write failed acquisition seeds and request diagnostics."""
    with path.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "state",
                "county",
                "zip_code",
                "error",
                "status_code",
                "final_url",
                "content_type",
                "html_length",
                "debug_html",
            ],
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(failed)


def _write_empty(
    path: Path,
    empty: list[dict],
) -> None:
    """Write successfully queried seeds that returned no store cards."""
    with path.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "state",
                "county",
                "zip_code",
                "status_code",
                "final_url",
                "content_type",
                "html_length",
                "card_marker_count",
            ],
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(empty)


def main() -> None:
    """Run the Whole Foods acquisition and write its output artifacts."""
    # Quick test first. Increase/remove seed_limit after confirming
    # non-empty seeds and duplicate behaviour.
    seed_limit = None

    strategy = WholeFoodsAcquisitionStrategy(
        min_delay=0.3,
        max_delay=1.0,
        zips_per_county=1,
        max_consecutive_hard_failures=3,
    )

    print("=" * 72)
    print("Whole Foods Acquisition Strategy v3")
    print("=" * 72)
    print("Source: Whole Foods official APLF store locator")
    print("Method: requests + BeautifulSoup")
    print(
        "Hierarchy: US county -> representative ZIP -> "
        "store cards -> merge"
    )
    print("Store ID: storeCode when exposed; fallback otherwise")
    print("Workers: 1")
    print("Random delay: 1.0-2.5 seconds")
    print("Retry backoff: 2/4/8/16 seconds")
    print("Fail-fast: 3 consecutive hard failures only")
    print(f"Seed limit: {seed_limit}")
    print()

    output = strategy.acquire(
        seed_limit=seed_limit
    )

    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    # Store each acquisition run under a timestamped retailer directory.
    output_dir = WHOLE_FOODS_OUTPUT_ROOT / timestamp
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    csv_path = (
        output_dir
        / f"{timestamp}_whole_foods_us_locations.csv"
    )
    summary_path = (
        output_dir
        / f"{timestamp}_whole_foods_summary.json"
    )
    seed_path = (
        output_dir
        / f"{timestamp}_whole_foods_county_zip_seeds.csv"
    )
    failed_path = (
        output_dir
        / f"{timestamp}_whole_foods_failed_seeds.csv"
    )
    empty_path = (
        output_dir
        / f"{timestamp}_whole_foods_empty_seeds.csv"
    )

    _write_csv(
        csv_path,
        output["records"],
    )
    _write_seeds(
        seed_path,
        output["county_zip_seeds"],
    )
    _write_failed(
        failed_path,
        output["failed_seeds"],
    )
    _write_empty(
        empty_path,
        output["empty_seeds"],
    )

    validation = output["validation"]

    summary = {
        "retailer": output["retailer"],
        "retailer_key": output["retailer_key"],
        "source_type": output["source_type"],
        "validation": validation,
        "failed_seeds": output["failed_seeds"],
        "empty_seeds": output["empty_seeds"],
        "excluded_cards_count": len(
            output["excluded_cards"]
        ),
        "notes": output["notes"],
        "output_files": {
            "csv": str(csv_path),
            "summary": str(summary_path),
            "county_zip_seeds": str(seed_path),
            "failed_seeds": str(failed_path),
            "empty_seeds": str(empty_path),
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

    print()
    print("=" * 72)
    print("Whole Foods Acquisition Completed")
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
        "raw_card_records",
        "duplicate_records_merged",
        "with_store_id",
        "missing_store_id",
        "missing_addresses",
        "missing_phones",
        "missing_coordinates",
        "county_zip_seeds",
        "successful_nonempty_seeds",
        "successful_empty_seeds",
        "failed_seeds",
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

    if validation["issues"]:
        print()
        print("Issues")
        print("-" * 72)

        for issue in validation["issues"]:
            print(f"- {issue}")

    print()
    print("Notes:")

    for note in output["notes"]:
        print(f"- {note}")

    print()
    print("Output files")
    print("-" * 72)
    print(f"- csv: {csv_path}")
    print(f"- summary: {summary_path}")
    print(f"- county ZIP seeds: {seed_path}")
    print(f"- failed seeds: {failed_path}")
    print(f"- empty seeds: {empty_path}")
    print("=" * 72)


if __name__ == "__main__":
    main()