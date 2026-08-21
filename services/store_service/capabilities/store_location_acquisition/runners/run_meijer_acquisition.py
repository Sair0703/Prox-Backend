# services/store_service/capabilities/store_location_acquisition/runners/run_meijer_acquisition.py

"""Executable runner for Meijer store location acquisition."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from services.store_service.capabilities.store_location_acquisition.constants import (
    OUTPUT_ROOT,
)
from services.store_service.capabilities.store_location_acquisition.strategies.meijer_acquisition_strategy import (
    MeijerAcquisitionStrategy,
)


MEIJER_OUTPUT_ROOT = OUTPUT_ROOT / "meijer"


def _write_csv(
    path: Path,
    records: list[dict],
) -> None:
    """
    Write acquired Meijer records to a CSV file.

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
    """Run the Meijer acquisition and write its output artifacts."""
    strategy = MeijerAcquisitionStrategy(
        radius_miles=1000,
        workers=8,
    )

    output = strategy.acquire()

    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    # Store each acquisition run under a timestamped retailer directory.
    output_dir = MEIJER_OUTPUT_ROOT / timestamp
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    csv_path = (
        output_dir
        / f"{timestamp}_meijer_us_locations.csv"
    )

    summary_path = (
        output_dir
        / f"{timestamp}_meijer_summary.json"
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
        "seed_count": output["seed_count"],
        "successful_seeds": (
            output["successful_seeds"]
        ),
        "failed_seeds": (
            output["failed_seeds"]
        ),
        "seed_results": (
            output["seed_results"]
        ),
        "http_status_counts": (
            output["http_status_counts"]
        ),
        "notes": output["notes"],
        "output_files": {
            "csv": str(
                csv_path
            ),
            "summary": str(
                summary_path
            ),
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
        "Meijer Acquisition Completed"
    )
    print("=" * 72)
    print(
        f"Retailer: "
        f"{output['retailer']}"
    )
    print(
        f"Retailer key: "
        f"{output['retailer_key']}"
    )
    print(
        f"Source type: "
        f"{output['source_type']}"
    )

    print()
    print("Validation")
    print("-" * 72)

    print(
        f"Valid: "
        f"{validation['valid']}"
    )
    print(
        f"Total unique records: "
        f"{validation['total_records']}"
    )
    print(
        f"Unique store IDs: "
        f"{validation['unique_store_ids']}"
    )
    print(
        f"Raw records before merge: "
        f"{validation['raw_record_count']}"
    )
    print(
        f"Excluded without store ID: "
        f"{validation['excluded_without_store_id']}"
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
        f"Missing coordinates: "
        f"{validation['missing_coordinates']}"
    )
    print(
        f"Missing phones: "
        f"{validation['missing_phones']}"
    )
    print(
        f"Geographic seeds: "
        f"{validation['seed_count']}"
    )
    print(
        f"Successful seeds: "
        f"{validation['successful_seeds']}"
    )
    print(
        f"Failed seeds: "
        f"{validation['failed_seeds']}"
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

        for issue in validation["issues"]:
            print(
                f"- {issue}"
            )

    print()
    print("Notes:")

    for note in output[
        "notes"
    ]:
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