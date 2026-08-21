# services/store_service/capabilities/store_location_acquisition/strategies/piggly_wiggly_acquisition_strategy.py

"""Acquisition strategy for Piggly Wiggly store locations."""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

from services.store_service.capabilities.store_location_acquisition.protocals import (
    AcquisitionArtifact,
    AcquisitionSourceInfo,
    AcquisitionValidationResult,
    StoreLocationAcquisitionStrategy,
)


BASE_URL = "https://www.pigglywiggly.com"
ROOT_URL = f"{BASE_URL}/store-locations/"

STATE_PATH_RE = re.compile(
    r"^/store-locations/(?P<state>[a-z0-9-]+)/?$"
)
STORE_ID_RE = re.compile(
    r"^store-item-(?P<id>[^\s]+)$"
)
LOCALITY_RE = re.compile(
    r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})\s+"
    r"(?P<zip>\d{5}(?:-\d{4})?)$"
)

DEFAULT_HEADERS = {
    "accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
    "accept-language": "en-US,en;q=0.9",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
}


class PigglyWigglyAcquisitionStrategy(
    StoreLocationAcquisitionStrategy
):
    """
    Acquires Piggly Wiggly store locations from the official locator.

    Source hierarchy:
        official root store-location directory
            -> official state directory pages
            -> store cards rendered in HTML

    The store card's ``data-store`` value is treated as the authoritative
    retailer store identifier. External "Go to Website" links are retained
    only as auxiliary information and are not used as canonical store data.
    """

    retailer_key = "piggly_wiggly"
    retailer_name = "Piggly Wiggly"

    def __init__(
        self,
        *,
        state_workers: int = 16,
        parse_workers: int = 32,
        request_timeout: int = 30,
        max_retries: int = 4,
        retry_backoff_base: float = 1.0,
        retry_backoff_max: float = 16.0,
    ) -> None:
        """
        Initialize the Piggly Wiggly acquisition strategy.

        :param state_workers: Maximum concurrent state-page requests.
        :param parse_workers: Maximum concurrent state-page parsing workers.
        :param request_timeout: HTTP request timeout in seconds.
        :param max_retries: Maximum number of attempts for failed requests.
        :param retry_backoff_base: Initial retry delay in seconds.
        :param retry_backoff_max: Maximum retry delay in seconds.
        """
        self.state_workers = state_workers
        self.parse_workers = parse_workers
        self.request_timeout = request_timeout
        self.max_retries = max_retries
        self.retry_backoff_base = retry_backoff_base
        self.retry_backoff_max = retry_backoff_max

        self._thread_local = threading.local()
        self._state_urls: list[str] = []
        self._failed_state_pages: list[dict[str, Any]] = []
        self._request_status_counts: dict[str, int] = {}
        self._request_error_type_counts: dict[str, int] = {}
        self._root_http_status: int | None = None

    def discover_source(self) -> AcquisitionSourceInfo:
        """Describe the official Piggly Wiggly acquisition source."""
        return AcquisitionSourceInfo(
            retailer_key=self.retailer_key,
            retailer_name=self.retailer_name,
            official_website_url=BASE_URL,
            store_locator_url=ROOT_URL,
            endpoint_url=ROOT_URL,
            source_type="html",
            provider="Piggly Wiggly official store locator",
            notes=(
                "The official store locator exposes state directory pages and "
                "store cards directly in HTML. Each store card contains a "
                "data-store identifier, address, phone when available, and a "
                "Google Maps directions link. External Go to Website links are "
                "not treated as authoritative store sources because they may be "
                "stale, shared across stores, or missing."
            ),
        )

    def build_run_notes(self) -> list[str]:
        """Return notes describing the acquisition methodology."""
        return [
            f"Source: {ROOT_URL}",
            "Method: requests + BeautifulSoup",
            "Hierarchy: root store directory -> state pages -> official store cards",
            "Store ID: official store-card data-store attribute",
            "Go to Website links are captured as auxiliary franchise/operator URLs only; they are not followed for acquisition.",
            "Stores without a Go to Website link are still fully acquired from the official Piggly Wiggly locator card.",
            "Coordinates are not exposed in the observed locator HTML and are left empty rather than inferred.",
        ]

    def fetch_raw_artifacts(
        self,
    ) -> list[AcquisitionArtifact]:
        """Fetch the root directory and all discovered state pages."""
        self._reset_run_state()

        root_html = self._fetch_text(ROOT_URL)
        self._root_http_status = 200

        state_urls = self._parse_state_urls(root_html)
        if not state_urls:
            raise RuntimeError(
                "Piggly Wiggly root store directory returned no state links."
            )

        self._state_urls = state_urls

        artifacts: list[AcquisitionArtifact] = [
            AcquisitionArtifact(
                artifact_type="html",
                source_url=ROOT_URL,
                content=root_html,
                metadata={
                    "retrieved_at_utc": self._utc_now(),
                    "page_type": "root",
                    "http_status": self._root_http_status,
                    "scrape_status": "success",
                    "state_count": len(state_urls),
                },
            )
        ]

        state_results: dict[str, str] = {}

        with ThreadPoolExecutor(
            max_workers=self.state_workers,
        ) as pool:
            future_to_url = {
                pool.submit(
                    self._fetch_text,
                    url,
                ): url
                for url in state_urls
            }

            for future in tqdm(
                as_completed(future_to_url),
                total=len(future_to_url),
                desc="Piggly Wiggly state pages",
                unit="state",
            ):
                url = future_to_url[future]

                try:
                    state_results[url] = future.result()
                except Exception as exc:
                    self._failed_state_pages.append(
                        {
                            "url": url,
                            "error": str(exc),
                        }
                    )

        for url in state_urls:
            html = state_results.get(url)

            if html is None:
                continue

            artifacts.append(
                AcquisitionArtifact(
                    artifact_type="html",
                    source_url=url,
                    content=html,
                    metadata={
                        "retrieved_at_utc": self._utc_now(),
                        "page_type": "state",
                        "http_status": 200,
                        "scrape_status": "success",
                    },
                )
            )

        return artifacts

    def extract_store_payloads(
        self,
        artifacts: Sequence[AcquisitionArtifact],
    ) -> list[dict[str, Any]]:
        """
        Parse store records from successfully fetched state pages.

        :param artifacts: Raw acquisition artifacts produced by the fetch stage.
        :return: Normalized store payloads keyed by official store ID.
        """
        state_artifacts = [
            artifact
            for artifact in artifacts
            if artifact.metadata.get("page_type") == "state"
            and artifact.metadata.get("scrape_status") == "success"
            and artifact.content
        ]

        rows: list[dict[str, Any]] = []

        with ThreadPoolExecutor(
            max_workers=self.parse_workers,
        ) as pool:
            futures = [
                pool.submit(
                    self._parse_state_artifact,
                    artifact,
                )
                for artifact in state_artifacts
            ]

            for future in tqdm(
                as_completed(futures),
                total=len(futures),
                desc="Parsing Piggly Wiggly stores",
                unit="state",
            ):
                rows.extend(future.result())

        return self._dedupe_by_store_id(rows)

    def validate_store_payloads(
        self,
        payloads: Sequence[Mapping[str, Any]],
    ) -> AcquisitionValidationResult:
        """
        Validate identifiers, address completeness, and acquisition coverage.

        :param payloads: Store payloads produced by the extraction stage.
        :return: Validation result containing quality metrics and run notes.
        """
        total_records = len(payloads)

        store_ids = [
            self._clean_text(row.get("retailer_store_id"))
            for row in payloads
        ]
        unique_store_ids = len(
            {value for value in store_ids if value}
        )
        missing_store_ids = sum(
            not value
            for value in store_ids
        )

        duplicate_store_ids = self._find_duplicates(store_ids)

        missing_addresses = sum(
            not self._clean_text(row.get("address"))
            or not self._clean_text(row.get("city"))
            or not self._clean_text(row.get("state"))
            or not self._clean_text(row.get("zip_code"))
            for row in payloads
        )

        missing_phones = sum(
            not self._clean_text(row.get("phone"))
            for row in payloads
        )

        missing_coordinates = sum(
            row.get("latitude") is None
            or row.get("longitude") is None
            for row in payloads
        )

        duplicate_address_groups = (
            self._find_duplicate_address_groups(payloads)
        )
        issue_counts: dict[str, int] = {}

        if missing_store_ids:
            issue_counts["missing_store_ids"] = missing_store_ids
        if duplicate_store_ids:
            issue_counts["duplicate_store_ids"] = len(
                duplicate_store_ids
            )
        if missing_addresses:
            issue_counts["missing_addresses"] = missing_addresses
        if missing_phones:
            issue_counts["missing_phones"] = missing_phones
        if missing_coordinates:
            issue_counts["missing_coordinates"] = missing_coordinates
        if duplicate_address_groups:
            issue_counts["duplicate_address_groups"] = len(
                duplicate_address_groups
            )
        if self._failed_state_pages:
            issue_counts["failed_state_pages"] = len(
                self._failed_state_pages
            )

        notes = [
            "Official source: Piggly Wiggly store locator and state directory pages.",
            "The official store-card data-store attribute is used as retailer_store_id.",
            "External Go to Website links are auxiliary only and are not followed for store acquisition.",
            "Some stores have no external website link; this does not make the official store record incomplete.",
            "Coordinates are not exposed in the observed locator HTML and are left empty.",
            f"State pages discovered: {len(self._state_urls)}",
            (
                "State pages successfully fetched: "
                f"{len(self._state_urls) - len(self._failed_state_pages)}"
            ),
        ]

        if duplicate_address_groups:
            notes.append(
                "Duplicate address groups were retained when they have distinct official retailer store IDs."
            )

        if self._failed_state_pages:
            notes.append(
                "Some official state pages failed to fetch; the dataset should be considered partial until those pages succeed."
            )

        notes.append(
            "No store detail-page traversal is required because the locator cards already expose the authoritative location fields needed for acquisition."
        )

        is_valid = (
            total_records > 0
            and unique_store_ids == total_records
            and missing_store_ids == 0
            and missing_addresses == 0
            and not duplicate_store_ids
            and not self._failed_state_pages
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