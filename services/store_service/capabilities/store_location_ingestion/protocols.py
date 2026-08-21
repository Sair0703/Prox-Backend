from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol


RawStoreLocationRecord = Mapping[str, Any]


@dataclass(slots=True)
class StagingStoreLocation:
    """Common store-location record consumed by the staging writer."""

    retailer: str | None = None
    store_type: str | None = None
    store_number: str | None = None
    city_slug: str | None = None
    state: str | None = None
    store_url: str | None = None
    source_sitemap: str | None = None
    street_address: str | None = None
    address_city: str | None = None
    address_state: str | None = None
    zip_code: str | None = None
    full_address: str | None = None
    phone: str | None = None
    extraction_source: str | None = None
    scrape_status: str | None = None
    http_status: int | None = None
    error_message: str | None = None
    scraped_at_utc: str | None = None

    def to_staging_payload(self) -> dict[str, Any]:
        """Build the payload expected by staging_store_locations."""
        return {
            "retailer": self.retailer,
            "store_type": self.store_type,
            "store_number": self.store_number,
            "city_slug": self.city_slug,
            "state": self.state,
            "store_url": self.store_url,
            "source_sitemap": self.source_sitemap,
            "street_address": self.street_address,
            "address_city": self.address_city,
            "address_state": self.address_state,
            "zip_code": self.zip_code,
            "full_address": self.full_address,
            "phone": self.phone,
            "extraction_source": self.extraction_source,
            "scrape_status": self.scrape_status,
            "http_status": self.http_status,
            "error_message": self.error_message,
            "scraped_at_utc": self.scraped_at_utc,
        }


class StoreLocationIngestionStrategy(Protocol):
    """Adapt one retailer-specific raw source to the staging contract."""

    retailer_key: str
    input_path: Path

    def read_raw_records(self) -> Iterable[RawStoreLocationRecord]:
        """Read raw records from the configured retailer source."""
        ...

    def to_staging_record(
        self,
        record: RawStoreLocationRecord,
    ) -> StagingStoreLocation:
        """Convert one raw retailer record into the common staging record."""
        ...