from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import re
import threading
import time
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

from services.store_service.capabilities.store_location_acquisition.protocals import (
    AcquisitionArtifact,
    AcquisitionSourceInfo,
    AcquisitionValidationResult,
    StoreLocationAcquisitionStrategy,
)

BASE_URL = "https://www.costco.com"
ROOT_URL = f"{BASE_URL}/sitemaps/warehouses-by-state"

STATE_LINK_SELECTOR = 'a[data-testid="Link"]'
WAREHOUSE_LINK_SELECTOR = 'a[data-testid="Link"][href^="/w/-/"]'

STATE_PATH_RE = re.compile(
    r"^/sitemaps/warehouses-by-state/(?P<state>[A-Z]{2})/?$"
)
WAREHOUSE_PATH_RE = re.compile(
    r"^/w/-/(?P<state>[a-z]{2})/(?P<city>[^/]+)/(?P<store_id>[^/?#]+)$",
    re.IGNORECASE,
)

ZIP_RE = re.compile(r"^\d{5}(?:-\d{4})?$")

USPS_TO_NAME = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "PR": "Puerto Rico",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WI": "Wisconsin",
    "WV": "West Virginia",
    "WY": "Wyoming",
}

ALLOWED_US_TERRITORIES = {"PR"}

RETRYABLE_STATUS_CODES = {403, 408, 425, 429, 500, 502, 503, 504}


@dataclass(frozen=True, slots=True)
class _StateEntry:
    """Represent StateEntry data used by the acquisition strategy."""
    state_code: str
    state_name: str
    url: str
    expected_store_count: int | None


@dataclass(frozen=True, slots=True)
class _WarehouseEntry:
    """Represent WarehouseEntry data used by the acquisition strategy."""
    state_code: str
    city_slug: str
    store_id: str
    url: str


class CostcoAcquisitionStrategyV2(
    StoreLocationAcquisitionStrategy
):
    """Represent CostcoAcquisitionStrategyV2 data used by the acquisition strategy."""
    retailer_key = "costco"
    retailer_name = "Costco"

    def __init__(
        self,
        *,
        state_workers: int = 16,
        store_workers: int = 16,
        retry_workers: int = 8,
        parse_workers: int = 32,
        request_timeout: int = 30,
        per_request_attempts: int = 1,
        retry_rounds: int = 3,
        retry_round_delays: tuple[float, ...] = (15.0, 45.0, 90.0),
        retry_round_workers: tuple[int, ...] = (8, 4, 2),
        retry_backoff_base: float = 1.0,
        retry_backoff_max: float = 16.0,
        debug_failed_limit: int = 25,
    ) -> None:
        """Initialize acquisition configuration and run state."""
        self.state_workers = state_workers
        self.store_workers = store_workers
        self.retry_workers = retry_workers
        self.parse_workers = parse_workers
        self.request_timeout = request_timeout
        self.per_request_attempts = per_request_attempts
        self.retry_rounds = retry_rounds
        self.retry_round_delays = retry_round_delays
        self.retry_round_workers = retry_round_workers
        self.retry_backoff_base = retry_backoff_base
        self.retry_backoff_max = retry_backoff_max
        self.debug_failed_limit = debug_failed_limit

        self._thread_local = threading.local()

        self._expected_store_count = 0
        self._failed_state_pages: list[dict[str, Any]] = []
        self._failed_store_pages: list[dict[str, Any]] = []
        self._state_count_mismatches: list[dict[str, Any]] = []

        self._request_status_counts: dict[str, int] = {}
        self._request_error_type_counts: dict[str, int] = {}
        self._deferred_store_count = 0
        self._retry_success_count = 0

    def discover_source(self) -> AcquisitionSourceInfo:
        """Return metadata describing the retailer's official acquisition source."""
        return AcquisitionSourceInfo(
            retailer_key=self.retailer_key,
            retailer_name=self.retailer_name,
            official_website_url="https://www.costco.com/",
            store_locator_url=ROOT_URL,
            endpoint_url=ROOT_URL,
            source_type="html",
            provider="Costco official warehouse sitemap",
            notes=(
                "v2 keeps the official Costco sitemap hierarchy, treats "
                "bounded detail-page concurrency, exponential backoff, "
                "deferred retry, and request diagnostics."
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
                "Costco warehouse sitemap returned no state links."
            )

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

        state_artifacts, warehouses = self._acquire_states(
            states
        )
        artifacts.extend(
            state_artifacts
        )

        if not warehouses:
            raise RuntimeError(
                "Costco state pages were fetched, but no warehouse links "
                "were discovered."
            )

        self._acquire_store_pages(
            warehouses=warehouses,
            artifacts=artifacts,
        )

        return artifacts

    def extract_store_payloads(
        self,
        artifacts: Sequence[AcquisitionArtifact],
    ) -> list[dict[str, Any]]:
        """Extract normalized store payloads from acquired artifacts."""
        store_artifacts = [
            artifact
            for artifact in artifacts
            if artifact.metadata.get(
                "page_type"
            ) == "store"
            and artifact.metadata.get(
                "scrape_status"
            ) == "success"
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
                    self._parse_store_artifact,
                    artifact,
                ): artifact
                for artifact in store_artifacts
            }

            for future in tqdm(
                as_completed(futures),
                total=len(futures),
                desc="Parsing Costco warehouses",
                unit="store",
            ):
                row = future.result()

                if not row:
                    continue

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
        """Validate acquired store payloads for completeness and uniqueness."""
        total_records = len(
            payloads
        )

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

        missing_store_urls = sum(
            1
            for row in payloads
            if not self._clean_text(
                row.get("store_url")
            )
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

        if missing_store_urls:
            issue_counts[
                "missing_store_urls"
            ] = missing_store_urls

        if self._failed_state_pages:
            issue_counts[
                "failed_state_pages"
            ] = len(
                self._failed_state_pages
            )

        if self._failed_store_pages:
            issue_counts[
                "failed_store_pages"
            ] = len(
                self._failed_store_pages
            )

        if self._state_count_mismatches:
            issue_counts[
                "state_count_mismatches"
            ] = len(
                self._state_count_mismatches
            )

        if (
            self._expected_store_count
            and total_records
            != self._expected_store_count
        ):
            issue_counts[
                "declared_store_count_mismatch"
            ] = 1

        notes = [
            (
                "Official source: Costco warehouse sitemap and "
                "warehouse detail pages."
            ),
            (
                "v1 uses bounded detail-page concurrency, "
                "per-request retry/backoff, and a deferred retry pass "
                "for stores that temporarily fail."
            ),
            (
                "retailer_store_id is the final path segment of the "
                "canonical warehouse URL, e.g. "
                "/w/-/al/hoover/362 -> 362."
            ),
            (
                f"Root/state sitemap declared warehouses: "
                f"{self._expected_store_count}"
            ),
            (
                f"Workers: state={self.state_workers}, "
                f"store={self.store_workers}, "
                f"retry={self.retry_workers}, "
                f"parse={self.parse_workers}"
            ),
            (
                f"Final deferred failures: {self._deferred_store_count}; "
                f"retry successes: {self._retry_success_count}"
            ),
            (
                "Puerto Rico (PR) is retained as a US retailer location "
                "because Costco lists it in the US warehouse sitemap."
            ),
        ]

        if self._request_status_counts:
            notes.append(
                "HTTP status counts: "
                + ", ".join(
                    f"{key}={value}"
                    for key, value in sorted(
                        self._request_status_counts.items()
                    )
                )
            )

        if self._request_error_type_counts:
            notes.append(
                "Request error types: "
                + ", ".join(
                    f"{key}={value}"
                    for key, value in sorted(
                        self._request_error_type_counts.items()
                    )
                )
            )

        is_valid = (
            total_records > 0
            and missing_store_ids == 0
            and not duplicate_store_ids
            and missing_addresses == 0
            and missing_phones == 0
            and missing_store_urls == 0
            and not self._failed_state_pages
            and not self._failed_store_pages
            and not self._state_count_mismatches
            and (
                not self._expected_store_count
                or total_records
                == self._expected_store_count
            )
        )

        return AcquisitionValidationResult(
            is_valid=is_valid,
            total_records=total_records,
            unique_store_ids=unique_store_ids,
            missing_store_ids=missing_store_ids,
            missing_coordinates=0,
            non_us_records=0,
            duplicate_store_ids=duplicate_store_ids,
            issue_counts=issue_counts,
            notes=notes,
        )

    def build_run_notes(
        self,
    ) -> list[str]:
        """Return acquisition source and execution details for the run summary."""
        return [
            f"Source: {ROOT_URL}",
            "Method: requests + BeautifulSoup",
            (
                "Hierarchy: all states -> state pages -> "
                "warehouse detail pages"
            ),
            (
                "v1: bounded detail-page concurrency + "
                "retry/backoff + deferred retry queue"
            ),
            (
                "Canonical warehouse ID: final path segment of "
                "/w/-/<state>/<city>/<id>"
            ),
            (
                "Address and phone are parsed directly from "
                "official Costco warehouse pages."
            ),
            (
                "Puerto Rico is retained as PR because it is included "
                "in Costco's US sitemap."
            ),
        ]

    def _acquire_states(
        self,
        states: Sequence[_StateEntry],
    ) -> tuple[
        list[AcquisitionArtifact],
        list[_WarehouseEntry],
    ]:
        """Handle acquire states."""
        state_artifacts: list[
            AcquisitionArtifact
        ] = []

        with ThreadPoolExecutor(
            max_workers=self.state_workers
        ) as pool:
            futures = {
                pool.submit(
                    self._fetch_state_artifact,
                    state,
                ): state
                for state in states
            }

            for future in tqdm(
                as_completed(futures),
                total=len(futures),
                desc="Costco states",
                unit="state",
            ):
                state = futures[
                    future
                ]

                try:
                    artifact, warehouses = (
                        future.result()
                    )
                except Exception as exc:
                    self._failed_state_pages.append(
                        {
                            "state_code": state.state_code,
                            "state_name": state.state_name,
                            "url": state.url,
                            "error": str(exc),
                        }
                    )
                    continue

                state_artifacts.append(
                    artifact
                )

                if (
                    state.expected_store_count
                    is not None
                    and len(warehouses)
                    != state.expected_store_count
                ):
                    self._state_count_mismatches.append(
                        {
                            "state_code": state.state_code,
                            "expected": state.expected_store_count,
                            "actual": len(warehouses),
                            "url": state.url,
                        }
                    )

        warehouses: list[
            _WarehouseEntry
        ] = []

        seen_urls: set[str] = set()

        for artifact in state_artifacts:
            state_code = self._clean_text(
                artifact.metadata.get(
                    "state_code"
                )
            )

            if not state_code:
                continue

            for warehouse in self._parse_warehouse_entries(
                artifact.content or "",
                state_code=state_code,
            ):
                if warehouse.url in seen_urls:
                    continue

                seen_urls.add(
                    warehouse.url
                )
                warehouses.append(
                    warehouse
                )

        return state_artifacts, warehouses

    def _acquire_store_pages(
        self,
        *,
        warehouses: Sequence[_WarehouseEntry],
        artifacts: list[AcquisitionArtifact],
    ) -> None:
        """Handle acquire store pages."""
        pending = list(warehouses)

        # First pass: one request per store. Failures that look transient or
        # access-controlled are deferred instead of being retried immediately.
        pending = self._run_store_round(
            pending,
            workers=self.store_workers,
            round_label="Costco warehouses",
            artifacts=artifacts,
            is_initial_round=True,
        )

        for round_index in range(1, self.retry_rounds + 1):
            if not pending:
                break

            delay = self.retry_round_delays[
                min(round_index - 1, len(self.retry_round_delays) - 1)
            ]
            workers = self.retry_round_workers[
                min(round_index - 1, len(self.retry_round_workers) - 1)
            ]

            print(
                f"[Costco][deferred] round={round_index} "
                f"pending={len(pending)} workers={workers} "
                f"delay={delay}s"
            )
            time.sleep(delay)

            before = len(pending)
            pending = self._run_store_round(
                pending,
                workers=workers,
                round_label=f"Costco deferred retry {round_index}",
                artifacts=artifacts,
                is_initial_round=False,
            )
            succeeded = before - len(pending)
            self._retry_success_count += succeeded

        self._deferred_store_count = len(pending)

        for warehouse in pending:
            self._record_failed_store(
                warehouse,
                RuntimeError(
                    "Deferred retry rounds exhausted"
                ),
            )

    def _run_store_round(
        self,
        warehouses: Sequence[_WarehouseEntry],
        *,
        workers: int,
        round_label: str,
        artifacts: list[AcquisitionArtifact],
        is_initial_round: bool,
    ) -> list[_WarehouseEntry]:
        """Run store round."""
        deferred: list[_WarehouseEntry] = []

        if not warehouses:
            return deferred

        with ThreadPoolExecutor(
            max_workers=workers
        ) as pool:
            futures = {
                pool.submit(
                    self._fetch_store_artifact,
                    warehouse,
                    retry_mode=not is_initial_round,
                ): warehouse
                for warehouse in warehouses
            }

            for future in tqdm(
                as_completed(futures),
                total=len(futures),
                desc=round_label,
                unit="store",
            ):
                warehouse = futures[future]

                try:
                    artifact = future.result()
                    artifacts.append(artifact)
                except Exception as exc:
                    if self._is_deferred_retry_error(exc):
                        deferred.append(warehouse)
                    else:
                        self._record_failed_store(
                            warehouse,
                            exc,
                        )

        return deferred

    def _fetch_state_artifact(
        self,
        state: _StateEntry,
    ) -> tuple[
        AcquisitionArtifact,
        list[_WarehouseEntry],
    ]:
        """Fetch state artifact."""
        html = self._fetch_text(
            state.url,
            page_type="state",
        )

        warehouses = self._parse_warehouse_entries(
            html,
            state_code=state.state_code,
        )

        if not warehouses:
            raise RuntimeError(
                f"No Costco warehouses discovered for "
                f"{state.state_code}: {state.url}"
            )

        return (
            AcquisitionArtifact(
                artifact_type="html",
                source_url=state.url,
                content=html,
                metadata={
                    "retrieved_at_utc": self._utc_now(),
                    "page_type": "state",
                    "state_code": state.state_code,
                    "state_name": state.state_name,
                    "expected_store_count": (
                        state.expected_store_count
                    ),
                    "warehouse_count": len(
                        warehouses
                    ),
                    "http_status": 200,
                    "scrape_status": "success",
                },
            ),
            warehouses,
        )

    def _fetch_store_artifact(
        self,
        warehouse: _WarehouseEntry,
        *,
        retry_mode: bool = False,
    ) -> AcquisitionArtifact:
        """Fetch store artifact."""
        html = self._fetch_text(
            warehouse.url,
            page_type="store",
            retry_mode=retry_mode,
        )

        return AcquisitionArtifact(
            artifact_type="html",
            source_url=warehouse.url,
            content=html,
            metadata={
                "retrieved_at_utc": self._utc_now(),
                "page_type": "store",
                "state_code": warehouse.state_code,
                "city_slug": warehouse.city_slug,
                "store_id": warehouse.store_id,
                "http_status": 200,
                "scrape_status": "success",
                "retry_mode": retry_mode,
            },
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
                anchor.get("href")
            )

            if not href:
                continue

            absolute_url = urljoin(
                ROOT_URL,
                href,
            )

            path = urlparse(
                absolute_url
            ).path

            match = STATE_PATH_RE.fullmatch(
                path
            )

            if not match:
                continue

            state_code = match.group(
                "state"
            ).upper()

            raw_text = self._clean_text(
                anchor.get_text(
                    " ",
                    strip=True,
                )
            )

            if not raw_text:
                continue

            state_name = re.sub(
                r"\s*\(\s*\d+\s*\)\s*$",
                "",
                raw_text,
            ).strip()

            entries.append(
                _StateEntry(
                    state_code=state_code,
                    state_name=state_name,
                    url=absolute_url,
                    expected_store_count=(
                        self._extract_count(
                            raw_text
                        )
                    ),
                )
            )

        return self._dedupe_state_entries(
            entries
        )

    def _parse_warehouse_entries(
        self,
        html: str,
        *,
        state_code: str,
    ) -> list[_WarehouseEntry]:
        """Parse warehouse entries."""
        soup = BeautifulSoup(
            html or "",
            "html.parser",
        )
        entries: list[
            _WarehouseEntry
        ] = []

        for anchor in soup.select(
            WAREHOUSE_LINK_SELECTOR
        ):
            href = self._clean_text(
                anchor.get("href")
            )

            if not href:
                continue

            absolute_url = urljoin(
                BASE_URL,
                href,
            )

            match = WAREHOUSE_PATH_RE.fullmatch(
                urlparse(
                    absolute_url
                ).path
            )

            if not match:
                continue

            parsed_state = match.group(
                "state"
            ).upper()

            if parsed_state != state_code.upper():
                continue

            entries.append(
                _WarehouseEntry(
                    state_code=parsed_state,
                    city_slug=match.group(
                        "city"
                    ),
                    store_id=match.group(
                        "store_id"
                    ),
                    url=absolute_url,
                )
            )

        return self._dedupe_warehouse_entries(
            entries
        )

    def _parse_store_artifact(
        self,
        artifact: AcquisitionArtifact,
    ) -> dict[str, Any] | None:
        """Parse store artifact."""
        soup = BeautifulSoup(
            artifact.content or "",
            "html.parser",
        )

        address_heading = (
            self._find_heading_by_text(
                soup,
                "Address",
            )
        )

        if address_heading is None:
            return None

        address_container = (
            address_heading.parent
        )

        if address_container is None:
            return None

        address_texts = [
            self._clean_text(
                node.get_text(
                    " ",
                    strip=True,
                )
            )
            for node in address_container.select(
                '[data-testid="Text"]'
            )
        ]

        address_texts = [
            value
            for value in address_texts
            if value
            and value.upper() != "ADDRESS"
        ]

        parsed = (
            self._parse_address(
                address_texts
            )
        )

        phone = self._extract_phone(
            soup
        )

        store_url = artifact.source_url
        parsed_url = urlparse(
            store_url
        )

        store_match = WAREHOUSE_PATH_RE.fullmatch(
            parsed_url.path
        )

        if store_match:
            store_id = store_match.group(
                "store_id"
            )
            city_slug = store_match.group(
                "city"
            )
        else:
            store_id = self._clean_text(
                artifact.metadata.get(
                    "store_id"
                )
            )
            city_slug = self._clean_text(
                artifact.metadata.get(
                    "city_slug"
                )
            )

        store_name = (
            self._extract_store_name(
                soup
            )
        )

        full_address = (
            self._build_full_address(
                parsed["street_address"],
                parsed["city"],
                parsed["state"],
                parsed["zip_code"],
            )
        )

        return {
            "retailer": self.retailer_name,
            "retailer_store_id": store_id,
            "store_number": store_id,
            "store_type": "Warehouse",
            "store_name": store_name,
            "address": parsed["street_address"],
            "street_address": parsed["street_address"],
            "city": parsed["city"],
            "state": parsed["state"],
            "address_city": parsed["city"],
            "address_state": parsed["state"],
            "zip_code": parsed["zip_code"],
            "full_address": full_address,
            "phone": phone,
            "store_url": store_url,
            "source_url": store_url,
            "source_sitemap": ROOT_URL,
            "city_slug": city_slug,
            "extraction_source": (
                "Costco official warehouse detail page"
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

    @staticmethod
    def _parse_address(
        values: Sequence[str | None],
    ) -> dict[str, str | None]:
        """Parse address."""
        clean = [
            value
            for value in values
            if value
        ]

        street_address = (
            clean[0]
            if len(clean) >= 1
            else None
        )

        city = None
        state = None
        zip_code = None

        if len(clean) >= 2:
            match = re.match(
                r"^(?P<city>.+?),\s*"
                r"(?P<state>[A-Za-z]{2})$",
                clean[1],
            )

            if match:
                city = match.group(
                    "city"
                ).strip()
                state = match.group(
                    "state"
                ).upper()

        for value in clean[2:]:
            if ZIP_RE.fullmatch(
                value
            ):
                zip_code = value
                break

        return {
            "street_address": street_address,
            "city": city,
            "state": state,
            "zip_code": zip_code,
        }

    def _extract_store_name(
        self,
        soup: BeautifulSoup,
    ) -> str | None:
        """Extract store name."""
        for selector in (
            "h1",
            "h2",
        ):
            node = soup.select_one(
                selector
            )

            if node is not None:
                text = self._clean_text(
                    node.get_text(
                        " ",
                        strip=True,
                    )
                )

                if (
                    text
                    and "address"
                    not in text.lower()
                ):
                    return text

        title = self._clean_text(
            soup.title.get_text(
                " ",
                strip=True,
            )
            if soup.title
            else None
        )

        if title:
            return re.sub(
                r"\s*\|\s*Costco.*$",
                "",
                title,
                flags=re.IGNORECASE,
            ).strip() or None

        return None

    def _extract_phone(
        self,
        soup: BeautifulSoup,
    ) -> str | None:
        """Extract phone."""
        phone_heading = (
            self._find_heading_by_text(
                soup,
                "Phone:",
            )
        )

        if phone_heading is None:
            return None

        container = (
            phone_heading.parent
        )

        if container is None:
            return None

        link = container.find(
            "a",
            href=re.compile(
                r"^tel:"
            ),
        )

        if link is not None:
            href = self._clean_text(
                link.get("href")
            )

            if (
                href
                and href.startswith(
                    "tel:"
                )
            ):
                return (
                    href[4:].strip()
                    or None
                )

        text = container.get_text(
            " ",
            strip=True,
        )

        match = re.search(
            r"(\(\d{3}\)\s*\d{3}[- ]\d{4})",
            text,
        )

        return (
            match.group(1)
            if match
            else None
        )


    @staticmethod
    def _build_full_address(
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
            locality = f"{locality} {zip_code}"
        elif zip_code:
            locality = zip_code

        parts = [
            part
            for part in (
                street_address,
                locality,
            )
            if part
        ]

        return ", ".join(parts) if parts else None

    @staticmethod
    def _find_heading_by_text(
        soup: BeautifulSoup,
        expected: str,
    ) -> Any | None:
        """Find heading by text."""
        normalized_expected = re.sub(
            r"\s+",
            " ",
            expected.strip().lower(),
        )

        for heading in soup.find_all(
            [
                "h1",
                "h2",
                "h3",
                "h4",
            ]
        ):
            text = re.sub(
                r"\s+",
                " ",
                heading.get_text(
                    " ",
                    strip=True,
                ).lower(),
            )

            if (
                text
                == normalized_expected
            ):
                return heading

        return None

    def _fetch_text(
        self,
        url: str,
        *,
        page_type: str,
        retry_mode: bool = False,
    ) -> str:
        """Fetch text."""
        session = self._get_session()
        last_error: Exception | None = None

        for attempt in range(
            1,
            self.per_request_attempts + 1,
        ):
            try:
                response = session.get(
                    url,
                    timeout=self.request_timeout,
                )

                self._record_response_status(
                    response.status_code
                )

                if response.status_code >= 400:
                    if (
                        response.status_code
                        not in RETRYABLE_STATUS_CODES
                    ):
                        raise RuntimeError(
                            f"HTTP {response.status_code} "
                            f"for Costco {page_type} page: {url}"
                        )

                    body_preview = (
                        response.text[:300]
                        .replace(
                            "\n",
                            " ",
                        )
                    )

                    raise RuntimeError(
                        f"HTTP {response.status_code} "
                        f"for Costco {page_type} page: "
                        f"{url}; body={body_preview!r}"
                    )

                html = response.text

                if not html:
                    raise RuntimeError(
                        f"Empty response body for {url}"
                    )

                return html

            except Exception as exc:
                last_error = exc

                self._record_error_type(
                    exc
                )

                if attempt >= self.per_request_attempts:
                    break

                delay = min(
                    self.retry_backoff_base
                    * (
                        2 ** (
                            attempt - 1
                        )
                    ),
                    self.retry_backoff_max,
                )

                # During the deferred retry pass, use the full backoff;
                # the first pass also gets backoff for transient failures.
                if retry_mode:
                    delay = min(
                        delay * 1.5,
                        self.retry_backoff_max,
                    )

                time.sleep(
                    delay
                )

        raise RuntimeError(
            f"Failed to fetch Costco {page_type} "
            f"page {url} after {self.per_request_attempts} attempts: "
            f"{last_error}"
        ) from last_error

    def _is_deferred_retry_error(
        self,
        exc: Exception,
    ) -> bool:
        """Return whether deferred retry error."""
        message = str(
            exc
        ).lower()

        return any(
            token in message
            for token in (
                "http 403",
                "http 408",
                "http 425",
                "http 429",
                "http 500",
                "http 502",
                "http 503",
                "http 504",
                "timed out",
                "timeout",
                "connection reset",
                "connection aborted",
                "remote disconnected",
                "temporarily unavailable",
            )
        )

    def _record_failed_store(
        self,
        warehouse: _WarehouseEntry,
        exc: Exception,
    ) -> None:
        """Record failed store."""
        self._failed_store_pages.append(
            {
                "state_code": warehouse.state_code,
                "city_slug": warehouse.city_slug,
                "store_id": warehouse.store_id,
                "url": warehouse.url,
                "error": str(exc),
            }
        )

    def _record_response_status(
        self,
        status_code: int,
    ) -> None:
        """Record response status."""
        key = str(
            status_code
        )

        self._request_status_counts[
            key
        ] = (
            self._request_status_counts.get(
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

        self._request_error_type_counts[
            key
        ] = (
            self._request_error_type_counts.get(
                key,
                0,
            )
            + 1
        )

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
                        "text/html,application/xhtml+xml,"
                        "application/xml;q=0.9,image/avif,"
                        "image/webp,*/*;q=0.8"
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

            self._thread_local.session = (
                session
            )

        return session

    @staticmethod
    def _extract_count(
        text: str,
    ) -> int | None:
        """Extract count."""
        match = re.search(
            r"\(\s*(\d+)\s*\)",
            text,
        )

        return (
            int(
                match.group(1)
            )
            if match
            else None
        )

    @staticmethod
    def _dedupe_state_entries(
        entries: Sequence[
            _StateEntry
        ],
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
    def _dedupe_warehouse_entries(
        entries: Sequence[
            _WarehouseEntry
        ],
    ) -> list[
        _WarehouseEntry
    ]:
        """Deduplicate warehouse entries."""
        output: list[
            _WarehouseEntry
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

    def _failed_artifact(
        self,
        *,
        url: str,
        page_type: str,
        error: Exception,
        state_code: str | None = None,
        city_slug: str | None = None,
        store_id: str | None = None,
    ) -> AcquisitionArtifact:
        """Handle failed artifact."""
        return AcquisitionArtifact(
            artifact_type="html",
            source_url=url,
            content="",
            metadata={
                "retrieved_at_utc": (
                    self._utc_now()
                ),
                "page_type": page_type,
                "state_code": state_code,
                "city_slug": city_slug,
                "store_id": store_id,
                "http_status": 500,
                "scrape_status": "failed",
                "error": str(error),
            },
        )

    def _reset_run_state(
        self,
    ) -> None:
        """Reset run state."""
        self._expected_store_count = 0
        self._failed_state_pages = []
        self._failed_store_pages = []
        self._state_count_mismatches = []

        self._request_status_counts = {}
        self._request_error_type_counts = {}
        self._deferred_store_count = 0
        self._retry_success_count = 0

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


class CostcoAcquisitionStrategy(
    CostcoAcquisitionStrategyV2
):
    """Backward-compatible alias for the v1 implementation."""


__all__ = [
    "CostcoAcquisitionStrategyV2",
    "CostcoAcquisitionStrategy",
]