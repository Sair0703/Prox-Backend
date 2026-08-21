from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import threading
import time
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

from services.store_service.capabilities.store_location_acquisition.protocals import (
    AcquisitionArtifact,
    AcquisitionSourceInfo,
    AcquisitionValidationResult,
    StoreLocationAcquisitionStrategy,
)

BASE_URL = "https://stores.foodlion.com"
SCHEMA_BASE_URL = "https://schema.milestoneinternet.com/schema/stores.foodlion.com"
ROOT_URL = f"{BASE_URL}/"

STATE_LINK_SELECTOR = (
    'a.Directory-listLink[data-ya-track="todirectory"]'
)

STATE_PATH_RE = re.compile(
    r"^/(?P<state>[a-z]{2})/?$",
    re.IGNORECASE,
)

LOCATION_PATH_RE = re.compile(
    r"^/(?P<state>[a-z]{2})/(?P<parts>[^/]+(?:/[^/]+){0,2})/?$",
    re.IGNORECASE,
)

US_STATE_CODES = {
    "DE",
    "GA",
    "KY",
    "MD",
    "NC",
    "PA",
    "SC",
    "TN",
    "VA",
    "WV",
}

STORE_DETAIL_PARTS = 3


@dataclass(frozen=True, slots=True)
class _StateEntry:
    """Represent StateEntry used by the acquisition workflow."""
    state_code: str
    state_name: str
    url: str
    expected_store_count: int | None


@dataclass(frozen=True, slots=True)
class _LocationEntry:
    """Represent LocationEntry used by the acquisition workflow."""
    url: str
    state_code: str
    kind: str  # "store" or "city"


class FoodLionAcquisitionStrategyV2(
    StoreLocationAcquisitionStrategy
):
    """
    Food Lion v2.

    Discovery/acquisition is driven by Milestone schema JSON instead of
    parsing the store-directory HTML for every location:

        root HTML
          -> state URLs
          -> state schema.json
          -> location URLs
          -> city/store schema.json
          -> store schema / JSON-LD
          -> normalized store records

    The strategy intentionally does not invent a retailer store number.
    If a store-level identifier is present in the schema payload, it is
    used; otherwise store_number remains None.
    """

    retailer_key = "food_lion"
    retailer_name = "Food Lion"

    def __init__(
        self,
        *,
        state_workers: int = 16,
        schema_workers: int = 32,
        parse_workers: int = 32,
        request_timeout: int = 30,
        max_retries: int = 4,
        retry_backoff_base: float = 1.0,
        retry_backoff_max: float = 16.0,
        debug_failed_limit: int = 25,
    ) -> None:
        """Initialize acquisition configuration and run state."""
        self.state_workers = state_workers
        self.schema_workers = schema_workers
        self.parse_workers = parse_workers
        self.request_timeout = request_timeout
        self.max_retries = max_retries
        self.retry_backoff_base = retry_backoff_base
        self.retry_backoff_max = retry_backoff_max
        self.debug_failed_limit = debug_failed_limit

        self._thread_local = threading.local()

        self._expected_store_count = 0
        self._state_entries: list[_StateEntry] = []

        self._failed_state_schema: list[dict[str, Any]] = []
        self._failed_location_schema: list[dict[str, Any]] = []

        self._http_status_counts: dict[str, int] = {}
        self._error_type_counts: dict[str, int] = {}

        self._discovered_store_urls: set[str] = set()
        self._discovered_city_urls: set[str] = set()

    def discover_source(self) -> AcquisitionSourceInfo:
        """Return metadata describing the retailer's official acquisition source."""
        return AcquisitionSourceInfo(
            retailer_key=self.retailer_key,
            retailer_name=self.retailer_name,
            official_website_url="https://www.foodlion.com/",
            store_locator_url=ROOT_URL,
            endpoint_url=SCHEMA_BASE_URL,
            source_type="api",
            provider="Food Lion Milestone schema JSON",
            notes=(
                "Uses the official Food Lion directory plus Milestone "
                "schema JSON resources. State schema provides canonical "
                "location URLs; city/store schema resources are then used "
                "to obtain structured Store Info."
            ),
        )

    def fetch_raw_artifacts(
        self,
    ) -> list[AcquisitionArtifact]:
        """Fetch raw artifacts required for store location acquisition."""
        self._reset_run_state()

        artifacts: list[AcquisitionArtifact] = []

        root_html = self._fetch_text(
            ROOT_URL,
            page_type="root",
        )

        states = self._parse_state_entries(
            root_html
        )

        if not states:
            raise RuntimeError(
                "Food Lion root directory returned no state links."
            )

        self._state_entries = states
        self._expected_store_count = sum(
            state.expected_store_count or 0
            for state in states
        )

        artifacts.append(
            AcquisitionArtifact(
                artifact_type="html",
                source_url=ROOT_URL,
                content=root_html,
                metadata={
                    "retrieved_at_utc": self._utc_now(),
                    "page_type": "root",
                    "http_status": 200,
                    "scrape_status": "success",
                    "state_count": len(states),
                    "expected_store_count": self._expected_store_count,
                },
            )
        )

        # ----------------------------------------------------------
        # Stage 1: state page -> state schema JSON
        # ----------------------------------------------------------
        location_entries: dict[str, _LocationEntry] = {}

        with ThreadPoolExecutor(
            max_workers=self.state_workers
        ) as pool:
            futures = {
                pool.submit(
                    self._fetch_state_schema,
                    state,
                ): state
                for state in states
            }

            for future in tqdm(
                as_completed(futures),
                total=len(futures),
                desc="Food Lion state schema",
                unit="state",
            ):
                state = futures[future]

                try:
                    artifact, locations = future.result()
                except Exception as exc:
                    self._failed_state_schema.append(
                        {
                            "state_code": state.state_code,
                            "state_name": state.state_name,
                            "url": state.url,
                            "schema_url": self._build_schema_url(
                                state.url
                            ),
                            "error": str(exc),
                        }
                    )
                    continue

                artifacts.append(artifact)

                for location in locations:
                    location_entries[
                        location.url
                    ] = location

        if not location_entries:
            raise RuntimeError(
                "Food Lion state schema JSON returned no location URLs."
            )

        # ----------------------------------------------------------
        # Stage 2: city schema JSON expands city pages into store URLs;
        #           store schema JSON is fetched directly.
        # ----------------------------------------------------------
        pending_city_entries = [
            entry
            for entry in location_entries.values()
            if entry.kind == "city"
        ]

        pending_store_entries = {
            entry.url: entry
            for entry in location_entries.values()
            if entry.kind == "store"
        }

        # Existing state schema can already contain direct store URLs.
        # Fetch city schema only for actual city aggregation pages.
        if pending_city_entries:
            with ThreadPoolExecutor(
                max_workers=self.schema_workers
            ) as pool:
                futures = {
                    pool.submit(
                        self._fetch_location_schema,
                        entry,
                    ): entry
                    for entry in pending_city_entries
                }

                for future in tqdm(
                    as_completed(futures),
                    total=len(futures),
                    desc="Food Lion city schema",
                    unit="city",
                ):
                    entry = futures[future]

                    try:
                        artifact, child_locations = (
                            future.result()
                        )
                    except Exception as exc:
                        self._failed_location_schema.append(
                            {
                                "url": entry.url,
                                "schema_url": self._build_schema_url(
                                    entry.url
                                ),
                                "kind": entry.kind,
                                "error": str(exc),
                            }
                        )
                        continue

                    artifacts.append(artifact)

                    for child in child_locations:
                        if child.kind == "store":
                            pending_store_entries[
                                child.url
                            ] = child

        # ----------------------------------------------------------
        # Stage 3: direct store schema JSON.
        # ----------------------------------------------------------
        store_entries = list(
            pending_store_entries.values()
        )

        with ThreadPoolExecutor(
            max_workers=self.schema_workers
        ) as pool:
            futures = {
                pool.submit(
                    self._fetch_location_schema,
                    entry,
                ): entry
                for entry in store_entries
            }

            for future in tqdm(
                as_completed(futures),
                total=len(futures),
                desc="Food Lion store schema",
                unit="store",
            ):
                entry = futures[future]

                try:
                    artifact, _ = future.result()
                except Exception as exc:
                    self._failed_location_schema.append(
                        {
                            "url": entry.url,
                            "schema_url": self._build_schema_url(
                                entry.url
                            ),
                            "kind": entry.kind,
                            "error": str(exc),
                        }
                    )
                    continue

                artifacts.append(artifact)

        self._discovered_store_urls = {
            entry.url
            for entry in store_entries
        }

        self._discovered_city_urls = {
            entry.url
            for entry in pending_city_entries
        }

        return artifacts

    def extract_store_payloads(
        self,
        artifacts: Sequence[AcquisitionArtifact],
    ) -> list[dict[str, Any]]:
        """Extract normalized store payloads from acquired artifacts."""
        store_schema_artifacts = [
            artifact
            for artifact in artifacts
            if artifact.metadata.get(
                "page_type"
            ) == "store_schema"
            and artifact.metadata.get(
                "scrape_status"
            ) == "success"
            and artifact.content
        ]

        rows_by_store_url: dict[
            str,
            dict[str, Any],
        ] = {}

        with ThreadPoolExecutor(
            max_workers=self.parse_workers
        ) as pool:
            futures = {
                pool.submit(
                    self._parse_store_schema_artifact,
                    artifact,
                ): artifact
                for artifact in store_schema_artifacts
            }

            for future in tqdm(
                as_completed(futures),
                total=len(futures),
                desc="Parsing Food Lion store schema",
                unit="store",
            ):
                row = future.result()

                if not row:
                    continue

                store_url = self._clean_text(
                    row.get("store_url")
                )

                if store_url:
                    rows_by_store_url[
                        store_url
                    ] = row

        return list(
            rows_by_store_url.values()
        )

    def validate_store_payloads(
        self,
        payloads: Sequence[Mapping[str, Any]],
    ) -> AcquisitionValidationResult:
        """Validate acquired store payloads for completeness and uniqueness."""
        total_records = len(payloads)

        store_urls = [
            self._clean_text(
                row.get("store_url")
            )
            for row in payloads
        ]

        unique_store_urls = len(
            {
                url
                for url in store_urls
                if url
            }
        )

        store_numbers = [
            self._clean_text(
                row.get("store_number")
            )
            for row in payloads
        ]

        missing_store_numbers = sum(
            1
            for value in store_numbers
            if not value
        )

        duplicate_store_urls: list[str] = []
        seen_urls: set[str] = set()

        for store_url in store_urls:
            if not store_url:
                continue

            if (
                store_url in seen_urls
                and store_url
                not in duplicate_store_urls
            ):
                duplicate_store_urls.append(
                    store_url
                )

            seen_urls.add(
                store_url
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

        if missing_store_numbers:
            issue_counts[
                "missing_store_numbers"
            ] = missing_store_numbers

        if duplicate_store_urls:
            issue_counts[
                "duplicate_store_urls"
            ] = len(
                duplicate_store_urls
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

        if self._failed_state_schema:
            issue_counts[
                "failed_state_schema"
            ] = len(
                self._failed_state_schema
            )

        if self._failed_location_schema:
            issue_counts[
                "failed_location_schema"
            ] = len(
                self._failed_location_schema
            )

        if (
            self._expected_store_count
            and total_records != self._expected_store_count
        ):
            issue_counts[
                "declared_count_mismatch"
            ] = 1

        notes = [
            (
                "Official source chain: Food Lion directory -> "
                "Milestone schema JSON -> store schema JSON."
            ),
            (
                "State schema JSON is used for canonical location discovery; "
                "city schema JSON expands multi-store city pages."
            ),
            (
                "Store-level information is parsed from the structured "
                "GroceryStore/schema payload rather than store-page HTML."
            ),
            (
                "The strategy uses an identifier exposed by the store schema "
                "when present; it does not infer a store number from the "
                "address slug."
            ),
            (
                f"Root-declared store count: "
                f"{self._expected_store_count}"
            ),
            (
                f"Workers: state={self.state_workers}, "
                f"schema={self.schema_workers}, "
                f"parse={self.parse_workers}"
            ),
        ]

        if self._http_status_counts:
            notes.append(
                "HTTP status counts: "
                + ", ".join(
                    f"{key}={value}"
                    for key, value in sorted(
                        self._http_status_counts.items()
                    )
                )
            )

        if self._error_type_counts:
            notes.append(
                "Request error types: "
                + ", ".join(
                    f"{key}={value}"
                    for key, value in sorted(
                        self._error_type_counts.items()
                    )
                )
            )

        is_valid = (
            total_records > 0
            and not duplicate_store_urls
            and missing_addresses == 0
            and missing_phones == 0
            and missing_coordinates == 0
            and not self._failed_state_schema
            and not self._failed_location_schema
            and (
                not self._expected_store_count
                or total_records == self._expected_store_count
            )
        )

        return AcquisitionValidationResult(
            is_valid=is_valid,
            total_records=total_records,
            unique_store_ids=unique_store_urls,
            missing_store_ids=0,
            missing_coordinates=missing_coordinates,
            non_us_records=0,
            duplicate_store_ids=[],
            issue_counts=issue_counts,
            notes=notes,
        )

    def build_run_notes(
        self,
    ) -> list[str]:
        """Return acquisition source and execution details for the run summary."""
        return [
            f"Directory source: {ROOT_URL}",
            f"Schema source: {SCHEMA_BASE_URL}",
            (
                "Method: requests + BeautifulSoup for root discovery; "
                "Milestone schema JSON for location/store acquisition"
            ),
            (
                "Hierarchy: root -> state schema -> "
                "city/store schema -> structured Store Info"
            ),
            "No Playwright required.",
            (
                "Canonical store URLs are retained as stable location "
                "references."
            ),
            (
                "Store number is taken from an authoritative schema "
                "identifier when exposed; otherwise it remains empty."
            ),
            (
                f"Workers: state={self.state_workers}, "
                f"schema={self.schema_workers}, "
                f"parse={self.parse_workers}"
            ),
        ]

    def _fetch_state_schema(
        self,
        state: _StateEntry,
    ) -> tuple[
        AcquisitionArtifact,
        list[_LocationEntry],
    ]:
        """Fetch state schema."""
        schema_url = self._build_schema_url(
            state.url
        )

        payload = self._fetch_json(
            schema_url,
            request_name=f"state:{state.state_code}",
        )

        locations = self._extract_location_entries(
            payload,
            state_code=state.state_code,
        )

        if not locations:
            raise RuntimeError(
                f"No location URLs found in Food Lion state schema: "
                f"{schema_url}"
            )

        return (
            AcquisitionArtifact(
                artifact_type="json",
                source_url=schema_url,
                content=json.dumps(
                    payload
                ),
                metadata={
                    "retrieved_at_utc": self._utc_now(),
                    "page_type": "state_schema",
                    "state_code": state.state_code,
                    "state_name": state.state_name,
                    "expected_store_count": (
                        state.expected_store_count
                    ),
                    "http_status": 200,
                    "scrape_status": "success",
                },
            ),
            locations,
        )

    def _fetch_location_schema(
        self,
        entry: _LocationEntry,
    ) -> tuple[
        AcquisitionArtifact,
        list[_LocationEntry],
    ]:
        """Fetch location schema."""
        schema_url = self._build_schema_url(
            entry.url
        )

        payload = self._fetch_json(
            schema_url,
            request_name=f"{entry.kind}:{entry.url}",
        )

        child_locations: list[
            _LocationEntry
        ] = []

        if entry.kind == "city":
            child_locations = self._extract_location_entries(
                payload,
                state_code=entry.state_code,
            )

        page_type = (
            "store_schema"
            if entry.kind == "store"
            else "city_schema"
        )

        return (
            AcquisitionArtifact(
                artifact_type="json",
                source_url=schema_url,
                content=json.dumps(
                    payload
                ),
                metadata={
                    "retrieved_at_utc": self._utc_now(),
                    "page_type": page_type,
                    "location_url": entry.url,
                    "state_code": entry.state_code,
                    "http_status": 200,
                    "scrape_status": "success",
                },
            ),
            child_locations,
        )

    def _parse_store_schema_artifact(
        self,
        artifact: AcquisitionArtifact,
    ) -> dict[str, Any] | None:
        """Parse store schema artifact."""
        try:
            payload = json.loads(
                artifact.content
            )
        except json.JSONDecodeError:
            return None

        objects = self._flatten_schema_objects(
            payload
        )

        grocery_store = next(
            (
                obj
                for obj in objects
                if self._is_store_schema_object(
                    obj
                )
            ),
            None,
        )

        if grocery_store is None:
            return None

        store_url = self._clean_text(
            grocery_store.get("url")
        ) or self._clean_text(
            artifact.metadata.get(
                "location_url"
            )
        )

        if not store_url:
            return None

        address = (
            grocery_store.get(
                "address"
            )
            if isinstance(
                grocery_store.get(
                    "address"
                ),
                dict,
            )
            else {}
        )

        geo = (
            grocery_store.get(
                "geo"
            )
            if isinstance(
                grocery_store.get(
                    "geo"
                ),
                dict,
            )
            else {}
        )

        phone = self._first_text(
            grocery_store.get(
                "telephone"
            )
        )

        name = self._first_text(
            grocery_store.get(
                "name"
            )
        )

        street_address = self._first_text(
            address.get(
                "streetAddress"
            )
        )

        city = self._first_text(
            address.get(
                "addressLocality"
            )
        )

        state = self._first_text(
            address.get(
                "addressRegion"
            )
        )

        zip_code = self._first_text(
            address.get(
                "postalCode"
            )
        )

        latitude = self._first_float(
            geo.get(
                "latitude"
            )
        )

        longitude = self._first_float(
            geo.get(
                "longitude"
            )
        )

        identifier = self._extract_store_identifier(
            grocery_store
        )

        full_address = self._build_full_address(
            street_address=street_address,
            city=city,
            state=state,
            zip_code=zip_code,
        )

        return {
            "retailer": self.retailer_name,
            "retailer_store_id": identifier,
            "store_number": identifier,
            "store_type": "Grocery",
            "store_name": name,
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
            "store_url": store_url,
            "source_url": store_url,
            "source_sitemap": artifact.source_url,
            "extraction_source": (
                "Food Lion Milestone store schema JSON"
            ),
            "schema_source": artifact.source_url,
            "scrape_status": "success",
            "http_status": artifact.metadata.get(
                "http_status"
            ),
            "error_message": None,
            "scraped_at_utc": artifact.metadata.get(
                "retrieved_at_utc"
            ),
        }

    def _extract_location_entries(
        self,
        payload: Any,
        *,
        state_code: str,
    ) -> list[_LocationEntry]:
        """Extract location entries."""
        entries: list[
            _LocationEntry
        ] = []

        for obj in self._flatten_schema_objects(
            payload
        ):
            if obj.get("@type") != "ItemList":
                continue

            items = obj.get(
                "itemListElement"
            )

            if not isinstance(
                items,
                list,
            ):
                continue

            for item in items:
                if not isinstance(
                    item,
                    dict,
                ):
                    continue

                target = item.get(
                    "item"
                )

                if not isinstance(
                    target,
                    dict,
                ):
                    continue

                url = self._clean_text(
                    target.get(
                        "url"
                    )
                )

                if not url:
                    continue

                kind = self._classify_location_url(
                    url
                )

                if kind is None:
                    continue

                entries.append(
                    _LocationEntry(
                        url=url,
                        state_code=state_code,
                        kind=kind,
                    )
                )

        return self._dedupe_location_entries(
            entries
        )

    @staticmethod
    def _classify_location_url(
        url: str,
    ) -> str | None:
        """Handle classify location url."""
        path = urlparse(
            url
        ).path.strip("/")

        parts = path.split("/")

        if len(parts) != 2:
            if len(parts) == 3:
                return "store"
            return None

        state, city = parts

        if (
            len(state) == 2
            and state.upper() in US_STATE_CODES
            and city
        ):
            return "city"

        return None

    @staticmethod
    def _flatten_schema_objects(
        payload: Any,
    ) -> list[dict[str, Any]]:
        """Handle flatten schema objects."""
        output: list[dict[str, Any]] = []

        def visit(
            value: Any,
        ) -> None:
            """Handle visit."""
            if isinstance(
                value,
                dict,
            ):
                if "@type" in value:
                    output.append(
                        value
                    )

                for child in value.values():
                    visit(
                        child
                    )

            elif isinstance(
                value,
                list,
            ):
                for child in value:
                    visit(
                        child
                    )

        visit(
            payload
        )
        return output

    @staticmethod
    def _is_store_schema_object(
        obj: Mapping[str, Any],
    ) -> bool:
        """Return whether store schema object."""
        raw_type = obj.get(
            "@type"
        )

        if isinstance(
            raw_type,
            list,
        ):
            return any(
                str(value).lower()
                in {
                    "groceryStore".lower(),
                    "localbusiness",
                    "store",
                }
                for value in raw_type
            )

        if isinstance(
            raw_type,
            str,
        ):
            return raw_type.lower() in {
                "grocerystore",
                "localbusiness",
                "store",
            }

        return False

    @staticmethod
    def _extract_store_identifier(
        obj: Mapping[str, Any],
    ) -> str | None:
        """Extract store identifier."""
        candidate_keys = (
            "identifier",
            "branchCode",
            "storeNumber",
            "storeId",
            "locationId",
            "globalLocationNumber",
        )

        for key in candidate_keys:
            value = obj.get(
                key
            )

            value = FoodLionAcquisitionStrategyV2._first_text(
                value
            )

            if value:
                return value

        return None

    def _fetch_json(
        self,
        url: str,
        *,
        request_name: str,
    ) -> Any:
        """Fetch json."""
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
                )

                self._record_http_status(
                    response.status_code
                )

                response.raise_for_status()

                return response.json()

            except Exception as exc:
                last_error = exc
                self._record_error_type(
                    exc
                )

                if attempt < self.max_retries:
                    delay = min(
                        self.retry_backoff_base
                        * (
                            2 ** (
                                attempt - 1
                            )
                        ),
                        self.retry_backoff_max,
                    )
                    time.sleep(
                        delay
                    )

        raise RuntimeError(
            f"Failed to fetch Food Lion schema "
            f"{request_name} {url} after "
            f"{self.max_retries} attempts: {last_error}"
        ) from last_error

    def _fetch_text(
        self,
        url: str,
        *,
        page_type: str,
    ) -> str:
        """Fetch text."""
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
                )

                self._record_http_status(
                    response.status_code
                )

                response.raise_for_status()

                if not response.text:
                    raise RuntimeError(
                        f"Empty response body for "
                        f"Food Lion {page_type}: {url}"
                    )

                return response.text

            except Exception as exc:
                last_error = exc
                self._record_error_type(
                    exc
                )

                if attempt < self.max_retries:
                    delay = min(
                        self.retry_backoff_base
                        * (
                            2 ** (
                                attempt - 1
                            )
                        ),
                        self.retry_backoff_max,
                    )
                    time.sleep(
                        delay
                    )

        raise RuntimeError(
            f"Failed to fetch Food Lion {page_type} "
            f"{url} after {self.max_retries} attempts: "
            f"{last_error}"
        ) from last_error

    def _get_session(
        self,
    ) -> requests.Session:
        """Return session."""
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
                    "origin": BASE_URL,
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

    def _build_schema_url(
        self,
        page_url: str,
    ) -> str:
        """Build schema url."""
        path = urlparse(
            page_url
        ).path.strip("/")

        return (
            f"{SCHEMA_BASE_URL}/"
            f"{path}/schema.json"
        )

    def _parse_state_entries(
        self,
        html: str,
    ) -> list[_StateEntry]:
        """Parse state entries."""
        soup = BeautifulSoup(
            html or "",
            "html.parser",
        )

        entries: list[
            _StateEntry
        ] = []

        for anchor in soup.select(
            STATE_LINK_SELECTOR
        ):
            href = self._clean_text(
                anchor.get(
                    "href"
                )
            )

            if not href:
                continue

            absolute_url = urljoin(
                BASE_URL,
                href,
            )

            match = STATE_PATH_RE.fullmatch(
                urlparse(
                    absolute_url
                ).path
            )

            if not match:
                continue

            state_code = match.group(
                "state"
            ).upper()

            if state_code not in US_STATE_CODES:
                continue

            state_name = self._clean_text(
                anchor.get_text(
                    " ",
                    strip=True,
                )
            )

            if not state_name:
                continue

            expected = self._extract_count(
                anchor.get(
                    "data-count"
                )
            )

            entries.append(
                _StateEntry(
                    state_code=state_code,
                    state_name=state_name,
                    url=absolute_url,
                    expected_store_count=expected,
                )
            )

        return self._dedupe_state_entries(
            entries
        )

    @staticmethod
    def _extract_count(
        value: Any,
    ) -> int | None:
        """Extract count."""
        if value is None:
            return None

        match = re.search(
            r"\d+",
            str(value),
        )

        return (
            int(match.group(0))
            if match
            else None
        )

    @staticmethod
    def _dedupe_state_entries(
        entries: Sequence[_StateEntry],
    ) -> list[_StateEntry]:
        """Deduplicate state entries."""
        output: list[
            _StateEntry
        ] = []

        seen: set[str] = set()

        for entry in entries:
            if entry.url in seen:
                continue

            seen.add(
                entry.url
            )
            output.append(
                entry
            )

        return output

    @staticmethod
    def _dedupe_location_entries(
        entries: Sequence[_LocationEntry],
    ) -> list[_LocationEntry]:
        """Deduplicate location entries."""
        output: list[
            _LocationEntry
        ] = []

        seen: set[str] = set()

        for entry in entries:
            if entry.url in seen:
                continue

            seen.add(
                entry.url
            )
            output.append(
                entry
            )

        return output

    @staticmethod
    def _first_text(
        value: Any,
    ) -> str | None:
        """Handle first text."""
        if isinstance(
            value,
            list,
        ):
            for item in value:
                result = FoodLionAcquisitionStrategyV2._first_text(
                    item
                )
                if result:
                    return result
            return None

        if value is None:
            return None

        if isinstance(
            value,
            (dict, list),
        ):
            return None

        text = str(
            value
        ).strip()

        return text or None

    @staticmethod
    def _first_float(
        value: Any,
    ) -> float | None:
        """Handle first float."""
        if isinstance(
            value,
            list,
        ):
            for item in value:
                result = FoodLionAcquisitionStrategyV2._first_float(
                    item
                )
                if result is not None:
                    return result
            return None

        if value is None:
            return None

        try:
            return float(
                value
            )
        except (
            TypeError,
            ValueError,
        ):
            return None

    @staticmethod
    def _build_full_address(
        *,
        street_address: str | None,
        city: str | None,
        state: str | None,
        zip_code: str | None,
    ) -> str | None:
        """Build full address."""
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

    def _record_http_status(
        self,
        status_code: int,
    ) -> None:
        """Record http status."""
        key = str(
            status_code
        )
        self._http_status_counts[
            key
        ] = (
            self._http_status_counts.get(
                key,
                0,
            )
            + 1
        )

    def _record_error_type(
        self,
        exc: Exception,
    ) -> None:
        """Record error type."""
        key = type(
            exc
        ).__name__

        self._error_type_counts[
            key
        ] = (
            self._error_type_counts.get(
                key,
                0,
            )
            + 1
        )

    def _reset_run_state(
        self,
    ) -> None:
        """Reset run state."""
        self._expected_store_count = 0
        self._state_entries = []

        self._failed_state_schema = []
        self._failed_location_schema = []

        self._http_status_counts = {}
        self._error_type_counts = {}

        self._discovered_store_urls = set()
        self._discovered_city_urls = set()

    @staticmethod
    def _clean_text(
        value: Any,
    ) -> str | None:
        """Normalize text."""
        if value is None:
            return None

        text = str(
            value
        ).strip()

        return text or None

    @staticmethod
    def _utc_now() -> str:
        """Return the current UTC timestamp in ISO 8601 format."""
        return datetime.now(
            timezone.utc
        ).isoformat()


__all__ = [
    "FoodLionAcquisitionStrategyV2",
]