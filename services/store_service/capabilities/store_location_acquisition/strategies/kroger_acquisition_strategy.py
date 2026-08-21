from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import threading
import time
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

from services.store_service.capabilities.store_location_acquisition.protocals import (
    AcquisitionArtifact,
    AcquisitionSourceInfo,
    AcquisitionValidationResult,
    StoreLocationAcquisitionStrategy,
)

BASE_URL = "https://www.kroger.com"
ROOT_URL = f"{BASE_URL}/stores/grocery"

DIRECTORY_URL = (
    f"{BASE_URL}/seo-store-files/link-hub/"
    "store-details-categories/grocery-stores.json"
)

CITY_JSON_BASE_URL = (
    f"{BASE_URL}/seo-store-files/link-hub/store-details-cities"
)

LOCATOR_URL = (
    f"{BASE_URL}/atlas/v1/stores/v2/locator"
)

US_STATE_CODES = {
    "AL", "AR", "GA", "IL", "IN", "KY", "LA", "MI", "MO", "MS",
    "OH", "SC", "TN", "TX", "VA", "WV",
}


@dataclass(frozen=True, slots=True)
class _CityEntry:
    state_code: str
    city_name: str
    city_slug: str
    city_url: str
    city_json_url: str


@dataclass(frozen=True, slots=True)
class _StoreReference:
    location_id: str
    state_code: str
    city_name: str
    city_json_url: str


class KrogerAcquisitionStrategyV5(
    StoreLocationAcquisitionStrategy
):
    retailer_key = "kroger"
    retailer_name = "Kroger"

    def __init__(
        self,
        *,
        city_workers: int = 48,
        locator_workers: int = 16,
        parse_workers: int = 32,
        request_timeout: int = 30,
        max_retries: int = 5,
        retry_backoff_base: float = 1.0,
        retry_backoff_max: float = 16.0,
        debug_failed_limit: int = 25,
    ) -> None:
        """Initialize acquisition configuration and run state.

        :param city_workers: City workers.
        :param locator_workers: Locator workers.
        :param parse_workers: Parse workers.
        :param request_timeout: Request timeout.
        :param max_retries: Max retries.
        :param retry_backoff_base: Retry backoff base.
        :param retry_backoff_max: Retry backoff max.
        :param debug_failed_limit: Debug failed limit.
        :return: Result produced by init  .
        """
        self.city_workers = city_workers
        self.locator_workers = locator_workers
        self.parse_workers = parse_workers
        self.request_timeout = request_timeout
        self.max_retries = max_retries
        self.retry_backoff_base = retry_backoff_base
        self.retry_backoff_max = retry_backoff_max
        self.debug_failed_limit = debug_failed_limit

        self._thread_local = threading.local()

        self._declared_state_count = 0
        self._declared_city_count = 0
        self._declared_store_count = 0

        self._discovered_city_count = 0
        self._discovered_location_id_count = 0
        self._successful_locator_count = 0

        self._failed_city_pages: list[dict[str, Any]] = []
        self._failed_locator_requests: list[dict[str, Any]] = []

        self._locator_failure_status_counts: dict[str, int] = {}
        self._locator_failure_type_counts: dict[str, int] = {}
        self._browser_fallback_count = 0
        self._browser_fallback_failures: list[dict[str, Any]] = []

    def discover_source(self) -> AcquisitionSourceInfo:
        """Return metadata describing the retailer's official acquisition source.

        :return: Metadata describing the acquisition source.
        """
        return AcquisitionSourceInfo(
            retailer_key=self.retailer_key,
            retailer_name=self.retailer_name,
            official_website_url="https://www.kroger.com/",
            store_locator_url=ROOT_URL,
            endpoint_url=LOCATOR_URL,
            source_type="api",
            provider="Kroger official grocery store directory / store locator API",
            notes=(
                "Official API chain: grocery-stores.json -> city JSON -> "
                "atlas locator API. v4 adds bounded locator concurrency, "
                "retry/backoff, and failure diagnostics."
            ),
        )

    def fetch_raw_artifacts(
        self,
    ) -> list[AcquisitionArtifact]:
        """Fetch raw artifacts required for store location acquisition.

        :return: Raw acquisition artifacts.
        """
        self._reset_run_state()

        print(
            "[Kroger][directory] fetching:",
            DIRECTORY_URL,
        )

        try:
            directory_payload = self._fetch_json(
                DIRECTORY_URL,
                request_name="directory",
            )
        except RuntimeError as exc:
            if "403" not in str(exc):
                raise
            print(
                "[Kroger][browser-fallback] directory JSON request "
                "returned 403; using browser request context."
            )
            directory_payload = self._fetch_json_with_browser(
                DIRECTORY_URL,
                request_name="directory",
            )

        meta = directory_payload.get("meta")
        data = directory_payload.get("data")

        if not isinstance(meta, dict):
            raise RuntimeError(
                "Kroger grocery-stores.json is missing a valid 'meta' object."
            )

        if not isinstance(data, dict):
            raise RuntimeError(
                "Kroger grocery-stores.json is missing a valid 'data' object."
            )

        self._declared_state_count = (
            self._as_int(meta.get("stateCount")) or 0
        )
        self._declared_city_count = (
            self._as_int(meta.get("citiesCount")) or 0
        )
        self._declared_store_count = (
            self._as_int(meta.get("storesCount")) or 0
        )

        cities = self._parse_city_entries(data)
        self._discovered_city_count = len(cities)

        print(
            "[Kroger][directory] states:",
            self._declared_state_count,
        )
        print(
            "[Kroger][directory] declared cities:",
            self._declared_city_count,
        )
        print(
            "[Kroger][directory] declared stores:",
            self._declared_store_count,
        )
        print(
            "[Kroger][directory] parsed cities:",
            len(cities),
        )

        if not cities:
            raise RuntimeError(
                "Kroger grocery-stores.json returned no city links."
            )

        artifacts: list[AcquisitionArtifact] = [
            AcquisitionArtifact(
                artifact_type="json",
                source_url=DIRECTORY_URL,
                content=json.dumps(directory_payload),
                metadata={
                    "retrieved_at_utc": self._utc_now(),
                    "page_type": "directory",
                    "http_status": 200,
                    "scrape_status": "success",
                    "state_count": self._declared_state_count,
                    "city_count": self._declared_city_count,
                    "store_count": self._declared_store_count,
                },
            )
        ]

        store_references = self._acquire_store_references(
            cities=cities,
            artifacts=artifacts,
        )

        self._discovered_location_id_count = len(
            store_references
        )

        print(
            "[Kroger][city-json] unique location IDs:",
            self._discovered_location_id_count,
        )

        if not store_references:
            raise RuntimeError(
                "Kroger city JSON acquisition returned no location IDs."
            )

        self._acquire_locator_artifacts(
            store_references=store_references,
            artifacts=artifacts,
        )

        return artifacts

    def extract_store_payloads(
        self,
        artifacts: Sequence[AcquisitionArtifact],
    ) -> list[dict[str, Any]]:
        """Extract normalized store payloads from acquired artifacts.

        :param artifacts: Acquisition artifacts to process.
        :return: Normalized and deduplicated store payloads.
        """
        locator_artifacts = [
            artifact
            for artifact in artifacts
            if artifact.metadata.get("page_type")
            == "store_locator"
            and artifact.metadata.get("scrape_status")
            == "success"
            and artifact.content
        ]

        rows_by_store_id: dict[
            str,
            dict[str, Any],
        ] = {}

        with ThreadPoolExecutor(
            max_workers=self.parse_workers
        ) as pool:
            futures = {
                pool.submit(
                    self._parse_locator_artifact,
                    artifact,
                ): artifact
                for artifact in locator_artifacts
            }

            for future in tqdm(
                as_completed(futures),
                total=len(futures),
                desc="Parsing Kroger stores",
                unit="store",
            ):
                rows = future.result()

                for row in rows:
                    store_id = self._clean_text(
                        row.get("retailer_store_id")
                    )
                    if store_id:
                        rows_by_store_id[
                            store_id
                        ] = row

        return list(
            rows_by_store_id.values()
        )

    def validate_store_payloads(
        self,
        payloads: Sequence[Mapping[str, Any]],
    ) -> AcquisitionValidationResult:
        """Validate acquired store payloads for completeness and uniqueness.

        :param payloads: Normalized store payloads to validate.
        :return: Validation result for the acquired payloads.
        """
        total_records = len(payloads)

        store_ids = [
            self._clean_text(
                row.get("retailer_store_id")
            )
            for row in payloads
        ]

        unique_store_ids = len(
            {
                value
                for value in store_ids
                if value
            }
        )

        missing_store_ids = sum(
            1
            for value in store_ids
            if not value
        )

        duplicate_store_ids: list[str] = []
        seen: set[str] = set()

        for store_id in store_ids:
            if not store_id:
                continue

            if (
                store_id in seen
                and store_id
                not in duplicate_store_ids
            ):
                duplicate_store_ids.append(
                    store_id
                )

            seen.add(
                store_id
            )

        missing_addresses = sum(
            1
            for row in payloads
            if not self._clean_text(
                row.get("street_address")
            )
            or not self._clean_text(
                row.get("city")
            )
            or not self._clean_text(
                row.get("state")
            )
            or not self._clean_text(
                row.get("zip_code")
            )
        )

        missing_phones = sum(
            1
            for row in payloads
            if not self._clean_text(
                row.get("phone")
            )
        )

        missing_coordinates = sum(
            1
            for row in payloads
            if row.get("latitude") is None
            or row.get("longitude") is None
        )

        issue_counts: dict[str, int] = {}

        if missing_store_ids:
            issue_counts[
                "missing_store_ids"
            ] = missing_store_ids

        if duplicate_store_ids:
            issue_counts[
                "duplicate_store_ids"
            ] = len(
                duplicate_store_ids
            )

        if missing_addresses:
            issue_counts[
                "missing_addresses"
            ] = missing_addresses

        if missing_phones:
            issue_counts[
                "missing_phones"
            ] = missing_phones

        if missing_coordinates:
            issue_counts[
                "missing_coordinates"
            ] = missing_coordinates

        if self._failed_city_pages:
            issue_counts[
                "failed_city_json"
            ] = len(
                self._failed_city_pages
            )

        if self._failed_locator_requests:
            issue_counts[
                "failed_locator_requests"
            ] = len(
                self._failed_locator_requests
            )

        if (
            self._declared_city_count
            and self._discovered_city_count
            != self._declared_city_count
        ):
            issue_counts[
                "declared_city_count_mismatch"
            ] = 1

        if (
            self._declared_store_count
            and self._discovered_location_id_count
            != self._declared_store_count
        ):
            issue_counts[
                "declared_location_id_count_mismatch"
            ] = 1

        if (
            self._declared_store_count
            and total_records
            != self._declared_store_count
        ):
            issue_counts[
                "declared_store_count_mismatch"
            ] = 1

        notes = [
            (
                "Official source chain: grocery-stores.json -> "
                "city JSON -> atlas store locator API."
            ),
            (
                f"Declared directory totals: "
                f"{self._declared_state_count} states, "
                f"{self._declared_city_count} cities, "
                f"{self._declared_store_count} stores."
            ),
            (
                "City JSON provides locationIds; the locator API with "
                "projections=full provides Store Info."
            ),
            (
                "retailer_store_id is the locator API locationId, "
                "e.g. 01100260."
            ),
            (
                "store_number is taken directly from the locator API "
                "storeNumber field, e.g. 00260."
            ),
            (
                "Coordinates are taken directly from locale.location; "
                "no geocoding is required."
            ),
            f"Browser API fallbacks: {self._browser_fallback_count}",
            (
                f"Workers: city={self.city_workers}, "
                f"locator={self.locator_workers}, "
                f"parse={self.parse_workers}"
            ),
            (
                f"Locator retries: max={self.max_retries}, "
                f"backoff={self.retry_backoff_base}s.."
                f"{self.retry_backoff_max}s"
            ),
        ]

        if self._locator_failure_status_counts:
            notes.append(
                "Locator failure statuses: "
                + ", ".join(
                    f"{key}={value}"
                    for key, value in sorted(
                        self._locator_failure_status_counts.items()
                    )
                )
            )

        if self._locator_failure_type_counts:
            notes.append(
                "Locator failure types: "
                + ", ".join(
                    f"{key}={value}"
                    for key, value in sorted(
                        self._locator_failure_type_counts.items()
                    )
                )
            )

        is_valid = (
            total_records > 0
            and missing_store_ids == 0
            and not duplicate_store_ids
            and missing_addresses == 0
            and missing_phones == 0
            and missing_coordinates == 0
            and not self._failed_city_pages
            and not self._failed_locator_requests
            and (
                not self._declared_city_count
                or self._discovered_city_count
                == self._declared_city_count
            )
            and (
                not self._declared_store_count
                or self._discovered_location_id_count
                == self._declared_store_count
            )
            and (
                not self._declared_store_count
                or total_records
                == self._declared_store_count
            )
        )

        return AcquisitionValidationResult(
            is_valid=is_valid,
            total_records=total_records,
            unique_store_ids=unique_store_ids,
            missing_store_ids=missing_store_ids,
            missing_coordinates=missing_coordinates,
            non_us_records=0,
            duplicate_store_ids=duplicate_store_ids,
            issue_counts=issue_counts,
            notes=notes,
        )

    def build_run_notes(
        self,
    ) -> list[str]:
        """Return acquisition source and execution details for the run summary.

        :return: Notes describing the acquisition run.
        """
        return [
            f"Directory source: {DIRECTORY_URL}",
            f"Locator source: {LOCATOR_URL}",
            (
                "Method: official JSON directory + "
                "city JSON + locator API"
            ),
            (
                "Hierarchy: grocery-stores.json -> city JSON -> "
                "locationIds -> full Store Info"
            ),
            "No Playwright required.",
            "No HTML scraping required.",
            (
                "Locator requests use bounded concurrency, "
                "exponential backoff, and failure diagnostics."
            ),
            (
                "Coordinates are obtained directly from "
                "Kroger's locator API."
            ),
            (
                f"Declared cities: {self._declared_city_count}; "
                f"declared stores: {self._declared_store_count}"
            ),
            (
                f"Workers: city={self.city_workers}, "
                f"locator={self.locator_workers}, "
                f"parse={self.parse_workers}"
            ),
        ]

    def _acquire_store_references(
        self,
        *,
        cities: Sequence[_CityEntry],
        artifacts: list[AcquisitionArtifact],
    ) -> dict[str, _StoreReference]:
        """Handle acquire store references.

        :param cities: Discovered city entries to process.
        :param artifacts: Acquisition artifacts to process.
        :return: Result produced by acquire store references.
        """
        store_references: dict[str, _StoreReference] = {}
        failed_cities: list[_CityEntry] = []

        with ThreadPoolExecutor(
            max_workers=self.city_workers
        ) as pool:
            futures = {
                pool.submit(
                    self._fetch_city_json_artifact,
                    city,
                ): city
                for city in cities
            }

            for future in tqdm(
                as_completed(futures),
                total=len(futures),
                desc="Kroger city JSON",
                unit="city",
            ):
                city = futures[future]

                try:
                    artifact, location_ids = future.result()
                except Exception as exc:
                    if "403" in str(exc):
                        failed_cities.append(city)
                    else:
                        self._failed_city_pages.append(
                            {
                                "state_code": city.state_code,
                                "city_name": city.city_name,
                                "city_url": city.city_url,
                                "city_json_url": city.city_json_url,
                                "error": str(exc),
                            }
                        )
                    continue

                artifacts.append(artifact)

                for location_id in location_ids:
                    store_references[location_id] = _StoreReference(
                        location_id=location_id,
                        state_code=city.state_code,
                        city_name=city.city_name,
                        city_json_url=city.city_json_url,
                    )

        if failed_cities:
            print(
                "[Kroger][browser-fallback] retrying failed city JSON:",
                len(failed_cities),
            )

            for city in tqdm(
                failed_cities,
                desc="Kroger city JSON browser fallback",
                unit="city",
            ):
                try:
                    artifact, location_ids = (
                        self._fetch_city_json_artifact_with_browser(city)
                    )
                except Exception as exc:
                    self._failed_city_pages.append(
                        {
                            "state_code": city.state_code,
                            "city_name": city.city_name,
                            "city_url": city.city_url,
                            "city_json_url": city.city_json_url,
                            "error": str(exc),
                        }
                    )
                    continue

                artifacts.append(artifact)

                for location_id in location_ids:
                    store_references[location_id] = _StoreReference(
                        location_id=location_id,
                        state_code=city.state_code,
                        city_name=city.city_name,
                        city_json_url=city.city_json_url,
                    )

        return store_references

    def _acquire_locator_artifacts(
        self,
        *,
        store_references: Mapping[str, _StoreReference],
        artifacts: list[AcquisitionArtifact],
    ) -> None:
        """Handle acquire locator artifacts.

        :param store_references: Store references keyed by retailer location ID.
        :param artifacts: Acquisition artifacts to process.
        :return: Result produced by acquire locator artifacts.
        """
        print(
            "[Kroger][locator] starting:",
            len(store_references),
            "store requests",
        )
        print(
            "[Kroger][locator] workers:",
            self.locator_workers,
        )
        print(
            "[Kroger][locator] max retries:",
            self.max_retries,
        )

        failed_references: list[_StoreReference] = []

        with ThreadPoolExecutor(
            max_workers=self.locator_workers
        ) as pool:
            futures = {
                pool.submit(
                    self._fetch_locator_artifact,
                    reference,
                ): reference
                for reference in store_references.values()
            }

            for future in tqdm(
                as_completed(futures),
                total=len(futures),
                desc="Kroger store locator",
                unit="store",
            ):
                reference = futures[future]

                try:
                    artifact = future.result()
                except Exception as exc:
                    if "403" in str(exc):
                        failed_references.append(reference)
                    else:
                        self._record_locator_failure(
                            reference,
                            exc,
                        )
                    continue

                self._successful_locator_count += 1
                artifacts.append(artifact)

        if failed_references:
            print(
                "[Kroger][browser-fallback] retrying failed locator requests:",
                len(failed_references),
            )

            for reference in tqdm(
                failed_references,
                desc="Kroger locator browser fallback",
                unit="store",
            ):
                try:
                    artifact = self._fetch_locator_artifact_with_browser(
                        reference
                    )
                except Exception as exc:
                    self._record_locator_failure(
                        reference,
                        exc,
                    )
                    continue

                self._successful_locator_count += 1
                artifacts.append(artifact)

        print(
            "[Kroger][locator] successful:",
            self._successful_locator_count,
        )
        print(
            "[Kroger][locator] failed:",
            len(self._failed_locator_requests),
        )

        if self._locator_failure_status_counts:
            print(
                "[Kroger][locator] failure statuses:",
                self._locator_failure_status_counts,
            )

        if self._locator_failure_type_counts:
            print(
                "[Kroger][locator] failure types:",
                self._locator_failure_type_counts,
            )

        if self._failed_locator_requests:
            print(
                "[Kroger][locator] sample failures:"
            )
            for failure in self._failed_locator_requests[
                : self.debug_failed_limit
            ]:
                print(
                    "[Kroger][locator][failure]",
                    failure,
                )

    def _fetch_city_json_artifact(
        self,
        city: _CityEntry,
    ) -> tuple[
        AcquisitionArtifact,
        list[str],
    ]:
        """Fetch city json artifact.

        :param city: City entry to process.
        :return: Result produced by fetch city json artifact.
        """
        payload = self._fetch_json(
            city.city_json_url,
            request_name="city_json",
        )

        location_ids_raw = payload.get(
            "locationIds"
        )

        if not isinstance(
            location_ids_raw,
            list,
        ):
            raise RuntimeError(
                f"Kroger city JSON has no valid locationIds: "
                f"{city.city_json_url}"
            )

        location_ids = [
            str(value).strip()
            for value in location_ids_raw
            if str(value).strip()
        ]

        if not location_ids:
            raise RuntimeError(
                f"Kroger city JSON returned no locationIds: "
                f"{city.city_json_url}"
            )

        artifact = AcquisitionArtifact(
            artifact_type="json",
            source_url=city.city_json_url,
            content=json.dumps(
                payload
            ),
            metadata={
                "retrieved_at_utc": self._utc_now(),
                "page_type": "city_json",
                "state_code": city.state_code,
                "city_name": city.city_name,
                "city_url": city.city_url,
                "http_status": 200,
                "scrape_status": "success",
                "location_count": len(
                    location_ids
                ),
            },
        )

        return artifact, location_ids

    def _fetch_locator_artifact(
        self,
        reference: _StoreReference,
    ) -> AcquisitionArtifact:
        """Fetch locator artifact.

        :param reference: Store reference to process.
        :return: Result produced by fetch locator artifact.
        """
        url = self._build_locator_url(
            reference.location_id
        )

        payload = self._fetch_json(
            url,
            request_name=(
                f"locator:{reference.location_id}"
            ),
            record_http_diagnostics=True,
        )

        stores = payload.get(
            "data",
            {}
        )

        if isinstance(
            stores,
            dict,
        ):
            stores = stores.get(
                "stores"
            )
        else:
            stores = None

        if not isinstance(
            stores,
            list,
        ) or not stores:
            raise RuntimeError(
                f"Kroger locator returned no stores for "
                f"{reference.location_id}: {url}"
            )

        return AcquisitionArtifact(
            artifact_type="json",
            source_url=url,
            content=json.dumps(
                payload
            ),
            metadata={
                "retrieved_at_utc": self._utc_now(),
                "page_type": "store_locator",
                "location_id": reference.location_id,
                "state_code": reference.state_code,
                "city_name": reference.city_name,
                "http_status": 200,
                "scrape_status": "success",
                "store_count": len(
                    stores
                ),
            },
        )

    def _parse_locator_artifact(
        self,
        artifact: AcquisitionArtifact,
    ) -> list[dict[str, Any]]:
        """Parse locator artifact.

        :param artifact: Acquisition artifact to parse.
        :return: Result produced by parse locator artifact.
        """
        payload = json.loads(
            artifact.content
        )

        data = payload.get(
            "data"
        )

        if not isinstance(
            data,
            dict,
        ):
            return []

        stores = data.get(
            "stores"
        )

        if not isinstance(
            stores,
            list,
        ):
            return []

        rows: list[dict[str, Any]] = []

        for store in stores:
            if not isinstance(
                store,
                dict,
            ):
                continue

            locale = store.get(
                "locale"
            )
            locale = (
                locale
                if isinstance(
                    locale,
                    dict,
                )
                else {}
            )

            address = locale.get(
                "address"
            )
            address = (
                address
                if isinstance(
                    address,
                    dict,
                )
                else {}
            )

            location = locale.get(
                "location"
            )
            location = (
                location
                if isinstance(
                    location,
                    dict,
                )
                else {}
            )

            phone_data = store.get(
                "phoneNumber"
            )
            phone = (
                phone_data.get("pretty")
                if isinstance(
                    phone_data,
                    dict,
                )
                else None
            )

            location_id = self._clean_text(
                store.get(
                    "locationId"
                )
            )

            store_number = self._clean_text(
                store.get(
                    "storeNumber"
                )
            )

            if not location_id:
                continue

            address_lines = address.get(
                "addressLines"
            )
            address_lines = (
                address_lines
                if isinstance(
                    address_lines,
                    list,
                )
                else []
            )

            street_address = ", ".join(
                str(line).strip()
                for line in address_lines
                if str(line).strip()
            ) or None

            city = self._clean_text(
                address.get(
                    "cityTown"
                )
            )

            state = self._clean_text(
                address.get(
                    "stateProvince"
                )
            )

            zip_code = self._clean_text(
                address.get(
                    "postalCode"
                )
            )

            store_name = (
                self._clean_text(
                    address.get(
                        "name"
                    )
                )
                or self._clean_text(
                    store.get(
                        "vanityName"
                    )
                )
                or self._clean_text(
                    store.get(
                        "facilityName"
                    )
                )
            )

            latitude = self._as_float(
                location.get("lat")
            )

            longitude = self._as_float(
                location.get("lng")
            )

            full_address = (
                self._build_full_address(
                    street_address=street_address,
                    city=city,
                    state=state,
                    zip_code=zip_code,
                )
            )

            rows.append(
                {
                    "retailer": self.retailer_name,
                    "retailer_store_id": location_id,
                    "store_number": store_number,
                    "store_type": self._clean_text(
                        store.get("storeType")
                    ),
                    "store_name": store_name,
                    "address": street_address,
                    "street_address": street_address,
                    "city": city,
                    "state": state,
                    "address_city": city,
                    "address_state": state,
                    "zip_code": zip_code,
                    "full_address": full_address,
                    "phone": phone,
                    "latitude": latitude,
                    "longitude": longitude,
                    "store_url": None,
                    "source_url": artifact.source_url,
                    "source_sitemap": artifact.source_url,
                    "division_number": self._clean_text(
                        store.get(
                            "loyaltyDivisionNumber"
                        )
                    ),
                    "facility_id": self._clean_text(
                        store.get(
                            "facilityId"
                        )
                    ),
                    "facility_name": self._clean_text(
                        store.get(
                            "facilityName"
                        )
                    ),
                    "legal_name": self._clean_text(
                        store.get(
                            "legalName"
                        )
                    ),
                    "banner": self._clean_text(
                        store.get(
                            "banner"
                        )
                    ),
                    "extraction_source": (
                        "Kroger official atlas store locator API"
                    ),
                    "scrape_status": "success",
                    "http_status": artifact.metadata.get(
                        "http_status"
                    ),
                    "error_message": None,
                    "scraped_at_utc": artifact.metadata.get(
                        "retrieved_at_utc"
                    ),
                }
            )

        return rows

    def _build_locator_url(
        self,
        location_id: str,
    ) -> str:
        """Build locator url.

        :param location_id: Retailer location identifier.
        :return: Result produced by build locator url.
        """
        return (
            f"{LOCATOR_URL}"
            f"?filter.locationIds={location_id}"
            f"&projections=full"
        )

    def _fetch_city_json_artifact_with_browser(
        self,
        city: _CityEntry,
    ) -> tuple[AcquisitionArtifact, list[str]]:
        """Fetch city json artifact with browser.

        :param city: City entry to process.
        :return: Result produced by fetch city json artifact with browser.
        """
        payload = self._fetch_json_with_browser(
            city.city_json_url,
            request_name=f"city_json:{city.city_name}",
        )

        location_ids_raw = payload.get("locationIds")
        if not isinstance(location_ids_raw, list):
            raise RuntimeError(
                f"Kroger city JSON has no valid locationIds: "
                f"{city.city_json_url}"
            )

        location_ids = [
            str(value).strip()
            for value in location_ids_raw
            if str(value).strip()
        ]

        if not location_ids:
            raise RuntimeError(
                f"Kroger city JSON returned no locationIds: "
                f"{city.city_json_url}"
            )

        return (
            AcquisitionArtifact(
                artifact_type="json",
                source_url=city.city_json_url,
                content=json.dumps(payload),
                metadata={
                    "retrieved_at_utc": self._utc_now(),
                    "page_type": "city_json",
                    "state_code": city.state_code,
                    "city_name": city.city_name,
                    "city_url": city.city_url,
                    "http_status": 200,
                    "scrape_status": "success",
                    "browser_fallback": True,
                    "location_count": len(location_ids),
                },
            ),
            location_ids,
        )

    def _fetch_locator_artifact_with_browser(
        self,
        reference: _StoreReference,
    ) -> AcquisitionArtifact:
        """Fetch locator artifact with browser.

        :param reference: Store reference to process.
        :return: Result produced by fetch locator artifact with browser.
        """
        url = self._build_locator_url(reference.location_id)

        payload = self._fetch_json_with_browser(
            url,
            request_name=f"locator:{reference.location_id}",
        )

        stores = payload.get("data", {})
        stores = (
            stores.get("stores")
            if isinstance(stores, dict)
            else None
        )

        if not isinstance(stores, list) or not stores:
            raise RuntimeError(
                f"Kroger locator returned no stores for "
                f"{reference.location_id}: {url}"
            )

        return AcquisitionArtifact(
            artifact_type="json",
            source_url=url,
            content=json.dumps(payload),
            metadata={
                "retrieved_at_utc": self._utc_now(),
                "page_type": "store_locator",
                "location_id": reference.location_id,
                "state_code": reference.state_code,
                "city_name": reference.city_name,
                "http_status": 200,
                "scrape_status": "success",
                "browser_fallback": True,
                "store_count": len(stores),
            },
        )

    def _fetch_json_with_browser(
        self,
        url: str,
        *,
        request_name: str,
    ) -> dict[str, Any]:
        """Fetch json with browser.

        :param url: URL to fetch or process.
        :param request_name: Diagnostic name for the request.
        :return: Result produced by fetch json with browser.
        """
        if sync_playwright is None:
            raise RuntimeError(
                "Playwright is required for Kroger browser API fallback "
                "but is not installed."
            )

        self._browser_fallback_count += 1

        print(
            "[Kroger][browser-fallback] request:",
            request_name,
            url,
        )

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-http2",
                    "--disable-quic",
                ],
            )

            try:
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/151.0.0.0 Safari/537.36"
                    ),
                    locale="en-US",
                    extra_http_headers={
                        "accept": "application/json,text/plain,*/*",
                        "accept-language": "en-US,en;q=0.9",
                        "referer": ROOT_URL,
                        "origin": BASE_URL,
                    },
                )

                response = context.request.get(
                    url,
                    timeout=self.request_timeout * 1000,
                )

                if not response.ok:
                    body_preview = response.text()[:500]
                    raise RuntimeError(
                        f"Browser API request failed: "
                        f"status={response.status}, "
                        f"url={url}, "
                        f"body={body_preview!r}"
                    )

                payload = response.json()

                if not isinstance(payload, dict):
                    raise RuntimeError(
                        f"Browser API returned non-object JSON: {url}"
                    )

                return payload

            finally:
                browser.close()

    def _record_locator_failure(
        self,
        reference: _StoreReference,
        exc: Exception,
    ) -> None:
        """Record locator failure.

        :param reference: Store reference to process.
        :param exc: Exc.
        :return: Result produced by record locator failure.
        """
        failure = {
            "location_id": reference.location_id,
            "state_code": reference.state_code,
            "city_name": reference.city_name,
            "url": self._build_locator_url(
                reference.location_id
            ),
            "error": str(exc),
        }

        self._failed_locator_requests.append(
            failure
        )

    def _fetch_json(
        self,
        url: str,
        *,
        request_name: str,
        record_http_diagnostics: bool = False,
    ) -> dict[str, Any]:
        """Fetch json.

        :param url: URL to fetch or process.
        :param request_name: Diagnostic name for the request.
        :param record_http_diagnostics: Whether to record HTTP failure diagnostics.
        :return: Result produced by fetch json.
        """
        session = self._get_session()
        last_error: Exception | None = None

        for attempt in range(
            1,
            self.max_retries + 1,
        ):
            try:
                response = session.get(
                    url,
                    timeout=self.request_timeout,
                    headers={
                        "accept": (
                            "application/json,text/plain,*/*"
                        ),
                        "referer": ROOT_URL,
                    },
                )

                response.raise_for_status()

                payload = response.json()

                if not isinstance(
                    payload,
                    dict,
                ):
                    raise RuntimeError(
                        f"Expected JSON object from {url}"
                    )

                return payload

            except Exception as exc:
                last_error = exc

                if record_http_diagnostics:
                    status = "unknown"

                    response_obj = (
                        response
                        if "response" in locals()
                        and response is not None
                        else None
                    )

                    if response_obj is not None:
                        status = str(
                            response_obj.status_code
                        )

                    status_key = str(
                        status
                    )

                    self._locator_failure_status_counts[
                        status_key
                    ] = (
                        self._locator_failure_status_counts.get(
                            status_key,
                            0,
                        )
                        + 1
                    )

                    error_type = type(
                        exc
                    ).__name__

                    self._locator_failure_type_counts[
                        error_type
                    ] = (
                        self._locator_failure_type_counts.get(
                            error_type,
                            0,
                        )
                        + 1
                    )

                    print(
                        "[Kroger][locator][retry]",
                        {
                            "request": request_name,
                            "attempt": attempt,
                            "status": status,
                            "error_type": error_type,
                            "error": str(exc),
                        },
                    )

                if attempt < self.max_retries:
                    delay = min(
                        self.retry_backoff_base
                        * (
                            2 ** (attempt - 1)
                        ),
                        self.retry_backoff_max,
                    )

                    time.sleep(
                        delay
                    )

        raise RuntimeError(
            f"Failed to fetch Kroger JSON "
            f"{url} after {self.max_retries} attempts: "
            f"{last_error}"
        ) from last_error

    def _get_session(
        self,
    ) -> requests.Session:
        """Return session.

        :return: Result produced by get session.
        """
        session = getattr(
            self._thread_local,
            "session",
            None,
        )

        if session is None:
            session = requests.Session()

            session.headers.update(
                {
                    "accept": (
                        "application/json,text/plain,*/*"
                    ),
                    "accept-language": (
                        "en-US,en;q=0.9"
                    ),
                    "cache-control": "no-cache",
                    "pragma": "no-cache",
                    "referer": ROOT_URL,
                    "user-agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/151.0.0.0 Safari/537.36"
                    ),
                }
            )

            self._thread_local.session = session

        return session

    def _failed_artifact(
        self,
        *,
        url: str,
        page_type: str,
        error: Exception,
        state_code: str | None = None,
        location_id: str | None = None,
    ) -> AcquisitionArtifact:
        """Handle failed artifact.

        :param url: URL to fetch or process.
        :param page_type: Acquisition page type.
        :param error: Request or parsing error.
        :param state_code: State code associated with the page.
        :param location_id: Retailer location identifier.
        :return: Result produced by failed artifact.
        """
        return AcquisitionArtifact(
            artifact_type="json",
            source_url=url,
            content="",
            metadata={
                "retrieved_at_utc": self._utc_now(),
                "page_type": page_type,
                "state_code": state_code,
                "location_id": location_id,
                "http_status": 500,
                "scrape_status": "failed",
                "error": str(error),
            },
        )

    @staticmethod
    def _parse_city_entries(
        data: Mapping[str, Any],
    ) -> list[_CityEntry]:
        """Parse city entries.

        :param data: Directory data to parse.
        :return: Result produced by parse city entries.
        """
        entries: list[_CityEntry] = []
        seen: set[str] = set()

        for state_payload in data.values():
            if not isinstance(
                state_payload,
                dict,
            ):
                continue

            links = state_payload.get(
                "links"
            )

            if not isinstance(
                links,
                list,
            ):
                continue

            for item in links:
                if not isinstance(
                    item,
                    dict,
                ):
                    continue

                href = item.get(
                    "link"
                )

                if not isinstance(
                    href,
                    str,
                ):
                    continue

                href = href.strip()

                if not href:
                    continue

                absolute_url = urljoin(
                    BASE_URL,
                    href,
                )

                path = urlparse(
                    absolute_url
                ).path

                match = re.fullmatch(
                    r"/stores/grocery/"
                    r"(?P<state>[a-z]{2})/"
                    r"(?P<city>[^/]+)/?",
                    path,
                    flags=re.IGNORECASE,
                )

                if not match:
                    continue

                state_code = match.group(
                    "state"
                ).upper()

                if state_code not in US_STATE_CODES:
                    continue

                city_slug = match.group(
                    "city"
                )

                city_name = (
                    str(
                        item.get(
                            "text"
                        )
                    ).strip()
                    if item.get(
                        "text"
                    ) is not None
                    else ""
                )

                if not city_name:
                    city_name = (
                        city_slug
                        .replace(
                            "-",
                            " ",
                        )
                        .title()
                    )

                city_json_url = (
                    f"{CITY_JSON_BASE_URL}/"
                    f"{city_slug.lower()}-"
                    f"{state_code.lower()}-grocery.json"
                )

                if city_json_url in seen:
                    continue

                seen.add(
                    city_json_url
                )

                entries.append(
                    _CityEntry(
                        state_code=state_code,
                        city_name=city_name,
                        city_slug=city_slug,
                        city_url=absolute_url,
                        city_json_url=city_json_url,
                    )
                )

        return entries

    @staticmethod
    def _build_full_address(
        *,
        street_address: str | None,
        city: str | None,
        state: str | None,
        zip_code: str | None,
    ) -> str | None:
        """Build full address.

        :param street_address: Street address component.
        :param city: City entry to process.
        :param state: State name or abbreviation.
        :param zip_code: Postal code component.
        :return: Result produced by build full address.
        """
        locality = None

        if city and state:
            locality = f"{city}, {state}"
        elif city:
            locality = city
        elif state:
            locality = state

        if locality and zip_code:
            locality = (
                f"{locality} {zip_code}"
            )
        elif zip_code:
            locality = zip_code

        parts = [
            value
            for value in (
                street_address,
                locality,
            )
            if value
        ]

        return (
            ", ".join(parts)
            if parts
            else None
        )

    def _reset_run_state(
        self,
    ) -> None:
        """Reset run state.

        :return: Result produced by reset run state.
        """
        self._declared_state_count = 0
        self._declared_city_count = 0
        self._declared_store_count = 0

        self._discovered_city_count = 0
        self._discovered_location_id_count = 0
        self._successful_locator_count = 0

        self._failed_city_pages = []
        self._failed_locator_requests = []

        self._locator_failure_status_counts = {}
        self._locator_failure_type_counts = {}
        self._browser_fallback_count = 0
        self._browser_fallback_failures = []

    @staticmethod
    def _as_int(
        value: Any,
    ) -> int | None:
        """Handle as int.

        :param value: Value to normalize or convert.
        :return: Result produced by as int.
        """
        try:
            return int(value)
        except (
            TypeError,
            ValueError,
        ):
            return None

    @staticmethod
    def _as_float(
        value: Any,
    ) -> float | None:
        """Handle as float.

        :param value: Value to normalize or convert.
        :return: Result produced by as float.
        """
        try:
            return float(value)
        except (
            TypeError,
            ValueError,
        ):
            return None

    @staticmethod
    def _clean_text(
        value: Any,
    ) -> str | None:
        """Normalize text.

        :param value: Value to normalize or convert.
        :return: Result produced by clean text.
        """
        if value is None:
            return None

        text = str(value).strip()
        return text or None

    @staticmethod
    def _utc_now() -> str:
        """Handle utc now.

        :return: Result produced by utc now.
        """
        return datetime.now(
            timezone.utc
        ).isoformat()


__all__ = [
    "KrogerAcquisitionStrategyV5",
]