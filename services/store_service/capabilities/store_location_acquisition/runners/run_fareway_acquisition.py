# services/store_service/capabilities/store_location_acquisition/runners/run_fareway_acquisition.py

"""Executable runner for Fareway store location acquisition."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from services.store_service.capabilities.store_location_acquisition.constants import (
    OUTPUT_ROOT,
)
from services.store_service.capabilities.store_location_acquisition.strategies.fareway_acquisition_strategy import (
    FarewayAcquisitionStrategy,
)


FAREWAY_OUTPUT_ROOT = OUTPUT_ROOT / "fareway"


def _write_csv(
    path: Path,
    records: list[dict],
) -> None:
    """
    Write acquired Fareway records to a CSV file.

    :param path: Destination CSV path.
    :param records: Store records to write.
    """
    fields = (
        list(records[0].keys())
        if records
        else [
            "retailer",
            "retailer_key",
            "retailer_store_id",
            "store_number",
            "store_name",
            "address",
            "city",
            "state",
            "zip_code",
            "full_address",
            "phone",
            "latitude",
            "longitude",
            "store_url",
            "source",
            "source_type",
        ]
    )

    with path.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
        )
        writer.writeheader()
        writer.writerows(records)


def main() -> None:
    """Run the Fareway acquisition and write its output artifacts."""
    strategy = FarewayAcquisitionStrategy(
        workers=8,
        total_pages=18,
    )

    output = strategy.acquire()

    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    # Store each acquisition run under a timestamped retailer directory.
    output_dir = FAREWAY_OUTPUT_ROOT / timestamp
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    csv_path = (
        output_dir
        / f"{timestamp}_fareway_us_locations.csv"
    )

    summary_path = (
        output_dir
        / f"{timestamp}_fareway_summary.json"
    )

    _write_csv(
        csv_path,
        output["records"],
    )

    summary = {
        "retailer": output["retailer"],
        "retailer_key": output["retailer_key"],
        "source_type": output["source_type"],
        "provider": output["provider"],
        "validation": output["validation"],
        "page_urls": output["page_urls"],
        "failed_urls": output["failed_urls"],
        "notes": output["notes"],
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

    validation = output[
        "validation"
    ]

    print()
    print("=" * 72)
    print(
        "Fareway Acquisition Completed"
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
        f"Raw records: "
        f"{validation['raw_records']}"
    )
    print(
        f"Duplicate records merged: "
        f"{validation['duplicate_records_merged']}"
    )
    print(
        f"Missing store IDs: "
        f"{validation['missing_store_ids']}"
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
        f"Missing coordinates: "
        f"{validation['missing_coordinates']}"
    )
    print(
        f"Pages: "
        f"{validation['page_count']}"
    )
    print(
        f"Successful pages: "
        f"{validation['successful_pages']}"
    )
    print(
        f"Parse failures: "
        f"{validation['parse_failures']}"
    )
    print(
        f"Failed URLs: "
        f"{validation['failed_urls']}"
    )

    print()
    print("State counts")
    print("-" * 72)

    for state, count in sorted(
        validation[
            "state_counts"
        ].items()
    ):
        print(
            f"{state}: {count}"
        )

    if validation["issues"]:
        print()
        print("Issues:")

        for issue in validation[
            "issues"
        ]:
            print(
                f"- {issue}"
            )

    print()
    print("Notes:")

    for note in output["notes"]:
        print(
            f"- {note}"
        )

    print()
    print("Output files")
    print("-" * 72)
    print(
        f"- csv: {csv_path}"
    )
    print(
        f"- summary: {summary_path}"
    )
    print("=" * 72)


if __name__ == "__main__":
    main()