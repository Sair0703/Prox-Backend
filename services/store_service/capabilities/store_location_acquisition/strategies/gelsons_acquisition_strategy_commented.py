from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin

import re
import requests
from bs4 import BeautifulSoup

from services.store_service.capabilities.store_location_acquisition.protocals import (
    AcquisitionArtifact,
    AcquisitionSourceInfo,
    AcquisitionValidationResult,
    StoreLocationAcquisitionStrategy,
)

ROOT_URL = "https://www.gelsons.com/stores"
BASE_URL = "https://www.gelsons.com"

STORE_ITEM_SELECTOR = 'ol > li[id], li[id]'
DETAIL_LINK_SELECTOR = 'a[aria-label="See store details"], a[href^="/stores/"]'
ADDRESS_LINK_SELECTOR = 'a[aria-label^="store address"]'
PHONE_LINK_SELECTOR = 'a[href^="tel:"]'


class GelsonsAcquisitionStrategyV2(
    StoreLocationAcquisitionStrategy
):
    """Represent GelsonsAcquisitionStrategyV2 used by the acquisition workflow."""
    retailer_key = "gelsons"
    retailer_name = "Gelson's"

    def __init__(
        self,
        *,
        request_timeout: int = 30,
        expected_store_count: int = 28,
    ) -> None:
        """Initialize acquisition configuration and run state."""
        self.request_timeout = request_timeout
        self.expected_store_count = expected_store_count

        self._http_status: int | None = None
        self._request_error: str | None = None

    def discover_source(self) -> AcquisitionSourceInfo:
        """Return metadata describing the retailer's official acquisition source."""
        return AcquisitionSourceInfo(
            retailer_key=self.retailer_key,
            retailer_name=self.retailer_name,
            official_website_url="https://www.gelsons.com/",
            store_locator_url=ROOT_URL,
            endpoint_url=ROOT_URL,
            source_type="html",
            provider="Gelson's official store locator",
            notes=(
                "The official /stores page exposes the complete current store list "
                "directly in server-rendered HTML. Each store entry contains store "
                "name, address, phone, and canonical store detail URL. A dedicated "
                "retailer store ID is not exposed in the observed HTML, so no ID is "
                "inferred from the numeric list item or URL slug."
            ),
        )

    def build_run_notes(self) -> list[str]:
        """Return acquisition source and execution details for the run summary."""
        return [
            f"Source: {ROOT_URL}",
            "Method: requests + BeautifulSoup",
            "Hierarchy: single store locator HTML page -> 28 store entries with structural selector fallback",
            "Store detail pages are not required for the observed Store Info fields.",
            "retailer_store_id/store_number left empty because no authoritative store ID was observed.",
            f"Expected current store count: {self.expected_store_count}",
        ]

    def fetch_raw_artifacts(self) -> list[AcquisitionArtifact]:
        """Fetch raw artifacts required for store location acquisition."""
        self._http_status = None
        self._request_error = None

        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/151.0.0.0 Safari/537.36"
            ),
        }

        try:
            response = requests.get(
                ROOT_URL,
                headers=headers,
                timeout=self.request_timeout,
            )
            self._http_status = response.status_code
            response.raise_for_status()
            html = response.text
        except Exception as exc:
            self._request_error = str(exc)
            raise RuntimeError(
                f"Failed to fetch Gelson's store locator {ROOT_URL}: {exc}"
            ) from exc

        return [
            AcquisitionArtifact(
                artifact_type="html",
                source_url=ROOT_URL,
                content=html,
                metadata={
                    "retrieved_at_utc": self._utc_now(),
                    "page_type": "store_locator",
                    "http_status": self._http_status,
                    "scrape_status": "success",
                },
            )
        ]

    def extract_store_payloads(
        self,
        artifacts: Sequence[AcquisitionArtifact],
    ) -> list[dict[str, Any]]:
        """Extract normalized store payloads from acquired artifacts."""
        payloads: list[dict[str, Any]] = []

        for artifact in artifacts:
            if artifact.artifact_type != "html":
                continue

            soup = BeautifulSoup(
                artifact.content or "",
                "html.parser",
            )

            all_li_count = len(soup.find_all("li"))
            item_count_with_id = len(soup.select("li[id]"))
            items = soup.select(STORE_ITEM_SELECTOR)

            print(
                "[Gelsons][parse] "
                f"HTML length={len(artifact.content or '')} "
                f"all_li={all_li_count} "
                f"li_with_id={item_count_with_id} "
                f"store_items={len(items)}"
            )

            if not items:
                # Last-resort structural fallback based on the observed
                # locator markup: list items containing a /stores/... link.
                candidates = []
                for candidate in soup.find_all("li"):
                    if candidate.select_one('a[href^="/stores/"]'):
                        candidates.append(candidate)
                items = candidates

                print(
                    "[Gelsons][parse] fallback store items:",
                    len(items),
                )

            for item in items:
                payload = self._parse_store_item(
                    item=item,
                    source_url=artifact.source_url,
                    http_status=artifact.metadata.get("http_status"),
                )
                if payload is not None:
                    payloads.append(payload)

        return self._dedupe_payloads(payloads)

    def validate_store_payloads(
        self,
        payloads: Sequence[Mapping[str, Any]],
    ) -> AcquisitionValidationResult:
        """Validate acquired store payloads for completeness and uniqueness."""
        total_records = len(payloads)
        store_urls = [
            self._clean_text(row.get("store_url"))
            for row in payloads
        ]
        unique_store_urls = len({url for url in store_urls if url})

        missing_store_ids = sum(
            1
            for row in payloads
            if not self._clean_text(row.get("retailer_store_id"))
        )

        missing_coordinates = sum(
            1
            for row in payloads
            if row.get("latitude") is None
            or row.get("longitude") is None
        )

        missing_addresses = sum(
            1
            for row in payloads
            if not self._clean_text(row.get("address"))
            or not self._clean_text(row.get("city"))
            or not self._clean_text(row.get("state"))
            or not self._clean_text(row.get("zip_code"))
        )

        missing_phones = sum(
            1
            for row in payloads
            if not self._clean_text(row.get("phone"))
        )

        missing_store_urls = sum(
            1
            for row in payloads
            if not self._clean_text(row.get("store_url"))
        )

        duplicate_store_urls = self._find_duplicates(
            store_urls
        )

        issue_counts: dict[str, int] = {}

        if total_records != self.expected_store_count:
            issue_counts["declared_store_count_mismatch"] = 1
            issue_counts["parsed_store_count"] = total_records
        if duplicate_store_urls:
            issue_counts["duplicate_store_urls"] = len(duplicate_store_urls)
        if missing_addresses:
            issue_counts["missing_addresses"] = missing_addresses
        if missing_phones:
            issue_counts["missing_phones"] = missing_phones
        if missing_store_ids:
            issue_counts["missing_store_ids"] = missing_store_ids
        if missing_coordinates:
            issue_counts["missing_coordinates"] = missing_coordinates
        if missing_store_urls:
            issue_counts["missing_store_urls"] = missing_store_urls
        if self._request_error:
            issue_counts["request_error"] = 1

        notes = [
            "Official source: Gelson's store locator HTML.",
            "All currently observed stores are exposed directly on the /stores page.",
            "Store name, address, phone, and canonical detail URL are parsed from each HTML list item.",
            "The numeric list-item prefix is treated only as a display/list index, not as a retailer store ID.",
            "No dedicated retailer store ID was observed in the supplied HTML, so retailer_store_id/store_number are left empty.",
            "Coordinates are not exposed in the supplied locator HTML; no geocoding is performed in v2.",
            f"Expected current store count: {self.expected_store_count}",
        ]

        if self._http_status is not None:
            notes.append(f"HTTP status: {self._http_status}")

        is_valid = (
            total_records == self.expected_store_count
            and unique_store_urls == total_records
            and missing_addresses == 0
            and missing_phones == 0
            and missing_store_urls == 0
            and not duplicate_store_urls
            and not self._request_error
        )

        return AcquisitionValidationResult(
            is_valid=is_valid,
            total_records=total_records,
            unique_store_ids=unique_store_urls,
            missing_store_ids=missing_store_ids,
            missing_coordinates=missing_coordinates,
            non_us_records=0,
            duplicate_store_ids=duplicate_store_urls,
            issue_counts=issue_counts,
            notes=notes,
        )

    @classmethod
    def _parse_store_item(
        cls,
        *,
        item: Any,
        source_url: str,
        http_status: int | None,
    ) -> dict[str, Any] | None:
        """Parse store item."""
        name_node = (
            item.select_one("button.flex.font-bold.text-lg")
            or item.select_one("button.font-bold.text-lg")
            or item.find("button")
        )
        store_name = cls._clean_text(
            name_node.get_text(" ", strip=True)
            if name_node is not None
            else None
        )

        address_link = item.select_one(
            ADDRESS_LINK_SELECTOR
        )
        if address_link is None:
            return None

        address_lines = [
            cls._clean_text(line)
            for line in address_link.stripped_strings
            if cls._clean_text(line)
        ]

        street_address = address_lines[0] if address_lines else None
        city = None
        state = None
        zip_code = None

        if len(address_lines) >= 2:
            city, state, zip_code = cls._parse_locality_line(
                address_lines[1]
            )

        full_address = cls._build_full_address(
            street_address=street_address,
            city=city,
            state=state,
            zip_code=zip_code,
        )

        phone_link = item.select_one(
            PHONE_LINK_SELECTOR
        )
        phone = cls._clean_text(
            phone_link.get_text(" ", strip=True)
            if phone_link is not None
            else None
        )

        detail_link = item.select_one(
            DETAIL_LINK_SELECTOR
        )
        store_url = (
            urljoin(BASE_URL, detail_link.get("href"))
            if detail_link is not None and detail_link.get("href")
            else None
        )

        is_closed = bool(
            store_name
            and re.search(r"\bCLOSED\b", store_name, re.IGNORECASE)
        )

        return {
            "retailer": cls.retailer_name,
            "retailer_store_id": None,
            "store_number": None,
            "store_type": "Regular",
            "store_name": store_name,
            "address": street_address,
            "street_address": street_address,
            "city": city,
            "state": state,
            "zip_code": zip_code,
            "full_address": full_address,
            "phone": phone,
            "latitude": None,
            "longitude": None,
            "store_url": store_url,
            "source_url": source_url,
            "source_sitemap": source_url,
            "extraction_source": "Gelson's official store locator HTML",
            "scrape_status": "success",
            "http_status": http_status,
            "error_message": None,
            "scraped_at_utc": cls._utc_now(),
            "is_closed": is_closed,
        }

    @staticmethod
    def _parse_locality_line(
        line: str,
    ) -> tuple[str | None, str | None, str | None]:
        """Parse locality line."""
        text = line.strip()
        match = re.match(
            r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$",
            text,
        )
        if not match:
            return None, None, None

        return (
            match.group("city").strip(),
            match.group("state").strip(),
            match.group("zip").strip(),
        )

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
            locality = f"{locality} {zip_code}"
        elif zip_code:
            locality = zip_code

        parts = [
            part
            for part in (street_address, locality)
            if part
        ]
        return ", ".join(parts) if parts else None

    @staticmethod
    def _dedupe_payloads(
        payloads: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Deduplicate payloads."""
        output: list[dict[str, Any]] = []
        seen: set[str] = set()

        for payload in payloads:
            key = (
                GelsonsAcquisitionStrategyV2._clean_text(
                    payload.get("store_url")
                )
                or GelsonsAcquisitionStrategyV2._clean_text(
                    payload.get("full_address")
                )
                or GelsonsAcquisitionStrategyV2._clean_text(
                    payload.get("store_name")
                )
            )
            if not key or key in seen:
                continue

            seen.add(key)
            output.append(dict(payload))

        return output

    @staticmethod
    def _find_duplicates(
        values: Sequence[str | None],
    ) -> list[str]:
        """Find duplicates."""
        seen: set[str] = set()
        duplicates: list[str] = []

        for value in values:
            if not value:
                continue
            if value in seen and value not in duplicates:
                duplicates.append(value)
            seen.add(value)

        return duplicates

    @staticmethod
    def _clean_text(value: Any) -> str | None:
        """Normalize text."""
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _utc_now() -> str:
        """Return the current UTC timestamp in ISO 8601 format."""
        return datetime.now(timezone.utc).isoformat()


class GelsonsAcquisitionStrategy(GelsonsAcquisitionStrategyV2):
    """Backward-compatible alias."""


__all__ = [
    "GelsonsAcquisitionStrategyV2",
    "GelsonsAcquisitionStrategy",
]