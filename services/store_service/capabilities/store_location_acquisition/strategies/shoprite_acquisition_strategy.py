from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

import json
import random
import re
import threading
import time

import requests
from tqdm import tqdm


BASE_URL = "https://www.shoprite.com/sm/pickup/rsid/3000/stores"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)

# ShopRite's official store endpoint is geographic rather than a
# simple statewide directory. These probes intentionally overlap so
# the final dataset is the union of official store objects returned by
# the retailer API.
PROBES: list[tuple[str, float, float, float]] = [
    ("New Jersey", 40.5512, -74.4471, 220.0),
    ("New York City", 40.7128, -74.0060, 220.0),
    ("Long Island", 40.7891, -73.1350, 180.0),
    ("Albany", 42.6526, -73.7562, 220.0),
    ("Rochester", 43.1566, -77.6088, 220.0),
    ("Connecticut", 41.6032, -72.6740, 220.0),
    ("Philadelphia", 39.9526, -75.1652, 220.0),
    ("Allentown", 40.6023, -75.4714, 180.0),
    ("Scranton", 41.4089, -75.6624, 180.0),
    ("Harrisburg", 40.2732, -76.8867, 220.0),
    ("Poconos", 41.1200, -75.3500, 180.0),
    ("Delaware", 39.1573, -75.5244, 180.0),
    ("Maryland", 39.2904, -76.6122, 220.0),
    ("Pittsburgh", 40.4406, -79.9959, 180.0),
    ("Northern Pennsylvania", 41.9000, -76.5000, 220.0),
]

WORKERS = 6
MAX_RETRIES = 4
BACKOFF_BASE = 1.0
BACKOFF_MAX = 8.0
REQUEST_TIMEOUT = 30

RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}


@dataclass(slots=True)
class FetchResult:
    label: str
    latitude: float
    longitude: float
    radius_km: float
    status_code: int | None
    payload: Any | None
    error: str | None = None
    attempts: int = 0


class ShopRiteAcquisitionStrategy:
    """
    Acquire the ShopRite authoritative location dataset from the
    retailer's official geographic stores endpoint.

    Endpoint:
        https://www.shoprite.com/sm/pickup/rsid/3000/stores

    Query parameters:
        latitude
        longitude
        withinKilometers

    The API returns store objects containing retailerStoreId,
    address, phone, openingHours, coordinates, status, etc.

    Wines & Spirits / Liquors records are excluded because the target
    dataset is the ShopRite grocery-store network requested by the
    acquisition queue.
    """

    retailer = "ShopRite"
    retailer_key = "shoprite"
    provider = "ShopRite official stores API"
    source_type = "json_api"

    def __init__(self) -> None:
        """Initialize acquisition configuration and run state.

        :return: Result produced by init  .
        """
        self._local = threading.local()
        self._lock = threading.Lock()
        self.http_status_counts: dict[str, int] = {}
        self.request_error_counts: dict[str, int] = {}
        self.failed_probes: list[str] = []
        self.probe_record_counts: dict[str, int] = {}
        self.excluded_non_grocery_records = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self) -> dict[str, Any]:
        """Run the complete store location acquisition workflow.

        :return: Acquired records, validation results, and run metadata.
        """
        print("=" * 72)
        print("ShopRite Acquisition v1")
        print("=" * 72)
        print(f"Source: {BASE_URL}")
        print("Method: requests + official JSON stores endpoint")
        print("Hierarchy: geographic probes -> official store objects")
        print("Store ID: official retailerStoreId")
        print("Coordinates: official location.latitude/longitude")
        print(f"Workers: {WORKERS}")
        print(
            f"Retry: max={MAX_RETRIES}, "
            f"backoff={BACKOFF_BASE}s-{BACKOFF_MAX}s"
        )
        print("Excluded: ShopRite Wines & Spirits / Liquors")
        print()

        results = self._fetch_all_probes()
        records = self._collect_records(results)
        records = self._deduplicate(records)
        records.sort(key=self._sort_key)

        validation = self._validate(records)

        return {
            "retailer": self.retailer,
            "retailer_key": self.retailer_key,
            "provider": self.provider,
            "source_type": self.source_type,
            "records": records,
            "validation": validation,
            "probe_record_counts": dict(self.probe_record_counts),
            "http_status_counts": dict(self.http_status_counts),
            "request_error_counts": dict(self.request_error_counts),
            "failed_probes": list(self.failed_probes),
            "notes": self._notes(),
        }

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _session(self) -> requests.Session:
        """Handle session.

        :return: Result produced by session.
        """
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "https://www.shoprite.com/",
                    "Origin": "https://www.shoprite.com",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                }
            )
            self._local.session = session
        return session

    def _fetch_all_probes(self) -> list[FetchResult]:
        """Fetch all probes.

        :return: Result produced by fetch all probes.
        """
        results: list[FetchResult] = []

        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = {
                executor.submit(
                    self._fetch_probe,
                    label,
                    latitude,
                    longitude,
                    radius_km,
                ): label
                for label, latitude, longitude, radius_km in PROBES
            }

            for future in tqdm(
                as_completed(futures),
                total=len(futures),
                desc="ShopRite geographic probes",
                unit="probe",
            ):
                label = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = FetchResult(
                        label=label,
                        latitude=0.0,
                        longitude=0.0,
                        radius_km=0.0,
                        status_code=None,
                        payload=None,
                        error=repr(exc),
                    )
                results.append(result)

        return results

    def _fetch_probe(
        self,
        label: str,
        latitude: float,
        longitude: float,
        radius_km: float,
    ) -> FetchResult:
        """Fetch probe.

        :param label: Human-readable label for the request or progress output.
        :param latitude: Latitude of the geographic probe.
        :param longitude: Longitude of the geographic probe.
        :param radius_km: Search radius in kilometers.
        :return: Result produced by fetch probe.
        """
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "withinKilometers": radius_km,
        }

        last_status: int | None = None
        last_error: str | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            if attempt > 1:
                delay = min(
                    BACKOFF_MAX,
                    BACKOFF_BASE * (2 ** (attempt - 2)),
                )
                time.sleep(delay + random.uniform(0.0, 0.3))

            try:
                response = self._session().get(
                    BASE_URL,
                    params=params,
                    timeout=REQUEST_TIMEOUT,
                )
                last_status = response.status_code
                with self._lock:
                    key = str(response.status_code)
                    self.http_status_counts[key] = (
                        self.http_status_counts.get(key, 0) + 1
                    )

                if response.status_code == 200:
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        last_error = f"Invalid JSON: {exc!r}"
                        continue

                    return FetchResult(
                        label=label,
                        latitude=latitude,
                        longitude=longitude,
                        radius_km=radius_km,
                        status_code=200,
                        payload=payload,
                        attempts=attempt,
                    )

                if response.status_code in RETRYABLE_STATUS_CODES:
                    last_error = f"HTTP {response.status_code}"
                    continue

                return FetchResult(
                    label=label,
                    latitude=latitude,
                    longitude=longitude,
                    radius_km=radius_km,
                    status_code=response.status_code,
                    payload=None,
                    error=f"HTTP {response.status_code}",
                    attempts=attempt,
                )

            except requests.RequestException as exc:
                last_error = repr(exc)
                error_name = type(exc).__name__
                with self._lock:
                    self.request_error_counts[error_name] = (
                        self.request_error_counts.get(error_name, 0) + 1
                    )

        self.failed_probes.append(label)

        return FetchResult(
            label=label,
            latitude=latitude,
            longitude=longitude,
            radius_km=radius_km,
            status_code=last_status,
            payload=None,
            error=last_error,
            attempts=MAX_RETRIES,
        )

    # ------------------------------------------------------------------
    # Parsing / normalization
    # ------------------------------------------------------------------

    def _collect_records(
        self,
        results: list[FetchResult],
    ) -> list[dict[str, Any]]:
        """Collect records.

        :param results: Completed acquisition results to process.
        :return: Result produced by collect records.
        """
        records: list[dict[str, Any]] = []

        for result in results:
            stores = self._extract_store_list(result.payload)
            self.probe_record_counts[result.label] = len(stores)

            for store in stores:
                if self._is_non_grocery(record=store):
                    self.excluded_non_grocery_records += 1
                    continue

                normalized = self._normalize_store(store)
                if normalized is not None:
                    records.append(normalized)

        return records

    @staticmethod
    def _extract_store_list(payload: Any) -> list[dict[str, Any]]:
        """Extract store list.

        :param payload: Store payload to process.
        :return: Result produced by extract store list.
        """
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]

        if not isinstance(payload, dict):
            return []

        # Common API shapes.
        for key in ("stores", "items", "results", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                for nested_key in ("stores", "items", "results"):
                    nested = value.get(nested_key)
                    if isinstance(nested, list):
                        return [
                            item for item in nested
                            if isinstance(item, dict)
                        ]

        return []

    @staticmethod
    def _is_non_grocery(
        *,
        record: dict[str, Any],
    ) -> bool:
        """Return whether non grocery.

        :param record: Store record to process.
        :return: Result produced by is non grocery.
        """
        name = str(record.get("name") or "").strip().lower()
        return (
            "wines & spirits" in name
            or "wine & spirits" in name
            or "liquors" in name
            or "liquor" in name
        )

    def _normalize_store(
        self,
        store: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Normalize store.

        :param store: Raw retailer store object.
        :return: Result produced by normalize store.
        """
        retailer_store_id = store.get("retailerStoreId")
        if retailer_store_id in (None, ""):
            return None

        location = store.get("location") or {}
        if not isinstance(location, dict):
            location = {}

        address1 = store.get("addressLine1")
        address2 = store.get("addressLine2")
        address3 = store.get("addressLine3")
        city = store.get("city")
        state = store.get("countyProvinceState")
        zip_code = store.get("postCode")
        country = store.get("country")

        address_parts = [
            str(part).strip()
            for part in (address1, address2, address3)
            if part not in (None, "")
        ]

        locality_parts = [
            str(part).strip()
            for part in (city, state, zip_code)
            if part not in (None, "")
        ]

        full_address_parts = address_parts + locality_parts

        return {
            "retailer": self.retailer,
            "retailer_key": self.retailer_key,
            "retailer_store_id": str(retailer_store_id),
            "store_number": str(retailer_store_id),
            "store_name": store.get("name"),
            "status": store.get("status"),
            "store_type": store.get("type"),
            "address": address1,
            "address_line_2": address2,
            "address_line_3": address3,
            "city": city,
            "state": state,
            "zip_code": zip_code,
            "country": country,
            "full_address": ", ".join(full_address_parts) or None,
            "phone": store.get("phone"),
            "email": store.get("email"),
            "latitude": location.get("latitude"),
            "longitude": location.get("longitude"),
            "timezone": store.get("timeZone"),
            "currency": store.get("currency"),
            "opening_hours": store.get("openingHours"),
            "site_id": store.get("siteId"),
            "category_hierarchy_id": store.get("categoryHierarchyId"),
            "shopping_modes": store.get("shoppingModes"),
            "store_url": self._extract_store_url(store),
            "source": self.provider,
            "source_type": self.source_type,
        }

    @staticmethod
    def _extract_store_url(store: dict[str, Any]) -> str | None:
        """Extract store url.

        :param store: Raw retailer store object.
        :return: Result produced by extract store url.
        """
        urls = store.get("urls")
        if not isinstance(urls, list):
            return None
        for value in urls:
            if isinstance(value, str) and value.startswith("http"):
                return value
            if isinstance(value, dict):
                for key in ("url", "href"):
                    candidate = value.get(key)
                    if isinstance(candidate, str) and candidate.startswith("http"):
                        return candidate
        return None

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _deduplicate(
        self,
        records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Handle deduplicate.

        :param records: Store records to process.
        :return: Result produced by deduplicate.
        """
        by_store_id: dict[str, dict[str, Any]] = {}
        for record in records:
            store_id = record["retailer_store_id"]
            existing = by_store_id.get(store_id)
            if existing is None:
                by_store_id[store_id] = record
                continue

            # Prefer a record with coordinates/address/phone if overlapping
            # probes return the same store with partial data.
            if self._quality_score(record) > self._quality_score(existing):
                by_store_id[store_id] = record

        return list(by_store_id.values())

    @staticmethod
    def _quality_score(record: dict[str, Any]) -> int:
        """Handle quality score.

        :param record: Store record to process.
        :return: Result produced by quality score.
        """
        return sum(
            value not in (None, "", [])
            for value in (
                record.get("address"),
                record.get("city"),
                record.get("state"),
                record.get("zip_code"),
                record.get("phone"),
                record.get("latitude"),
                record.get("longitude"),
                record.get("opening_hours"),
            )
        )

    def _validate(
        self,
        records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Handle validate.

        :param records: Store records to process.
        :return: Result produced by validate.
        """
        missing_ids = sum(
            not record.get("retailer_store_id")
            for record in records
        )
        missing_addresses = sum(
            not record.get("full_address")
            for record in records
        )
        missing_coordinates = sum(
            record.get("latitude") is None
            or record.get("longitude") is None
            for record in records
        )
        missing_phones = sum(
            not record.get("phone")
            for record in records
        )
        non_us_records = sum(
            str(record.get("country") or "").lower()
            not in {
                "united states",
                "united states of america",
                "us",
                "usa",
            }
            for record in records
        )

        duplicate_store_ids = len(records) - len({
            record.get("retailer_store_id")
            for record in records
            if record.get("retailer_store_id")
        })

        issues: list[str] = []

        if missing_ids:
            issues.append("missing_store_ids")
        if missing_addresses:
            issues.append("missing_addresses")
        if missing_coordinates:
            issues.append("missing_coordinates")
        if missing_phones:
            issues.append("missing_phones")
        if non_us_records:
            issues.append("non_us_records")
        if duplicate_store_ids:
            issues.append("duplicate_store_ids")
        if self.failed_probes:
            issues.append("failed_probes")

        # Alston's requested target is approximately 315 ShopRite grocery
        # locations. We use a broad sanity range rather than a hard equality
        # because the official dataset can change over time.
        count_sanity = 250 <= len(records) <= 380
        if not count_sanity:
            issues.append("unexpected_record_count")

        valid = (
            not missing_ids
            and not missing_addresses
            and not missing_coordinates
            and not non_us_records
            and not duplicate_store_ids
            and not self.failed_probes
            and count_sanity
        )

        return {
            "valid": valid,
            "total_records": len(records),
            "unique_store_ids": len({
                record["retailer_store_id"]
                for record in records
                if record.get("retailer_store_id")
            }),
            "missing_store_ids": missing_ids,
            "missing_addresses": missing_addresses,
            "missing_coordinates": missing_coordinates,
            "missing_phones": missing_phones,
            "non_us_records": non_us_records,
            "duplicate_store_ids": duplicate_store_ids,
            "failed_probes": len(self.failed_probes),
            "excluded_non_grocery_records": self.excluded_non_grocery_records,
            "issues": issues,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _sort_key(record: dict[str, Any]) -> tuple[int, str]:
        """Handle sort key.

        :param record: Store record to process.
        :return: Result produced by sort key.
        """
        raw_id = str(record.get("retailer_store_id") or "")
        try:
            return (int(raw_id), raw_id)
        except ValueError:
            return (10**9, raw_id)

    @staticmethod
    def _notes() -> list[str]:
        """Handle notes.

        :return: Result produced by notes.
        """
        return [
            "Official source: ShopRite geographic stores API.",
            "retailerStoreId is used as retailer_store_id.",
            "latitude/longitude are taken directly from the official location object.",
            "The acquisition uses overlapping geographic probes and unions results by retailerStoreId.",
            "ShopRite Wines & Spirits / Liquors records are excluded from the grocery-store dataset based on the official store name.",
            "External store detail traversal is not required because the API response already contains the authoritative location fields.",
            "The target network is approximately 315 ShopRite grocery stores; validation therefore uses a broad count sanity range rather than hardcoding an exact count.",
        ]