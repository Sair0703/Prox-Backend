from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable

from services.store_service.capabilities.store_location_ingestion.protocols import (
    RawStoreLocationRecord,
    StagingStoreLocation,
)


class LegacyStoreLocationIngestionStrategy:
    """Adapt the existing legacy retailer source files to staging format."""

    def __init__(
        self,
        *,
        retailer_key: str,
        input_path: Path,
        retailer_name: str | None = None,
    ) -> None:
        """
        Initialize the legacy input strategy.

        :param retailer_key: Canonical retailer key used by the ingestion workflow.
        :param input_path: Legacy CSV or JSON source file.
        :param retailer_name: Optional retailer display name.
        """
        self.retailer_key = retailer_key
        self.input_path = input_path
        self.retailer_name = retailer_name or retailer_key

    def read_raw_records(self) -> Iterable[RawStoreLocationRecord]:
        """Read raw records without changing the retailer-specific schema."""
        if self.input_path.suffix.lower() == ".json":
            payload = json.loads(
                self.input_path.read_text(encoding="utf-8-sig")
            )
            if not isinstance(payload, list):
                raise ValueError(
                    f"Expected a JSON list in {self.input_path}."
                )
            return payload

        with self.input_path.open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as file:
            return list(csv.DictReader(file))

    def to_staging_record(
        self,
        record: RawStoreLocationRecord,
    ) -> StagingStoreLocation:
        """Map the legacy source fields into the common staging schema."""
        street_address = self._first_value(
            record,
            "street_address",
            "address",
            "address_line1",
            "address1",
        )
        city = self._first_value(
            record,
            "address_city",
            "city",
            "city_name",
        )
        state = self._first_value(
            record,
            "address_state",
            "state",
            "state_code",
        )
        zip_code = self._first_value(
            record,
            "zip_code",
            "zip",
            "postal_code",
            "postcode",
        )

        full_address = self._first_value(
            record,
            "full_address",
            "formatted_address",
        ) or self._build_full_address(
            street_address=street_address,
            city=city,
            state=state,
            zip_code=zip_code,
        )

        return StagingStoreLocation(
            retailer=self._first_value(
                record,
                "retailer",
                "retailer_name",
                "brand",
            ) or self.retailer_name,
            store_type=self._first_value(
                record,
                "store_type",
                "type",
                "location_type",
            ),
            store_number=self._first_value(
                record,
                "store_number",
                "store_id",
                "location_id",
                "retailer_store_id",
                "warehouse_id",
                "id",
            ),
            city_slug=self._first_value(
                record,
                "city_slug",
            ),
            state=state,
            store_url=self._first_value(
                record,
                "store_url",
                "location_url",
                "url",
            ),
            source_sitemap=self._first_value(
                record,
                "source_sitemap",
                "sitemap",
            ),
            street_address=street_address,
            address_city=city,
            address_state=state,
            zip_code=zip_code,
            full_address=full_address,
            phone=self._first_value(
                record,
                "phone",
                "phone_number",
                "telephone",
            ),
            extraction_source=self._first_value(
                record,
                "extraction_source",
                "source",
            ),
            scrape_status=self._first_value(
                record,
                "scrape_status",
                "status",
            ),
            http_status=self._parse_http_status(
                self._first_value(
                    record,
                    "http_status",
                    "status_code",
                )
            ),
            error_message=self._first_value(
                record,
                "error_message",
                "error",
            ),
            scraped_at_utc=self._first_value(
                record,
                "scraped_at_utc",
                "scraped_at",
                "created_at",
                "retrieved_at_utc",
            ),
        )

    @staticmethod
    def _first_value(
        record: RawStoreLocationRecord,
        *field_names: str,
    ) -> str | None:
        for field_name in field_names:
            if field_name not in record:
                continue

            value = record.get(field_name)
            if value is None:
                continue

            text = str(value).strip()
            if text:
                return text

        return None

    @staticmethod
    def _parse_http_status(value: str | None) -> int | None:
        if value is None:
            return None
        return int(value)

    @staticmethod
    def _build_full_address(
        *,
        street_address: str | None,
        city: str | None,
        state: str | None,
        zip_code: str | None,
    ) -> str | None:
        parts = [
            value
            for value in (
                street_address,
                city,
                state,
                zip_code,
            )
            if value
        ]
        return ", ".join(parts) if parts else None