# services/store_service/capabilities/store_location_acquisition/runners/run_food_lion_acquisition.py

"""Executable runner for Food Lion store location acquisition."""

from __future__ import annotations

import csv
import json
import re
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


CURRENT_FILE = Path(__file__).resolve()
PROJECT_ROOT = CURRENT_FILE.parents[5]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.store_service.capabilities.store_location_acquisition.constants import (
    OUTPUT_ROOT,
)
from services.store_service.capabilities.store_location_acquisition.strategies.food_lion_acquisition_strategy import (
    FoodLionAcquisitionStrategyV2,
)


FOOD_LION_OUTPUT_ROOT = OUTPUT_ROOT / "food_lion"

CSV_FIELDS = [
    "retailer",
    "store_type",
    "store_number",
    "city_slug",
    "state",
    "store_url",
    "source_sitemap",
    "street_address",
    "address_city",
    "address_state",
    "zip_code",
    "full_address",
    "phone",
    "extraction_source",
    "scrape_status",
    "http_status",
    "error_message",
    "scraped_at_utc",
]


def _clean_text(value: Any) -> str | None:
    """
    Normalize a value into trimmed text.

    :param value: Value to normalize.
    :return: Trimmed text, or None for empty values.
    """
    if value is None:
        return None

    text = str(value).strip()
    return text or None


def _slugify(value: Any) -> str:
    """
    Convert a value into a filesystem-safe lowercase slug.

    :param value: Value to convert.
    :return: Normalized slug.
    """
    text = _clean_text(value) or ""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or "unknown"


def _build_csv_row(
    payload: Mapping[str, Any],
    source_info: Any,
) -> dict[str, Any]:
    """
    Convert a Food Lion payload into the shared acquisition CSV schema.

    :param payload: Store payload returned by the acquisition strategy.
    :param source_info: Source metadata returned by the strategy.
    :return: Normalized CSV row.
    """
    retailer = (
        _clean_text(payload.get("retailer"))
        or source_info.retailer_name
    )
    store_type = _clean_text(
        payload.get("store_type")
    )
    store_number = _clean_text(
        payload.get("retailer_store_id")
    )
    store_name = _clean_text(
        payload.get("store_name")
    )
    city = _clean_text(
        payload.get("city")
    )
    state = _clean_text(
        payload.get("state")
    )
    zip_code = _clean_text(
        payload.get("zip_code")
    )
    address = _clean_text(
        payload.get("address")
    )
    full_address = _clean_text(
        payload.get("full_address")
    )
    phone = _clean_text(
        payload.get("phone")
    )

    source_url = (
        _clean_text(payload.get("source_url"))
        or ""
    )
    extraction_source = (
        _clean_text(
            payload.get("extraction_source")
        )
        or _clean_text(
            payload.get("provider")
        )
        or source_url
        or source_info.endpoint_url
    )

    return {
        "retailer": retailer,
        "store_type": store_type or "Regular",
        "store_number": store_number,
        "city_slug": _slugify(
            city
            or store_name
            or store_number
        ),
        "state": state,
        "store_url": _clean_text(
            payload.get("store_url")
        ),
        "source_sitemap": _clean_text(
            payload.get("source_sitemap")
        ),
        "street_address": address,
        "address_city": city,
        "address_state": state,
        "zip_code": zip_code,
        "full_address": full_address,
        "phone": phone,
        "extraction_source": extraction_source,
        "scrape_status": "success",
        "http_status": payload.get(
            "http_status"
        ),
        "error_message": None,
        "scraped_at_utc": (
            _clean_text(
                payload.get("scraped_at_utc")
            )
            or datetime.now(
                timezone.utc
            ).isoformat()
        ),
    }


def _write_csv(
    path: Path,
    rows: list[dict[str, Any]],
) -> None:
    """
    Write normalized Food Lion records to a CSV file.

    :param path: Destination CSV path.
    :param rows: Normalized store records.
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
        writer.writerows(rows)


def main() -> None:
    """Run the Food Lion acquisition and write its output artifacts."""
    strategy = FoodLionAcquisitionStrategyV2(
        state_workers=16,
        schema_workers=32,
        parse_workers=32,
        request_timeout=30,
        max_retries=4,
        retry_backoff_base=1.0,
        retry_backoff_max=16.0,
        debug_failed_limit=25,
    )

    source_info = strategy.discover_source()

    print()
    print("=" * 72)
    print("Food Lion Acquisition v2")
    print("=" * 72)
    print(
        "Source: https://stores.foodlion.com/"
    )
    print(
        "Schema source: "
        "https://schema.milestoneinternet.com/"
        "schema/stores.foodlion.com/<path>/schema.json"
    )
    print(
        "Method: requests + BeautifulSoup for discovery; "
        "Milestone schema JSON for location/store acquisition"
    )
    print(
        "Hierarchy: root -> state schema -> "
        "city/store schema -> structured Store Info"
    )
    print(
        "Workers: "
        f"state={strategy.state_workers}, "
        f"schema={strategy.schema_workers}, "
        f"parse={strategy.parse_workers}"
    )
    print(
        "Retry: "
        f"max={strategy.max_retries}, "
        f"backoff={strategy.retry_backoff_base}s-"
        f"{strategy.retry_backoff_max}s"
    )
    print("Playwright: not required")
    print()

    run_started_at = datetime.now(timezone.utc)
    run_id = run_started_at.strftime(
        "%Y%m%d_%H%M%S"
    )

    # Execute the acquisition stages directly so the runner owns output.
    raw_artifacts = strategy.fetch_raw_artifacts()
    store_payloads = strategy.extract_store_payloads(
        raw_artifacts
    )
    validation = strategy.validate_store_payloads(
        store_payloads
    )

    records = [
        _build_csv_row(
            payload,
            source_info,
        )
        for payload in store_payloads
    ]

    # Store each acquisition run under a timestamped retailer directory.
    output_dir = FOOD_LION_OUTPUT_ROOT / run_id
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    csv_path = (
        output_dir
        / f"{run_id}_food_lion_us_locations.csv"
    )
    summary_path = (
        output_dir
        / f"{run_id}_food_lion_summary.json"
    )

    _write_csv(
        csv_path,
        records,
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
    print("Food Lion Acquisition Completed")
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
        f"Unique store references: "
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