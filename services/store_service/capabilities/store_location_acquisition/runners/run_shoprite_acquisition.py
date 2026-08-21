# services/store_service/capabilities/store_location_acquisition/runners/run_shoprite_acquisition.py

"""Executable runner for ShopRite store location acquisition."""

from __future__ import annotations

import csv
import json
from datetime import datetime

from services.store_service.capabilities.store_location_acquisition.constants import (
    OUTPUT_ROOT,
)
from services.store_service.capabilities.store_location_acquisition.strategies.shoprite_acquisition_strategy import (
    ShopRiteAcquisitionStrategy,
)


SHOPRITE_OUTPUT_ROOT = OUTPUT_ROOT / "shoprite"


def _serialize(value):
    """
    Serialize structured values for CSV output.

    :param value: Record value to serialize.
    :return: JSON text for structured values, otherwise the original value.
    """
    if isinstance(value, (dict, list)):
        return json.dumps(
            value,
            ensure_ascii=False,
        )

    return value


def main() -> None:
    """Run the ShopRite acquisition and write its output artifacts."""
    strategy = ShopRiteAcquisitionStrategy()

    output = strategy.acquire()

    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    # Store each acquisition run under a timestamped retailer directory.
    output_dir = SHOPRITE_OUTPUT_ROOT / timestamp
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    records = output["records"]
    validation = output["validation"]

    csv_path = (
        output_dir
        / f"{timestamp}_shoprite_us_locations.csv"
    )
    summary_path = (
        output_dir
        / f"{timestamp}_shoprite_summary.json"
    )

    if records:
        fields = list(
            records[0].keys()
        )

        with csv_path.open(
            "w",
            newline="",
            encoding="utf-8-sig",
        ) as file:
            writer = csv.DictWriter(
                file,
                fieldnames=fields,
            )
            writer.writeheader()

            for record in records:
                writer.writerow(
                    {
                        key: _serialize(value)
                        for key, value in record.items()
                    }
                )
    else:
        csv_path.write_text(
            "",
            encoding="utf-8",
        )

    summary = {
        "retailer": output["retailer"],
        "retailer_key": output["retailer_key"],
        "provider": output["provider"],
        "source_type": output["source_type"],
        "validation": validation,
        "probe_record_counts": output[
            "probe_record_counts"
        ],
        "http_status_counts": output[
            "http_status_counts"
        ],
        "request_error_counts": output[
            "request_error_counts"
        ],
        "failed_probes": output[
            "failed_probes"
        ],
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

    print()
    print("=" * 72)
    print("ShopRite Acquisition Completed")
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
        f"Missing addresses: "
        f"{validation['missing_addresses']}"
    )
    print(
        f"Missing phones: "
        f"{validation['missing_phones']}"
    )
    print(
        f"Non-US records: "
        f"{validation['non_us_records']}"
    )
    print(
        f"Duplicate store IDs: "
        f"{validation['duplicate_store_ids']}"
    )
    print(
        f"Failed probes: "
        f"{validation['failed_probes']}"
    )
    print(
        f"Excluded non-grocery records: "
        f"{validation['excluded_non_grocery_records']}"
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