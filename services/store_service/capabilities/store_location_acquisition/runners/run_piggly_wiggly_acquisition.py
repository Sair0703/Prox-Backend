# services/store_service/capabilities/store_location_acquisition/runners/run_piggly_wiggly_acquisition.py

"""Executable runner for Piggly Wiggly store location acquisition."""

from __future__ import annotations

import csv
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from services.store_service.capabilities.store_location_acquisition.strategies.piggly_wiggly_acquisition_strategy import (
    PigglyWigglyAcquisitionStrategy,
)


OUTPUT_ROOT = (
    Path(__file__).resolve().parents[2]
    / "output"
    / "piggly_wiggly"
)

CSV_FIELDNAMES = [
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


def clean(value: Any) -> str | None:
    """
    Normalize a value to trimmed text.

    :param value: Value to normalize.
    :return: Trimmed text, or None for empty values.
    """
    if value is None:
        return None

    text = str(value).strip()
    return text or None


def first_text(
    payload: Mapping[str, Any],
    keys: Sequence[str],
) -> str | None:
    """
    Return the first non-empty textual value for the given keys.

    :param payload: Source payload containing candidate fields.
    :param keys: Candidate keys ordered by preference.
    :return: First available normalized text value, or None.
    """
    for key in keys:
        value = clean(payload.get(key))
        if value is not None:
            return value

    return None


def first_number(
    payload: Mapping[str, Any],
    keys: Sequence[str],
) -> float | None:
    """
    Return the first numeric value for the given keys.

    :param payload: Source payload containing candidate fields.
    :param keys: Candidate keys ordered by preference.
    :return: First parsable numeric value, or None.
    """
    for key in keys:
        value = payload.get(key)

        if value is None or isinstance(value, bool):
            continue

        try:
            return float(value)
        except (TypeError, ValueError):
            continue

    return None


def build_full_address(
    address: str | None,
    city: str | None,
    state: str | None,
    zip_code: str | None,
) -> str | None:
    """
    Compose available address fields into a single string.

    :param address: Street address.
    :param city: City or locality.
    :param state: State abbreviation.
    :param zip_code: Postal code.
    :return: Combined address, or None when no address data exists.
    """
    if not any((address, city, state, zip_code)):
        return None

    if city and state and zip_code:
        locality = f"{city}, {state} {zip_code}"
    elif city and state:
        locality = f"{city}, {state}"
    elif city and zip_code:
        locality = f"{city} {zip_code}"
    else:
        locality = city or state or zip_code

    return ", ".join(
        part
        for part in (address, locality)
        if part
    )


def normalize_payload(
    payload: Mapping[str, Any],
    strategy: PigglyWigglyAcquisitionStrategy,
) -> dict[str, Any]:
    """
    Normalize a raw acquisition payload into the common store schema.

    :param payload: Raw store payload extracted by the acquisition strategy.
    :param strategy: Acquisition strategy providing retailer metadata.
    :return: Normalized store-location record.
    """
    address = first_text(
        payload,
        ("address", "street_address", "address_line1", "street"),
    )
    city = first_text(
        payload,
        ("city", "address_city", "locality"),
    )
    state = first_text(
        payload,
        ("state", "address_state", "state_code", "region"),
    )
    zip_code = first_text(
        payload,
        ("zip_code", "postal_code", "zipcode"),
    )

    return {
        "retailer": first_text(
            payload,
            ("retailer",),
        ) or strategy.retailer_name,
        "retailer_key": first_text(
            payload,
            ("retailer_key",),
        ) or strategy.retailer_key,
        "store_name": first_text(
            payload,
            ("store_name", "name"),
        ),
        "retailer_store_id": first_text(
            payload,
            (
                "retailer_store_id",
                "store_number",
                "location_number",
                "store_code",
            ),
        ),
        "store_number": first_text(
            payload,
            (
                "store_number",
                "retailer_store_id",
                "location_number",
                "store_code",
            ),
        ),
        "address": address,
        "city": city,
        "state": state,
        "zip_code": zip_code,
        "full_address": first_text(
            payload,
            ("full_address", "fullAddress"),
        ) or build_full_address(
            address,
            city,
            state,
            zip_code,
        ),
        "phone": first_text(
            payload,
            ("phone", "phone_number", "phoneNumber"),
        ),
        "latitude": first_number(
            payload,
            ("latitude", "lat"),
        ),
        "longitude": first_number(
            payload,
            ("longitude", "lng"),
        ),
        "store_url": first_text(
            payload,
            ("store_url", "url", "location_url"),
        ),
        "source": first_text(
            payload,
            (
                "source",
                "extraction_source",
                "source_url",
                "source_sitemap",
                "provider",
            ),
        ) or strategy.ROOT_URL,
        "source_type": first_text(
            payload,
            ("source_type",),
        ) or "html",
    }


def write_csv(
    path: Path,
    rows: Sequence[Mapping[str, Any]],
) -> None:
    """
    Write normalized store records to a CSV file.

    :param path: Destination CSV path.
    :param rows: Normalized store records to write.
    """
    with path.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=CSV_FIELDNAMES,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    """Run the Piggly Wiggly acquisition and write output artifacts."""
    strategy = PigglyWigglyAcquisitionStrategy(
        state_workers=16,
        parse_workers=32,
        request_timeout=30,
        max_retries=4,
        retry_backoff_base=1.0,
        retry_backoff_max=16.0,
    )

    started = datetime.now(timezone.utc)
    run_id = started.strftime("%Y%m%d_%H%M%S")

    print()
    print("=" * 72)
    print("Piggly Wiggly Acquisition v3")
    print("=" * 72)
    print("Strategy:", strategy.__class__.__name__)
    print(
        "Source: https://www.pigglywiggly.com/store-locations/"
    )
    print("Method: requests + BeautifulSoup")
    print("CSV export: canonical common store-location schema")
    print()

    source_info = strategy.discover_source()
    raw_artifacts = strategy.fetch_raw_artifacts()
    payloads = strategy.extract_store_payloads(raw_artifacts)
    validation = strategy.validate_store_payloads(payloads)
    rows = [
        normalize_payload(payload, strategy)
        for payload in payloads
    ]

    # Store each acquisition run under a timestamped retailer directory.
    output_dir = OUTPUT_ROOT / run_id
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    csv_path = (
        output_dir
        / f"{run_id}_piggly_wiggly_us_locations.csv"
    )
    summary_path = (
        output_dir
        / f"{run_id}_piggly_wiggly_summary.json"
    )

    write_csv(csv_path, rows)

    summary = {
        "retailer": source_info.retailer_name,
        "retailer_key": source_info.retailer_key,
        "source_type": source_info.source_type,
        "validation": asdict(validation),
        "store_payload_count": len(payloads),
        "normalized_record_count": len(rows),
        "csv_schema": CSV_FIELDNAMES,
        "output_files": {
            "csv": str(csv_path),
            "summary": str(summary_path),
        },
        "run_notes": list(strategy.build_run_notes()),
        "run_started_at_utc": started.isoformat(),
        "run_finished_at_utc": datetime.now(timezone.utc).isoformat(),
    }

    summary_path.write_text(
        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("Piggly Wiggly Acquisition Completed")
    print("-" * 72)
    print(f"Retailer: {source_info.retailer_name}")
    print(f"Retailer key: {source_info.retailer_key}")
    print(f"Valid: {validation.is_valid}")
    print(f"Total records: {validation.total_records}")
    print(f"Unique store IDs: {validation.unique_store_ids}")
    print(f"Missing store IDs: {validation.missing_store_ids}")
    print(
        f"Missing coordinates: "
        f"{validation.missing_coordinates}"
    )
    print(f"CSV: {csv_path}")
    print(f"Summary: {summary_path}")
    print("=" * 72)


if __name__ == "__main__":
    main()