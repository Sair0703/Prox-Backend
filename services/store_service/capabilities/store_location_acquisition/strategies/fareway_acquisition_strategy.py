from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse
import re
import time

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm


ROOT_URL = "https://www.fareway.com/stores/page/1"
BASE_URL = "https://www.fareway.com"

RETAILER = "Fareway"
RETAILER_KEY = "fareway"

TOTAL_PAGES = 18

WORKERS = 8
REQUEST_TIMEOUT = 30
MAX_RETRIES = 4
BACKOFFS = (1.0, 2.0, 4.0, 8.0)

STORE_PATH_RE = re.compile(
    r"^/stores/(?P<store_id>\d+)/?$",
    re.IGNORECASE,
)


@dataclass(slots=True)
class FetchResult:
    """Represent FetchResult used by the acquisition workflow."""
    url: str
    html: str | None
    status_code: int | None
    error: str | None = None
    attempts: int = 0


class FarewayAcquisitionStrategy:
    """
    Fareway official store-directory acquisition.

    Observed source:
        https://www.fareway.com/stores/page/1
        ...
        https://www.fareway.com/stores/page/18

    Each page contains store cards with all required acquisition fields:
        - official store detail href: /stores/<store_id>
        - store number / name
        - address
        - phone
        - latitude / longitude on the card element

    Store ID:
        Numeric ID from the official card link:
            <a href="/stores/933">#933 ...</a>

    Detail pages are not required because the supplied store cards already
    expose the required authoritative location fields.
    """

    retailer = RETAILER
    retailer_key = RETAILER_KEY
    source_type = "html"
    provider = "Fareway official store directory"

    def __init__(
        self,
        *,
        workers: int = WORKERS,
        total_pages: int = TOTAL_PAGES,
    ) -> None:
        """Initialize acquisition configuration and run state."""
        self.workers = max(1, workers)
        self.total_pages = total_pages

        self.failed_urls: list[dict[str, Any]] = []
        self.page_results: list[FetchResult] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self) -> dict[str, Any]:
        """Run the complete store location acquisition workflow."""
        page_urls = self._build_page_urls()

        self._print_header(
            page_urls
        )

        page_results = self._fetch_many(
            page_urls
        )

        records_by_store_id: dict[
            str,
            dict[str, Any],
        ] = {}

        raw_records = 0
        parse_failures = 0

        for result in page_results:
            if result.error or not result.html:
                continue

            records = self._parse_page(
                result.html,
                result.url,
            )

            if records is None:
                parse_failures += 1
                self.failed_urls.append(
                    {
                        "url": result.url,
                        "stage": "page_parse",
                        "error": "No valid Fareway store-card data found",
                    }
                )
                continue

            raw_records += len(records)

            for record in records:
                store_id = record[
                    "retailer_store_id"
                ]

                existing = records_by_store_id.get(
                    store_id
                )

                if existing is None:
                    records_by_store_id[
                        store_id
                    ] = record
                else:
                    records_by_store_id[
                        store_id
                    ] = self._merge_records(
                        existing,
                        record,
                    )

        records = sorted(
            records_by_store_id.values(),
            key=self._store_sort_key,
        )

        validation = self._validate(
            records=records,
            raw_records=raw_records,
            parse_failures=parse_failures,
            page_count=len(page_urls),
        )

        return {
            "retailer": self.retailer,
            "retailer_key": self.retailer_key,
            "source_type": self.source_type,
            "provider": self.provider,
            "records": records,
            "validation": validation,
            "page_urls": page_urls,
            "failed_urls": self.failed_urls,
            "notes": self._build_notes(),
        }

    # ------------------------------------------------------------------
    # Page acquisition
    # ------------------------------------------------------------------

    def _build_page_urls(self) -> list[str]:
        """Build page urls."""
        return [
            f"{BASE_URL}/stores/page/{page}"
            for page in range(
                1,
                self.total_pages + 1,
            )
        ]

    def _fetch_many(
        self,
        urls: list[str],
    ) -> list[FetchResult]:
        """Fetch many."""
        results: list[FetchResult] = []

        with ThreadPoolExecutor(
            max_workers=self.workers
        ) as executor:
            futures = {
                executor.submit(
                    self._fetch_url,
                    url,
                ): url
                for url in urls
            }

            for future in tqdm(
                as_completed(futures),
                total=len(futures),
                desc="Fareway store pages",
                unit="page",
            ):
                result = future.result()
                results.append(result)
                self.page_results.append(result)

                if result.error:
                    self.failed_urls.append(
                        {
                            "url": result.url,
                            "stage": "page_fetch",
                            "status_code": result.status_code,
                            "error": result.error,
                            "attempts": result.attempts,
                        }
                    )

        return results

    @staticmethod
    def _fetch_url(
        url: str,
    ) -> FetchResult:
        """Fetch url."""
        session = requests.Session()
        session.headers.update(
            {
                "Accept": (
                    "text/html,application/xhtml+xml,"
                    "application/xml;q=0.9,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/151.0.0.0 Safari/537.36"
                ),
            }
        )

        last_error: str | None = None
        last_status: int | None = None

        for attempt in range(
            1,
            MAX_RETRIES + 1,
        ):
            try:
                if attempt > 1:
                    time.sleep(
                        BACKOFFS[
                            min(
                                attempt - 2,
                                len(BACKOFFS) - 1,
                            )
                        ]
                    )

                response = session.get(
                    url,
                    timeout=REQUEST_TIMEOUT,
                )

                last_status = response.status_code

                if response.status_code != 200:
                    last_error = (
                        f"HTTP {response.status_code}"
                    )
                    continue

                if not response.text.strip():
                    last_error = "Empty response body"
                    continue

                return FetchResult(
                    url=url,
                    html=response.text,
                    status_code=200,
                    attempts=attempt,
                )

            except requests.RequestException as exc:
                last_error = repr(exc)

        return FetchResult(
            url=url,
            html=None,
            status_code=last_status,
            error=last_error,
            attempts=MAX_RETRIES,
        )

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_page(
        self,
        html: str,
        url: str,
    ) -> list[dict[str, Any]] | None:
        """Parse page."""
        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        cards = soup.select(
            "div.card.store"
        )

        if not cards:
            return None

        records: list[dict[str, Any]] = []

        for card in cards:
            record = self._parse_card(
                card,
                url,
            )

            if record is not None:
                records.append(
                    record
                )

        return records

    def _parse_card(
        self,
        card: Any,
        source_page_url: str,
    ) -> dict[str, Any] | None:
        """Parse card."""
        store_link = card.select_one(
            "h3.card-title a[href]"
        )

        if store_link is None:
            store_link = card.select_one(
                "a[href^='/stores/']"
            )

        if store_link is None:
            return None

        href = store_link.get(
            "href"
        )

        store_id = self._extract_store_id(
            href
        )

        if not store_id:
            return None

        title_text = store_link.get_text(
            " ",
            strip=True,
        )

        store_name = (
            self._parse_store_name(
                title_text
            )
        )

        address_link = card.select_one(
            ".card-subtitle a[href*='google.com/maps']"
        )

        if address_link is None:
            address_link = card.select_one(
                ".card-subtitle a"
            )

        address = None

        if address_link:
            address = self._clean_text(
                address_link.get_text(
                    " ",
                    strip=True,
                )
            )

        phone_link = card.select_one(
            ".store-phone a[href^='tel:']"
        )

        phone = (
            self._clean_text(
                phone_link.get_text(
                    " ",
                    strip=True,
                )
            )
            if phone_link
            else None
        )

        latitude = self._parse_float(
            card.get(
                "data-latitude"
            )
        )

        longitude = self._parse_float(
            card.get(
                "data-longitude"
            )
        )

        city, state, zip_code = (
            self._parse_city_state_zip(
                title_text,
                address,
            )
        )

        return {
            "retailer": RETAILER,
            "retailer_key": RETAILER_KEY,
            "retailer_store_id": store_id,
            "store_number": store_id,
            "store_name": store_name,
            "address": self._extract_street_address(
                address
            ),
            "city": city,
            "state": state,
            "zip_code": zip_code,
            "full_address": address,
            "phone": phone,
            "latitude": latitude,
            "longitude": longitude,
            "store_url": self._canonical_url(
                urljoin(
                    source_page_url,
                    href,
                )
            ),
            "source": (
                "Fareway official store directory"
            ),
            "source_type": "html",
        }

    @staticmethod
    def _extract_store_id(
        href: str | None,
    ) -> str | None:
        """Extract store id."""
        if not href:
            return None

        path = urlparse(href).path

        match = re.match(
            r"^/stores/(\d+)/?$",
            path,
            flags=re.IGNORECASE,
        )

        return (
            match.group(1)
            if match
            else None
        )

    @staticmethod
    def _parse_store_name(
        title_text: str,
    ) -> str | None:
        # Example:
        # "#933 URBANDALE, IA"
        """Parse store name."""
        text = re.sub(
            r"^#\d+\s*",
            "",
            title_text,
            flags=re.IGNORECASE,
        )

        return (
            text.strip()
            if text.strip()
            else None
        )

    @staticmethod
    def _parse_city_state_zip(
        title_text: str,
        full_address: str | None,
    ) -> tuple[str | None, str | None, str | None]:
        """Parse city state zip."""
        state = None
        city = None
        zip_code = None

        if title_text:
            match = re.search(
                r"#\d+\s+(.+?),\s*([A-Z]{2})$",
                title_text.strip(),
            )

            if match:
                city = match.group(1).strip()
                state = match.group(2).strip()

        if full_address:
            zip_match = re.search(
                r"\b(\d{5})(?:-\d{4})?\s*$",
                full_address,
            )

            if zip_match:
                zip_code = zip_match.group(1)

        return city, state, zip_code

    @staticmethod
    def _extract_street_address(
        full_address: str | None,
    ) -> str | None:
        """Extract street address."""
        if not full_address:
            return None

        # Card address format:
        # 8450 Meredith Drive, URBANDALE, IA 50322
        match = re.match(
            r"^\s*(.+?),\s*[^,]+,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\s*$",
            full_address,
            flags=re.IGNORECASE,
        )

        if match:
            return match.group(1).strip()

        return full_address.strip()

    @staticmethod
    def _clean_text(
        value: str | None,
    ) -> str | None:
        """Normalize text."""
        if value is None:
            return None

        cleaned = re.sub(
            r"\s+",
            " ",
            value,
        ).strip()

        return cleaned or None

    @staticmethod
    def _parse_float(
        value: str | None,
    ) -> float | None:
        """Parse float."""
        if value is None:
            return None

        try:
            return float(value)
        except (
            TypeError,
            ValueError,
        ):
            return None

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate(
        self,
        *,
        records: list[dict[str, Any]],
        raw_records: int,
        parse_failures: int,
        page_count: int,
    ) -> dict[str, Any]:
        """Handle validate."""
        ids = [
            record.get(
                "retailer_store_id"
            )
            for record in records
            if record.get(
                "retailer_store_id"
            )
        ]

        missing_ids = sum(
            not record.get(
                "retailer_store_id"
            )
            for record in records
        )

        missing_addresses = sum(
            not record.get(
                "full_address"
            )
            for record in records
        )

        missing_phones = sum(
            not record.get(
                "phone"
            )
            for record in records
        )

        missing_coordinates = sum(
            record.get("latitude") is None
            or record.get("longitude") is None
            for record in records
        )

        state_counts: dict[str, int] = {}

        for record in records:
            state = (
                record.get(
                    "state"
                )
                or "UNKNOWN"
            )

            state_counts[state] = (
                state_counts.get(
                    state,
                    0,
                )
                + 1
            )

        duplicate_records = (
            raw_records
            - len(records)
        )

        issues: list[str] = []

        if missing_ids:
            issues.append(
                "missing_store_ids"
            )

        if missing_addresses:
            issues.append(
                "missing_addresses"
            )

        if missing_coordinates:
            issues.append(
                "missing_coordinates"
            )

        if parse_failures:
            issues.append(
                "page_parse_failures"
            )

        if self.failed_urls:
            issues.append(
                "failed_urls"
            )

        return {
            "valid": not issues,
            "total_records": len(records),
            "unique_store_ids": len(
                set(ids)
            ),
            "raw_records": raw_records,
            "duplicate_records_merged": duplicate_records,
            "missing_store_ids": missing_ids,
            "missing_addresses": missing_addresses,
            "missing_phones": missing_phones,
            "missing_coordinates": missing_coordinates,
            "page_count": page_count,
            "successful_pages": (
                page_count
                - len(
                    {
                        item["url"]
                        for item in self.failed_urls
                        if item["stage"]
                        == "page_fetch"
                    }
                )
            ),
            "parse_failures": parse_failures,
            "failed_urls": len(
                self.failed_urls
            ),
            "state_counts": state_counts,
            "issues": issues,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _canonical_url(
        url: str,
    ) -> str:
        """Return canonical url."""
        parsed = urlparse(
            url
        )

        return (
            f"{parsed.scheme}://"
            f"{parsed.netloc}"
            f"{parsed.path.rstrip('/')}"
        )

    @staticmethod
    def _store_sort_key(
        record: dict[str, Any],
    ) -> tuple[int, str]:
        """Handle store sort key."""
        value = str(
            record.get(
                "retailer_store_id",
                "",
            )
        )

        if value.isdigit():
            return (
                0,
                f"{int(value):010d}",
            )

        return (
            1,
            value,
        )

    @staticmethod
    def _merge_records(
        first: dict[str, Any],
        second: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge records."""
        merged = dict(first)

        for key, value in second.items():
            if (
                merged.get(key)
                in (None, "")
                and value
                not in (None, "")
            ):
                merged[key] = value

        return merged

    @staticmethod
    def _print_header(
        page_urls: list[str],
    ) -> None:
        """Print header."""
        print("=" * 72)
        print(
            "Fareway Acquisition Strategy v1"
        )
        print("=" * 72)
        print(
            f"Source: {BASE_URL}/stores/page/1"
        )
        print(
            "Method: requests + BeautifulSoup"
        )
        print(
            "Hierarchy: paginated store directory -> store cards"
        )
        print(
            "Store ID: official /stores/<store_id> href"
        )
        print(
            "Coordinates: official card data-latitude/data-longitude"
        )
        print(
            f"Pages: {len(page_urls)}"
        )
        print(
            f"Workers: {WORKERS}"
        )
        print(
            f"Retry: max={MAX_RETRIES}, "
            f"backoff={BACKOFFS}"
        )
        print(
            "Store detail traversal: not required; "
            "store cards contain required fields"
        )
        print()

    @staticmethod
    def _build_notes() -> list[str]:
        """Build notes."""
        return [
            (
                "Official source: Fareway paginated store directory."
            ),
            (
                "The acquisition traverses all 18 official store pages."
            ),
            (
                "Each store card exposes the official store link "
                "/stores/<store_id>; the numeric suffix is used as "
                "retailer_store_id/store_number."
            ),
            (
                "Store cards directly expose store name, address, phone, "
                "latitude, and longitude."
            ),
            (
                "No store detail-page traversal is required."
            ),
            (
                "Records are merged and deduplicated by retailer_store_id."
            ),
        ]


if __name__ == "__main__":
    result = FarewayAcquisitionStrategy().acquire()
    print(result["validation"])