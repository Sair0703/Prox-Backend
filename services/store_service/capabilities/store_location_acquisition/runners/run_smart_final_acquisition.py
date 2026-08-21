# services/store_service/capabilities/store_location_acquisition/runners/run_smart_final_acquisition.py

"""Executable runner for Smart & Final store location acquisition."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any, Mapping


CURRENT_FILE = Path(__file__).resolve()
PACKAGE_ROOT = CURRENT_FILE.parents[2]
PROJECT_ROOT = CURRENT_FILE.parents[5]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.store_service.capabilities.store_location_acquisition.constants import (
    OUTPUT_ROOT,
)
from services.store_service.capabilities.store_location_acquisition.strategies.smart_final_acquisition_strategy import (
    SmartFinalAcquisitionStrategy,
)


SMART_FINAL_OUTPUT_ROOT = OUTPUT_ROOT / "smart_final"

CSV_FIELDS = [
    "retailer",
    "retailer_key",
    "store_name",
    "retailer_store_id",
    "store_number",
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


def _write_csv(
    path: Path,
    records: list[Mapping[str, Any]],
) -> None:
    """
    Write acquired Smart & Final records to a CSV file.

    :param path: Destination CSV path.
    :param records: Normalized store records to write.
    """
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


def main() -> None:
    """Run the Smart & Final acquisition and write output artifacts."""
    strategy = SmartFinalAcquisitionStrategy()

    print("=" * 72)
    print("Smart & Final Acquisition")
    print("=" * 72)

    # Execute the acquisition stages directly so the runner owns output.
    source_info = strategy.discover_source()
    raw_artifacts = strategy.fetch_raw_artifacts()
    store_payloads = strategy.extract_store_payloads(
        raw_artifacts
    )
    validation = strategy.validate_store_payloads(
        store_payloads
    )

    run_started_at = datetime.now(timezone.utc)
    run_id = run_started_at.strftime(
        "%Y%m%d_%H%M%S"
    )

    # Store each acquisition run under a timestamped retailer directory.
    output_dir = (
        SMART_FINAL_OUTPUT_ROOT
        / run_id
    )
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    csv_path = (
        output_dir
        / f"{run_id}_smart_final_us_locations.csv"
    )
    summary_path = (
        output_dir
        / f"{run_id}_smart_final_summary.json"
    )

    _write_csv(
        csv_path,
        store_payloads,
    )

    summary = {
        "retailer": source_info.retailer_name,
        "retailer_key": source_info.retailer_key,
        "source_type": source_info.source_type,
        "provider": source_info.provider,
        "source_info": asdict(source_info),
        "validation": asdict(validation),
        "store_payload_count": len(
            store_payloads
        ),
        "raw_artifact_count": len(
            raw_artifacts
        ),
        "output_files": {
            "csv": str(csv_path),
            "summary": str(summary_path),
        },
        "run_started_at_utc": (
            run_started_at.isoformat()
        ),
        "run_finished_at_utc": (
            datetime.now(timezone.utc).isoformat()
        ),
        "run_notes": list(
            strategy.build_run_notes()
        ),
    }

    summary_path.write_text(
        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    print()
    print("=" * 72)
    print("Smart & Final Acquisition Completed")
    print("=" * 72)
    print(
        f"Retailer: "
        f"{source_info.retailer_name}"
    )
    print(
        f"Retailer key: "
        f"{source_info.retailer_key}"
    )
    print(
        f"Source type: "
        f"{source_info.source_type}"
    )
    print(
        f"Provider: "
        f"{source_info.provider}"
    )

    if validation is not None:
        print()
        print("Validation")
        print("-" * 72)
        print(
            f"Valid: "
            f"{validation.is_valid}"
        )
        print(
            f"Total records: "
            f"{validation.total_records}"
        )
        print(
            f"Unique store IDs: "
            f"{validation.unique_store_ids}"
        )
        print(
            f"Missing store IDs: "
            f"{validation.missing_store_ids}"
        )
        print(
            f"Missing coordinates: "
            f"{validation.missing_coordinates}"
        )
        print(
            f"Non-US records: "
            f"{validation.non_us_records}"
        )

        if validation.duplicate_store_ids:
            print()
            print("Duplicate store IDs:")

            for store_id in validation.duplicate_store_ids:
                print(
                    f"- {store_id}"
                )

        if validation.issue_counts:
            print()
            print("Issues:")

            for issue, count in sorted(
                validation.issue_counts.items()
            ):
                print(
                    f"- {issue}: {count}"
                )

        if validation.notes:
            print()
            print("Notes:")

            for note in validation.notes:
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