# services/store_service/capabilities/store_location_acquisition/runners/run_stop_and_shop_acquisition.py

"""Executable runner for Stop & Shop store location acquisition."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from services.store_service.capabilities.store_location_acquisition.constants import (
    OUTPUT_ROOT,
)
from services.store_service.capabilities.store_location_acquisition.strategies.stop_and_shop_acquisition_strategy import (
    StopAndShopAcquisitionStrategy,
)


STOP_AND_SHOP_OUTPUT_ROOT = OUTPUT_ROOT / "stop_and_shop"


def _timestamp() -> str:
    """Return a filesystem-safe timestamp for the acquisition run."""
    return datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )


def _write_csv(
    path: Path,
    records: list[dict],
) -> None:
    """
    Write acquired Stop & Shop records to a CSV file.

    Serializes structured store-hours data as JSON before writing.

    :param path: Destination CSV path.
    :param records: Store records to write.
    """
    if not records:
        path.write_text(
            "",
            encoding="utf-8",
        )
        return

    fields = list(records[0].keys())

    with path.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fields,
        )
        writer.writeheader()

        for record in records:
            row = dict(record)

            if isinstance(
                row.get("hours"),
                list,
            ):
                row["hours"] = json.dumps(
                    row["hours"],
                    ensure_ascii=False,
                )

            writer.writerow(row)


def main() -> None:
    """Run the Stop & Shop acquisition and write its output artifacts."""
    strategy = StopAndShopAcquisitionStrategy()

    output = strategy.acquire()

    timestamp = _timestamp()

    # Store each acquisition run under a timestamped retailer directory.
    output_dir = STOP_AND_SHOP_OUTPUT_ROOT / timestamp
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    records = output["records"]
    validation = output["validation"]

    csv_path = (
        output_dir
        / f"{timestamp}_stop_and_shop_us_locations.csv"
    )

    summary_path = (
        output_dir
        / f"{timestamp}_stop_and_shop_summary.json"
    )

    _write_csv(
        csv_path,
        records,
    )

    summary = {
        "retailer": output["retailer"],
        "retailer_key": output[
            "retailer_key"
        ],
        "source_type": output[
            "source_type"
        ],
        "provider": output["provider"],
        "validation": validation,
        "notes": output["notes"],
        "http_status_counts": output[
            "http_status_counts"
        ],
        "request_error_counts": output[
            "request_error_counts"
        ],
        "declared_state_counts": output[
            "declared_state_counts"
        ],
        "declared_location_counts": output[
            "declared_location_counts"
        ],
        "failed_urls": strategy.failed_urls,
        "deferred_urls": strategy.deferred_urls,
        "output_files": {
            "csv": str(csv_path),
            "summary": str(summary_path),
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
    print(
        "Stop & Shop Acquisition Completed"
    )
    print("=" * 72)

    print(
        f"Retailer: {output['retailer']}"
    )
    print(
        f"Retailer key: {output['retailer_key']}"
    )
    print(
        f"Source type: {output['source_type']}"
    )
    print(
        f"Provider: {output['provider']}"
    )

    print()
    print("Validation")
    print("-" * 72)

    print(
        f"Valid: {validation['valid']}"
    )
    print(
        f"Total records: "
        f"{validation['total_records']}"
    )
    print(
        f"Unique store IDs: "
        f"{validation['unique_store_ids']}"
    )
    print(
        f"Missing store IDs: "
        f"{validation['missing_store_ids']}"
    )
    print(
        f"Missing coordinates: "
        f"{validation['missing_coordinates']}"
    )
    print(
        f"Duplicate address groups: "
        f"{validation['duplicate_address_groups']}"
    )
    print(
        f"Missing addresses: "
        f"{validation['missing_addresses']}"
    )
    print(
        f"Missing phones: "
        f"{validation['missing_phones']}"
    )
    print(
        f"Failed store pages: "
        f"{validation['failed_store_pages']}"
    )

    if validation["issues"]:
        print()
        print("Issues:")

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
    print("=" * 72)


if __name__ == "__main__":
    main()