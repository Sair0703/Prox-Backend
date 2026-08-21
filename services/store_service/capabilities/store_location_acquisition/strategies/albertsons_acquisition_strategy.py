from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import re
import threading
import time
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

from services.store_service.capabilities.store_location_acquisition.protocals import (
    AcquisitionArtifact,
    AcquisitionSourceInfo,
    AcquisitionValidationResult,
    StoreLocationAcquisitionStrategy,
)

BASE_URL = "https://local.albertsons.com"
ROOT_URL = f"{BASE_URL}/index.html"

STATE_LINK_SELECTOR = (
    'a.Directory-listLink[data-ya-track="links_directory"]'
)

STORE_TEASER_LINK_SELECTOR = (
    'a.Teaser-titleLink[data-ya-track="storename_directory"]'
)

WEEKLY_AD_SELECTOR = (
    'a[data-ya-track="weeklyad_directory"][href*="storeId="]'
)

STATE_PATH_RE = re.compile(
    r"^/(?P<state>[a-z]{2})\.html$",
    re.IGNORECASE,
)

LOCATION_PATH_RE = re.compile(
    r"^/(?P<state>[a-z]{2})/"
    r"(?P<rest>[^/]+(?:/[^/]+)*)\.html$",
    re.IGNORECASE,
)

US_STATE_CODES = {
    "AZ", "AR", "CA", "CO", "ID", "LA", "MT", "NV",
    "NM", "ND", "OK", "OR", "TX", "UT", "WA", "WY",
}


@dataclass(frozen=True, slots=True)
class _StateEntry:
    """Represent StateEntry data used by the acquisition strategy."""
    state_code: str
    state_name: str
    url: str
    expected_store_count: int | None


@dataclass(frozen=True, slots=True)
class _LocationEntry:
    """Represent LocationEntry data used by the acquisition strategy."""
    url: str
    state_code: str
    kind: str  # "state", "city", "store"


class AlbertsonsAcquisitionStrategyV2(
    StoreLocationAcquisitionStrategy
):
    """Represent AlbertsonsAcquisitionStrategyV2 data used by the acquisition strategy."""
    retailer_key = "albertsons"
    retailer_name = "Albertsons"

    def __init__(
        self,
        *,
        state_workers: int = 16,
        location_workers: int = 16,
        retry_workers: int = 8,
        parse_workers: int = 32,
        request_timeout: int = 30,
        max_retries: int = 5,
        retry_round_delay: float = 5.0,
        retry_backoff_base: float = 1.0,
        retry_backoff_max: float = 16.0,
        debug_failed_limit: int = 25,
    ) -> None:
        """Initialize acquisition configuration and run state."""
        self.state_workers = state_workers
        self.location_workers = location_workers
        self.retry_workers = retry_workers
        self.parse_workers = parse_workers
        self.request_timeout = request_timeout
        self.max_retries = max_retries
        self.retry_round_delay = retry_round_delay
        self.retry_backoff_base = retry_backoff_base
        self.retry_backoff_max = retry_backoff_max
        self.debug_failed_limit = debug_failed_limit

        self._thread_local = threading.local()

        self._expected_store_count = 0
        self._failed_state_pages: list[dict[str, Any]] = []
        self._failed_city_pages: list[dict[str, Any]] = []
        self._failed_store_pages: list[dict[str, Any]] = []

        self._http_status_counts: dict[str, int] = {}
        self._error_type_counts: dict[str, int] = {}
        self._deferred_store_count = 0
        self._retry_success_count = 0

    def discover_source(self) -> AcquisitionSourceInfo:
        """Return metadata describing the retailer's official acquisition source."""
        return AcquisitionSourceInfo(
            retailer_key=self.retailer_key,
            retailer_name=self.retailer_name,
            official_website_url="https://www.albertsons.com/",
            store_locator_url=ROOT_URL,
            endpoint_url=ROOT_URL,
            source_type="html",
            provider="Albertsons official local store directory",
            notes=(
                "Official hierarchy: root -> state -> city/store pages. "
                "City pages expose canonical store URLs, address, phone, "
                "and an official Weekly Ad URL containing storeId. "
                "Single-store pages expose coordinates in Schema.org "
                "meta tags."
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
                "Albertsons root directory returned no state links."
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

        location_entries: dict[str, _LocationEntry] = {}
        direct_store_entries: dict[str, _LocationEntry] = {}
        city_entries: dict[str, _LocationEntry] = {}

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
                desc="Albertsons states",
                unit="state",
            ):
                state = futures[future]

                try:
                    artifact, locations = future.result()
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

                artifacts.append(
                    artifact
                )

                for location in locations:
                    location_entries[
                        location.url
                    ] = location

        for entry in location_entries.values():
            if entry.kind == "city":
                city_entries[entry.url] = entry
            elif entry.kind == "store":
                direct_store_entries[entry.url] = entry

        # Fetch city pages only for true city aggregations.
        city_artifacts: list[AcquisitionArtifact] = []

        if city_entries:
            with ThreadPoolExecutor(
                max_workers=self.location_workers
            ) as pool:
                futures = {
                    pool.submit(
                        self._fetch_city_artifact,
                        entry,
                    ): entry
                    for entry in city_entries.values()
                }

                for future in tqdm(
                    as_completed(futures),
                    total=len(futures),
                    desc="Albertsons cities",
                    unit="city",
                ):
                    entry = futures[future]

                    try:
                        artifact, store_entries = future.result()
                    except Exception as exc:
                        self._failed_city_pages.append(
                            {
                                "state_code": entry.state_code,
                                "url": entry.url,
                                "error": str(exc),
                            }
                        )
                        continue

                    city_artifacts.append(
                        artifact
                    )
                    artifacts.append(
                        artifact
                    )

                    for store_entry in store_entries:
                        direct_store_entries[
                            store_entry.url
                        ] = store_entry

        # Fetch only direct store pages. These are primarily for coordinates
        # and as a fallback to fields missing from city teasers.
        deferred_store_entries: list[_LocationEntry] = []

        if direct_store_entries:
            with ThreadPoolExecutor(
                max_workers=self.location_workers
            ) as pool:
                futures = {
                    pool.submit(
                        self._fetch_store_artifact,
                        entry,
                    ): entry
                    for entry in direct_store_entries.values()
                }

                for future in tqdm(
                    as_completed(futures),
                    total=len(futures),
                    desc="Albertsons stores",
                    unit="store",
                ):
                    entry = futures[future]

                    try:
                        artifact = future.result()
                    except Exception as exc:
                        if self._is_deferred_retry_error(exc):
                            deferred_store_entries.append(entry)
                        else:
                            self._record_failed_store(entry, exc)
                        continue

                    artifacts.append(artifact)

        self._deferred_store_count = len(
            deferred_store_entries
        )

        if deferred_store_entries:
            print(
                "[Albertsons][retry] deferred stores:",
                len(deferred_store_entries),
            )
            time.sleep(
                self.retry_round_delay
            )

            with ThreadPoolExecutor(
                max_workers=self.retry_workers
            ) as pool:
                futures = {
                    pool.submit(
                        self._fetch_store_artifact,
                        entry,
                        retry_mode=True,
                    ): entry
                    for entry in deferred_store_entries
                }

                for future in tqdm(
                    as_completed(futures),
                    total=len(futures),
                    desc="Albertsons deferred retry",
                    unit="store",
                ):
                    entry = futures[future]

                    try:
                        artifact = future.result()
                    except Exception as exc:
                        self._record_failed_store(
                            entry,
                            exc,
                        )
                        continue

                    self._retry_success_count += 1
                    artifacts.append(artifact)

        return artifacts

    def extract_store_payloads(
        self,
        artifacts: Sequence[AcquisitionArtifact],
    ) -> list[dict[str, Any]]:
        """Extract normalized store payloads from acquired artifacts."""
        city_artifacts = [
            artifact
            for artifact in artifacts
            if artifact.metadata.get(
                "page_type"
            ) == "city"
            and artifact.metadata.get(
                "scrape_status"
            ) == "success"
            and artifact.content
        ]

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

        rows_by_url: dict[
            str,
            dict[str, Any],
        ] = {}

        with ThreadPoolExecutor(
            max_workers=self.parse_workers
        ) as pool:
            futures = {
                pool.submit(
                    self._parse_location_artifact,
                    artifact,
                ): artifact
                for artifact in (
                    city_artifacts + store_artifacts
                )
            }

            for future in tqdm(
                as_completed(futures),
                total=len(futures),
                desc="Parsing Albertsons stores",
                unit="store",
            ):
                rows = future.result()

                for row in rows:
                    store_url = self._clean_text(
                        row.get("store_url")
                    )
                    if store_url:
                        existing = rows_by_url.get(
                            store_url
                        )

                        if existing is None:
                            rows_by_url[
                                store_url
                            ] = row
                        else:
                            rows_by_url[
                                store_url
                            ] = self._merge_rows(
                                existing,
                                row,
                            )

        return list(
            rows_by_url.values()
        )

    def validate_store_payloads(
        self,
        payloads: Sequence[Mapping[str, Any]],
    ) -> AcquisitionValidationResult:
        """Validate acquired store payloads for completeness and uniqueness."""
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
        seen_ids: set[str] = set()

        for store_id in store_ids:
            if not store_id:
                continue

            if (
                store_id in seen_ids
                and store_id not in duplicate_store_ids
            ):
                duplicate_store_ids.append(
                    store_id
                )

            seen_ids.add(
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

        if missing_coordinates:
            issue_counts[
                "missing_coordinates"
            ] = missing_coordinates

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

        if self._failed_city_pages:
            issue_counts[
                "failed_city_pages"
            ] = len(
                self._failed_city_pages
            )

        if self._failed_store_pages:
            issue_counts[
                "failed_store_pages"
            ] = len(
                self._failed_store_pages
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
                "Official source hierarchy: root -> state -> "
                "city/store pages."
            ),
            (
                "City pages provide canonical store URLs, address, "
                "phone, and Weekly Ad URLs containing the retailer "
                "storeId."
            ),
            (
                "retailer_store_id is extracted from the official "
                "Albertsons Weekly Ad URL parameter: storeId."
            ),
            (
                "Coordinates are obtained from the official store "
                "detail page via Schema.org latitude/longitude meta tags."
            ),
            (
                "Single-store state/city paths are handled directly; "
                "multi-store city pages are expanded before store detail "
                "enrichment."
            ),
            (
                f"Root-declared store count: "
                f"{self._expected_store_count}"
            ),
            (
                f"Workers: state={self.state_workers}, "
                f"location={self.location_workers}, "
                f"retry={self.retry_workers}, "
                f"parse={self.parse_workers}"
            ),
            (
                f"Deferred stores: {self._deferred_store_count}; "
                f"retry successes: {self._retry_success_count}"
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
            and missing_store_ids == 0
            and not duplicate_store_ids
            and missing_addresses == 0
            and missing_phones == 0
            and missing_coordinates == 0
            and missing_store_urls == 0
            and not self._failed_state_pages
            and not self._failed_city_pages
            and not self._failed_store_pages
            and (
                not self._expected_store_count
                or total_records == self._expected_store_count
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
        """Return acquisition source and execution details for the run summary."""
        return [
            f"Source: {ROOT_URL}",
            "Method: requests + BeautifulSoup",
            (
                "Hierarchy: root -> state pages -> "
                "city/store pages -> store detail enrichment"
            ),
            (
                "Store ID source: official Weekly Ad URL "
                "storeId parameter."
            ),
            (
                "Coordinate source: Schema.org geo meta tags on "
                "official store detail pages."
            ),
            (
                f"Workers: state={self.state_workers}, "
                f"location={self.location_workers}, "
                f"parse={self.parse_workers}"
            ),
        ]

    def _fetch_state_artifact(
        self,
        state: _StateEntry,
    ) -> tuple[
        AcquisitionArtifact,
        list[_LocationEntry],
    ]:
        """Fetch state artifact."""
        html = self._fetch_text(
            state.url,
            page_type="state",
        )

        locations = self._parse_directory_locations(
            html,
            state_code=state.state_code,
        )

        if not locations:
            raise RuntimeError(
                f"No Albertsons locations discovered for "
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
                    "location_count": len(locations),
                    "http_status": 200,
                    "scrape_status": "success",
                },
            ),
            locations,
        )

    def _fetch_city_artifact(
        self,
        entry: _LocationEntry,
    ) -> tuple[
        AcquisitionArtifact,
        list[_LocationEntry],
    ]:
        """Fetch city artifact."""
        html = self._fetch_text(
            entry.url,
            page_type="city",
        )

        store_entries = self._parse_store_entries_from_city(
            html,
            state_code=entry.state_code,
        )

        if not store_entries:
            raise RuntimeError(
                f"No Albertsons store teasers discovered on "
                f"{entry.url}"
            )

        return (
            AcquisitionArtifact(
                artifact_type="html",
                source_url=entry.url,
                content=html,
                metadata={
                    "retrieved_at_utc": self._utc_now(),
                    "page_type": "city",
                    "state_code": entry.state_code,
                    "store_count": len(store_entries),
                    "http_status": 200,
                    "scrape_status": "success",
                },
            ),
            store_entries,
        )

    def _fetch_store_artifact(
        self,
        entry: _LocationEntry,
        *,
        retry_mode: bool = False,
    ) -> AcquisitionArtifact:
        """Fetch store artifact."""
        html = self._fetch_text(
            entry.url,
            page_type="store",
            retry_mode=retry_mode,
        )

        return AcquisitionArtifact(
            artifact_type="html",
            source_url=entry.url,
            content=html,
            metadata={
                "retrieved_at_utc": self._utc_now(),
                "page_type": "store",
                "state_code": entry.state_code,
                "http_status": 200,
                "scrape_status": "success",
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

        entries: list[_StateEntry] = []

        for anchor in soup.select(
            STATE_LINK_SELECTOR
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

            entries.append(
                _StateEntry(
                    state_code=state_code,
                    state_name=state_name,
                    url=absolute_url,
                    expected_store_count=(
                        self._extract_count(
                            anchor.get(
                                "data-count"
                            )
                        )
                    ),
                )
            )

        return self._dedupe_states(
            entries
        )

    def _parse_directory_locations(
        self,
        html: str,
        *,
        state_code: str,
    ) -> list[_LocationEntry]:
        """Parse directory locations."""
        soup = BeautifulSoup(
            html or "",
            "html.parser",
        )

        entries: list[_LocationEntry] = []

        for anchor in soup.select(
            'a.Directory-listLink[data-ya-track="links_directory"]'
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

            path = urlparse(
                absolute_url
            ).path

            match = LOCATION_PATH_RE.fullmatch(
                path
            )

            if not match:
                continue

            parsed_state = match.group(
                "state"
            ).upper()

            if parsed_state != state_code.upper():
                continue

            rest = match.group(
                "rest"
            )

            parts = rest.split("/")

            if len(parts) == 1:
                # <state>/<city>.html
                kind = "city"
            else:
                # <state>/<city>/<address>.html
                kind = "store"

            entries.append(
                _LocationEntry(
                    url=absolute_url,
                    state_code=parsed_state,
                    kind=kind,
                )
            )

        return self._dedupe_locations(
            entries
        )

    def _parse_store_entries_from_city(
        self,
        html: str,
        *,
        state_code: str,
    ) -> list[_LocationEntry]:
        """Parse store entries from city."""
        soup = BeautifulSoup(
            html or "",
            "html.parser",
        )

        entries: list[_LocationEntry] = []

        for anchor in soup.select(
            STORE_TEASER_LINK_SELECTOR
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

            path = urlparse(
                absolute_url
            ).path

            match = LOCATION_PATH_RE.fullmatch(
                path
            )
            if not match:
                continue

            parsed_state = match.group(
                "state"
            ).upper()

            if parsed_state != state_code.upper():
                continue

            rest = match.group(
                "rest"
            )

            if len(rest.split("/")) < 2:
                continue

            entries.append(
                _LocationEntry(
                    url=absolute_url,
                    state_code=parsed_state,
                    kind="store",
                )
            )

        return self._dedupe_locations(
            entries
        )

    def _parse_location_artifact(
        self,
        artifact: AcquisitionArtifact,
    ) -> list[dict[str, Any]]:
        """Parse location artifact."""
        if artifact.metadata.get(
            "page_type"
        ) == "city":
            return self._parse_city_artifact(
                artifact
            )

        if artifact.metadata.get(
            "page_type"
        ) == "store":
            row = self._parse_store_detail_artifact(
                artifact
            )
            return [row] if row else []

        return []

    def _parse_city_artifact(
        self,
        artifact: AcquisitionArtifact,
    ) -> list[dict[str, Any]]:
        """Parse city artifact."""
        soup = BeautifulSoup(
            artifact.content or "",
            "html.parser",
        )

        rows: list[dict[str, Any]] = []

        for article in soup.select(
            "article.Teaser--ace.Teaser--directory"
        ):
            store_link = article.select_one(
                STORE_TEASER_LINK_SELECTOR
            )

            if store_link is None:
                continue

            store_url = self._clean_text(
                store_link.get(
                    "href"
                )
            )
            if not store_url:
                continue

            store_url = urljoin(
                BASE_URL,
                store_url,
            )

            name_node = article.select_one(
                ".LocationName"
            )
            store_name = self._clean_text(
                name_node.get_text(
                    " ",
                    strip=True,
                )
                if name_node is not None
                else None
            )

            street_node = article.select_one(
                ".c-address-street-1"
            )
            street_address = self._clean_text(
                street_node.get_text(
                    " ",
                    strip=True,
                )
                if street_node is not None
                else None
            )

            city_node = article.select_one(
                ".c-address-city"
            )
            city = self._clean_text(
                city_node.get_text(
                    " ",
                    strip=True,
                )
                if city_node is not None
                else None
            )

            state_node = article.select_one(
                ".c-address-state"
            )
            state = self._clean_text(
                state_node.get_text(
                    " ",
                    strip=True,
                )
                if state_node is not None
                else None
            )

            zip_node = article.select_one(
                ".c-address-postal-code"
            )
            zip_code = self._clean_text(
                zip_node.get_text(
                    " ",
                    strip=True,
                )
                if zip_node is not None
                else None
            )

            phone_node = article.select_one(
                ".Phone-display"
            )
            phone = self._clean_text(
                phone_node.get_text(
                    " ",
                    strip=True,
                )
                if phone_node is not None
                else None
            )

            weekly_ad = article.select_one(
                WEEKLY_AD_SELECTOR
            )

            store_id = (
                self._extract_store_id_from_weekly_ad(
                    weekly_ad
                )
            )

            full_address = self._build_full_address(
                street_address=street_address,
                city=city,
                state=state,
                zip_code=zip_code,
            )

            rows.append(
                {
                    "retailer": self.retailer_name,
                    "retailer_store_id": store_id,
                    "store_number": store_id,
                    "store_type": "Grocery",
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
                    "latitude": None,
                    "longitude": None,
                    "store_url": store_url,
                    "source_url": store_url,
                    "source_sitemap": artifact.source_url,
                    "weekly_ad_url": (
                        self._clean_text(
                            weekly_ad.get("href")
                        )
                        if weekly_ad is not None
                        else None
                    ),
                    "extraction_source": (
                        "Albertsons official city directory"
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

    def _parse_store_detail_artifact(
        self,
        artifact: AcquisitionArtifact,
    ) -> dict[str, Any] | None:
        """Parse store detail artifact."""
        soup = BeautifulSoup(
            artifact.content or "",
            "html.parser",
        )

        address = soup.select_one(
            'address#address[itemprop="address"]'
        )

        street_address = self._get_meta_content(
            address,
            "streetAddress",
        )

        city = self._get_meta_content(
            address,
            "addressLocality",
        )

        state = self._get_meta_content(
            address,
            "addressRegion",
        )

        zip_code = self._get_meta_content(
            address,
            "postalCode",
        )

        latitude = self._get_meta_content(
            soup,
            "latitude",
        )

        longitude = self._get_meta_content(
            soup,
            "longitude",
        )

        phone_node = soup.select_one(
            '[itemprop="telephone"]'
        )
        phone = self._clean_text(
            phone_node.get_text(
                " ",
                strip=True,
            )
            if phone_node is not None
            else None
        )

        title_node = soup.select_one(
            ".RedesignHero-titleWrapper"
        )

        store_name = self._clean_text(
            title_node.get_text(
                " ",
                strip=True,
            )
            if title_node is not None
            else None
        )

        store_id = self._extract_store_id_from_soup(
            soup
        )

        full_address = self._build_full_address(
            street_address=street_address,
            city=city,
            state=state,
            zip_code=zip_code,
        )

        return {
            "retailer": self.retailer_name,
            "retailer_store_id": store_id,
            "store_number": store_id,
            "store_type": "Grocery",
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
            "latitude": self._as_float(
                latitude
            ),
            "longitude": self._as_float(
                longitude
            ),
            "store_url": artifact.source_url,
            "source_url": artifact.source_url,
            "source_sitemap": ROOT_URL,
            "weekly_ad_url": self._find_weekly_ad_url(
                soup
            ),
            "extraction_source": (
                "Albertsons official store detail page"
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
    def _find_weekly_ad_url(
        soup: BeautifulSoup,
    ) -> str | None:
        """Find weekly ad url."""
        anchor = soup.select_one(
            'a[href*="set-store.html?storeId="]'
        )

        if anchor is None:
            return None

        return AlbertsonsAcquisitionStrategyV2._clean_text(
            anchor.get("href")
        )

    @staticmethod
    def _extract_store_id_from_weekly_ad(
        anchor: Any,
    ) -> str | None:
        """Extract store id from weekly ad."""
        if anchor is None:
            return None

        href = AlbertsonsAcquisitionStrategyV2._clean_text(
            anchor.get("href")
        )

        if not href:
            return None

        query = parse_qs(
            urlparse(
                href
            ).query
        )

        values = query.get(
            "storeId"
        )

        if not values:
            return None

        value = values[0].strip()

        return value or None

    @classmethod
    def _extract_store_id_from_soup(
        cls,
        soup: BeautifulSoup,
    ) -> str | None:
        """Extract store id from soup."""
        anchor = soup.select_one(
            'a[href*="set-store.html?storeId="]'
        )

        return cls._extract_store_id_from_weekly_ad(
            anchor
        )

    @staticmethod
    def _get_meta_content(
        root: Any,
        itemprop: str,
    ) -> str | None:
        """Return meta content."""
        if root is None:
            return None

        node = root.select_one(
            f'meta[itemprop="{itemprop}"]'
        )

        if node is None:
            return None

        return AlbertsonsAcquisitionStrategyV2._clean_text(
            node.get("content")
        )

    @staticmethod
    def _merge_rows(
        left: Mapping[str, Any],
        right: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Merge rows."""
        merged = dict(left)

        for key, value in right.items():
            if (
                merged.get(key) is None
                or merged.get(key) == ""
            ) and value not in (
                None,
                "",
            ):
                merged[key] = value

        # Store detail data is the preferred enrichment source for
        # coordinates and store ID.
        if right.get("latitude") is not None:
            merged["latitude"] = right["latitude"]

        if right.get("longitude") is not None:
            merged["longitude"] = right["longitude"]

        if right.get("retailer_store_id"):
            merged["retailer_store_id"] = (
                right["retailer_store_id"]
            )
            merged["store_number"] = (
                right["retailer_store_id"]
            )

        return merged

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
                        f"Albertsons {page_type}: {url}"
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

                    if retry_mode:
                        delay = min(
                            delay * 1.5,
                            self.retry_backoff_max,
                        )

                    time.sleep(
                        delay
                    )

        raise RuntimeError(
            f"Failed to fetch Albertsons {page_type} "
            f"{url} after {self.max_retries} attempts: "
            f"{last_error}"
        ) from last_error

    def _is_deferred_retry_error(
        self,
        exc: Exception,
    ) -> bool:
        """Return whether deferred retry error."""
        message = str(exc).lower()
        return any(
            token in message
            for token in (
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
        entry: _LocationEntry,
        exc: Exception,
    ) -> None:
        """Record failed store."""
        self._failed_store_pages.append(
            {
                "state_code": entry.state_code,
                "url": entry.url,
                "error": str(exc),
            }
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

            self._thread_local.session = session

        return session

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
    def _dedupe_states(
        entries: Sequence[_StateEntry],
    ) -> list[_StateEntry]:
        """Deduplicate states."""
        output: list[_StateEntry] = []
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
    def _dedupe_locations(
        entries: Sequence[_LocationEntry],
    ) -> list[_LocationEntry]:
        """Deduplicate locations."""
        output: list[_LocationEntry] = []
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

    @staticmethod
    def _as_float(
        value: Any,
    ) -> float | None:
        """Handle as float."""
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

    def _reset_run_state(
        self,
    ) -> None:
        """Reset run state."""
        self._expected_store_count = 0
        self._failed_state_pages = []
        self._failed_city_pages = []
        self._failed_store_pages = []

        self._http_status_counts = {}
        self._error_type_counts = {}
        self._deferred_store_count = 0
        self._retry_success_count = 0

    @staticmethod
    def _utc_now() -> str:
        """Return the current UTC timestamp in ISO 8601 format."""
        return datetime.now(
            timezone.utc
        ).isoformat()


class AlbertsonsAcquisitionStrategy(
    AlbertsonsAcquisitionStrategyV2
):
    """Backward-compatible alias."""


__all__ = [
    "AlbertsonsAcquisitionStrategyV2",
    "AlbertsonsAcquisitionStrategy",
]