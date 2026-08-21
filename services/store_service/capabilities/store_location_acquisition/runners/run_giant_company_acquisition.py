# services/store_service/capabilities/store_location_acquisition/runners/run_giant_company_acquisition.py

"""Executable runner for The GIANT Company store location acquisition."""

from __future__ import annotations

import asyncio
import csv
import json
import os
from datetime import datetime
from pathlib import Path

from services.store_service.capabilities.store_location_acquisition.constants import (
    OUTPUT_ROOT,
)
from services.store_service.capabilities.store_location_acquisition.strategies.giant_company_acquisition_strategy import (
    GiantCompanyAcquisitionStrategy,
)


GIANT_COMPANY_OUTPUT_ROOT = OUTPUT_ROOT / "giant_company"

CSV_FIELDS = [
    "retailer",
    "retailer_key",
    "store_name",
    "opco",
    "retailer_store_id",
    "store_number",
    "backend_location_id",
    "backend_location_ids_all",
    "address",
    "address2",
    "city",
    "state",
    "zip_code",
    "full_address",
    "phone",
    "longitude",
    "latitude",
    "ecomm_store_id",
    "pickup_location_id",
    "service_type",
    "pickup_point_type",
    "store_id",
    "site",
    "source",
    "source_type",
    "query_zip",
    "query_zip_all",
    "query_distance_miles",
    "distance_from_query",
]


def _write_csv(
    path: Path,
    records: list[dict],
) -> None:
    """
    Write acquired GIANT Company records to a CSV file.

    :param path: Destination CSV path.
    :param records: Store records to write.
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


def _write_simple_csv(
    path: Path,
    fieldnames: list[str],
    rows: list[dict],
) -> None:
    """
    Write an acquisition diagnostic dataset to a CSV file.

    :param path: Destination CSV path.
    :param fieldnames: Ordered CSV column names.
    :param rows: Diagnostic records to write.
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


async def async_main() -> None:
    """Run the asynchronous GIANT Company acquisition and write output artifacts."""
    cdp_url = os.getenv(
        "GIANT_CHROME_CDP_URL"
    )

    seed_limit = None

    strategy = GiantCompanyAcquisitionStrategy(
        min_delay=2.0,
        max_delay=5.0,
        cdp_url=cdp_url,
        persistent_profile_dir=(
            ".giant_company_chrome_profile"
        ),
    )

    print("=" * 72)
    print(
        "The GIANT Company Acquisition Strategy v4"
    )
    print("=" * 72)
    print(
        "Sources: GNTC giantfoodstores.com/api/v6.0/serviceLocations "
        "+ MRTN martinsfoods.com/api/v5.0/serviceLocations"
    )
    print(
        "Method: Playwright browser session + official JSON API"
    )
    print(
        "Hierarchy: regional ZIP -> GNTC + MRTN -> radius=30 -> merge"
    )
    print(
        "Store ID: (opco, locationNumber)"
    )
    print(
        "Backend location ID: official id, preserved separately"
    )
    print(
        "Coordinates: official location=[longitude, latitude]"
    )
    print(
        "Banners: GNTC + MRTN"
    )
    print(
        "Radius: 30 miles"
    )
    print(
        "Fetch cap: 100"
    )
    print(
        "Workers: 1"
    )
    print(
        "Random delay: 2-5 seconds"
    )
    print(
        "Retry: max=3, backoff=(5, 10, 20)"
    )
    print(
        "Fail-fast: after 3 consecutive hard failures"
    )
    print(
        "Seed limit: "
        f"{seed_limit if seed_limit is not None else 'all regional seeds'}"
    )
    print(
        "Chrome CDP: "
        f"{cdp_url or 'disabled (persistent Chrome profile fallback)'}"
    )
    print()

    output = await strategy.acquire(
        seed_limit=seed_limit
    )

    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    # Store each acquisition run under a timestamped retailer directory.
    output_dir = GIANT_COMPANY_OUTPUT_ROOT / timestamp
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    csv_path = (
        output_dir
        / f"{timestamp}_giant_company_us_locations.csv"
    )
    summary_path = (
        output_dir
        / f"{timestamp}_giant_company_summary.json"
    )
    failed_path = (
        output_dir
        / f"{timestamp}_giant_company_failed_queries.csv"
    )
    saturated_path = (
        output_dir
        / f"{timestamp}_giant_company_saturated_queries.csv"
    )
    empty_path = (
        output_dir
        / f"{timestamp}_giant_company_empty_queries.csv"
    )
    excluded_path = (
        output_dir
        / f"{timestamp}_giant_company_excluded_records.csv"
    )
    seed_path = (
        output_dir
        / f"{timestamp}_giant_company_seed_definitions.csv"
    )

    _write_csv(
        csv_path,
        output["records"],
    )

    _write_simple_csv(
        failed_path,
        [
            "opco",
            "zip_code",
            "label",
            "attempts",
            "error",
        ],
        output["failed_seeds"],
    )

    _write_simple_csv(
        saturated_path,
        [
            "opco",
            "zip_code",
            "label",
            "raw_record_count",
            "fetch_cap",
        ],
        output["saturated_seeds"],
    )

    _write_simple_csv(
        empty_path,
        [
            "opco",
            "zip_code",
            "label",
        ],
        output["empty_seeds"],
    )

    _write_simple_csv(
        excluded_path,
        [
            "reason",
            "opco",
            "seed_zip",
            "location_id",
            "location_number",
        ],
        output["excluded_records"],
    )

    _write_simple_csv(
        seed_path,
        [
            "zip_code",
            "label",
        ],
        output["seed_definitions"],
    )

    summary = {
        "retailer": output["retailer"],
        "retailer_key": output["retailer_key"],
        "source_type": output["source_type"],
        "validation": output["validation"],
        "failed_seeds": output["failed_seeds"],
        "empty_seeds": output["empty_seeds"],
        "saturated_seeds": output["saturated_seeds"],
        "notes": output["notes"],
        "output_files": {
            "csv": str(csv_path),
            "summary": str(summary_path),
            "failed_queries": str(failed_path),
            "saturated_queries": str(saturated_path),
            "empty_queries": str(empty_path),
            "excluded_records": str(
                excluded_path
            ),
            "seed_definitions": str(
                seed_path
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

    validation = output["validation"]

    print()
    print("=" * 72)
    print(
        "The GIANT Company Acquisition Completed"
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

    for key in (
        "valid",
        "total_records",
        "unique_store_ids",
        "raw_api_records",
        "eligible_raw_records",
        "duplicate_records_merged",
        "missing_store_ids",
        "missing_addresses",
        "missing_phones",
        "missing_coordinates",
        "regional_zip_seeds",
        "total_queries",
        "successful_seed_queries",
        "empty_seed_queries",
        "failed_seed_queries",
        "queries_hitting_fetch_cap",
        "excluded_records",
        "stopped_early",
    ):
        print(
            f"{key}: {validation[key]}"
        )

    print()
    print("State counts")
    print("-" * 72)

    for state, count in (
        validation[
            "state_counts"
        ].items()
    ):
        print(
            f"{state}: {count}"
        )

    print()
    print("Opco counts")
    print("-" * 72)

    for opco, count in (
        validation[
            "opco_counts"
        ].items()
    ):
        print(
            f"{opco}: {count}"
        )

    print()
    print("Store name counts")
    print("-" * 72)

    for name, count in (
        validation[
            "store_name_counts"
        ].items()
    ):
        print(
            f"{name}: {count}"
        )

    if validation[
        "queries_hitting_fetch_cap"
    ]:
        print()
        print("WARNING")
        print("-" * 72)
        print(
            "Some opco/ZIP queries returned the API fetch cap of "
            "100 records. Those regions may require additional "
            "nearby ZIP seeds for complete coverage."
        )

    if validation[
        "failed_seed_queries"
    ]:
        print()
        print("Failed queries")
        print("-" * 72)

        for item in output[
            "failed_seeds"
        ]:
            print(
                f"- {item['opco']} "
                f"{item['zip_code']}: "
                f"{item['error']}"
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
    print(
        f"- failed queries: {failed_path}"
    )
    print(
        f"- saturated queries: {saturated_path}"
    )
    print(
        f"- empty queries: {empty_path}"
    )
    print(
        f"- excluded records: {excluded_path}"
    )
    print(
        f"- seed definitions: {seed_path}"
    )

    print("=" * 72)


def main() -> None:
    """Run the asynchronous acquisition from a synchronous entry point."""
    asyncio.run(
        async_main()
    )


if __name__ == "__main__":
    main()