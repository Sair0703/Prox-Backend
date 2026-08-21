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


ROOT_URL = "https://stores.hannaford.com/"
RETAILER = "Hannaford"
RETAILER_KEY = "hannaford"

WORKERS_STATE = 5
WORKERS_CITY = 10
WORKERS_DETAIL = 16

REQUEST_TIMEOUT = 30
MAX_RETRIES = 4
BACKOFFS = (1.0, 2.0, 4.0, 8.0)

EXPECTED_STATES = {"me", "ma", "nh", "ny", "vt"}

STATE_NAMES = {
    "me": "Maine",
    "ma": "Massachusetts",
    "nh": "New Hampshire",
    "ny": "New York",
    "vt": "Vermont",
}

DETAIL_PATH_RE = re.compile(
    r"^/(?P<state>[a-z]{2})/(?P<city>[^/]+)/(?P<store_id>\d+)/?$",
    re.IGNORECASE,
)


@dataclass(slots=True)
class FetchResult:
    url: str
    html: str | None
    status_code: int | None
    error: str | None = None
    attempts: int = 0


class HannafordAcquisitionStrategy:
    """
    Hannaford official directory acquisition.

    Root -> state -> city/direct-detail -> detail.

    Single-store city:
        /me/auburn/8347

    Multi-store city:
        /me/augusta
        -> /me/augusta/8250
        -> /me/augusta/8239

    retailer_store_id:
        numeric suffix of official store detail URL.
    """

    retailer = RETAILER
    retailer_key = RETAILER_KEY
    source_type = "html"
    provider = "Hannaford official store locator"

    def __init__(
        self,
        *,
        workers_state: int = WORKERS_STATE,
        workers_city: int = WORKERS_CITY,
        workers_detail: int = WORKERS_DETAIL,
    ) -> None:
        """Handle init  .

        :param workers_state: Workers state.
        :param workers_city: Workers city.
        :param workers_detail: Workers detail.
        :return: Result produced by init  .
        """
        self.workers_state = max(1, workers_state)
        self.workers_city = max(1, workers_city)
        self.workers_detail = max(1, workers_detail)

        self.failed_urls: list[dict[str, Any]] = []
        self.state_results: list[FetchResult] = []
        self.city_results: list[FetchResult] = []
        self.detail_results: list[FetchResult] = []

    def acquire(self) -> dict[str, Any]:
        """Run the complete store location acquisition workflow.

        :return: Acquired records, validation results, and run metadata.
        """
        self._print_header()

        state_urls = self._discover_state_urls()

        state_results = self._fetch_many(
            state_urls,
            self.workers_state,
            "Hannaford state pages",
            self.state_results,
        )

        city_links = self._discover_city_links(
            state_results
        )

        direct_detail_urls = {
            url for url in city_links
            if self._is_detail_url(url)
        }

        city_page_urls = {
            url for url in city_links
            if self._is_city_url(url)
        }

        city_results = self._fetch_many(
            sorted(city_page_urls),
            self.workers_city,
            "Hannaford multi-store city pages",
            self.city_results,
        )

        detail_urls = set(direct_detail_urls)
        detail_urls.update(
            self._discover_detail_links(
                city_results
            )
        )

        detail_results = self._fetch_many(
            sorted(detail_urls),
            self.workers_detail,
            "Hannaford store details",
            self.detail_results,
        )

        records_by_id: dict[str, dict[str, Any]] = {}
        raw_records = 0
        parse_failures = 0

        for result in detail_results:
            if result.error or not result.html:
                continue

            record = self._parse_detail_page(
                result.html,
                result.url,
            )

            if record is None:
                parse_failures += 1
                self.failed_urls.append(
                    {
                        "url": result.url,
                        "stage": "detail_parse",
                        "error": "No store record parsed",
                    }
                )
                continue

            raw_records += 1
            store_id = record["retailer_store_id"]

            existing = records_by_id.get(store_id)
            if existing is None:
                records_by_id[store_id] = record
            else:
                records_by_id[store_id] = self._merge_records(
                    existing,
                    record,
                )

        records = sorted(
            records_by_id.values(),
            key=self._store_sort_key,
        )

        validation = self._validate(
            records=records,
            state_urls=state_urls,
            city_page_urls=city_page_urls,
            direct_detail_urls=direct_detail_urls,
            detail_urls=detail_urls,
            raw_records=raw_records,
            parse_failures=parse_failures,
        )

        return {
            "retailer": self.retailer,
            "retailer_key": self.retailer_key,
            "source_type": self.source_type,
            "provider": self.provider,
            "records": records,
            "validation": validation,
            "state_urls": sorted(state_urls),
            "city_page_urls": sorted(city_page_urls),
            "direct_detail_urls": sorted(direct_detail_urls),
            "detail_urls": sorted(detail_urls),
            "failed_urls": self.failed_urls,
            "notes": self._build_notes(),
        }

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _discover_state_urls(self) -> list[str]:
        """Discover canonical state-directory URLs from the locator root.

        :return: Canonical state-directory URLs.
        """
        result = self._fetch_url(ROOT_URL)

        if result.error or not result.html:
            raise RuntimeError(
                f"Unable to fetch Hannaford root locator: "
                f"{result.error or 'empty response'}"
            )

        soup = BeautifulSoup(result.html, "html.parser")
        urls: set[str] = set()

        for anchor in soup.select(
            "section.Directory.Directory--ace.StateList "
            "a.Directory-listLink[href]"
        ):
            href = anchor.get("href")
            if not href:
                continue

            code = href.strip("/").split("/")[0].lower()
            if code not in EXPECTED_STATES:
                continue

            urls.add(
                self._canonical_url(
                    urljoin(ROOT_URL, href)
                )
            )

        if not urls:
            raise RuntimeError(
                "No Hannaford state directory links found"
            )

        return sorted(urls)

    def _discover_city_links(
        self,
        state_results: list[FetchResult],
    ) -> list[str]:
        """Discover city pages and direct store detail links from state pages.

        :param state_results: Fetched state-directory results.
        :return: Discovered city or direct-detail URLs.
        """
        urls: set[str] = set()

        for result in state_results:
            if result.error or not result.html:
                continue

            soup = BeautifulSoup(
                result.html,
                "html.parser",
            )

            for anchor in soup.select(
                "section.Directory.Directory--ace.CityList "
                "a.Directory-listLink[href]"
            ):
                href = anchor.get("href")
                if not href:
                    continue

                full_url = self._canonical_url(
                    urljoin(result.url, href)
                )

                if (
                    self._is_detail_url(full_url)
                    or self._is_city_url(full_url)
                ):
                    urls.add(full_url)

        return sorted(urls)

    def _discover_detail_links(
        self,
        city_results: list[FetchResult],
    ) -> set[str]:
        """Discover store detail URLs from city pages.

        :param city_results: Fetched city-directory results.
        :return: Canonical store detail URLs.
        """
        urls: set[str] = set()

        for result in city_results:
            if result.error or not result.html:
                continue

            soup = BeautifulSoup(
                result.html,
                "html.parser",
            )

            for anchor in soup.select(
                "a.Teaser-titleLink[href]"
            ):
                href = anchor.get("href")
                if not href:
                    continue

                full_url = self._canonical_url(
                    urljoin(result.url, href)
                )

                if self._is_detail_url(full_url):
                    urls.add(full_url)

        return urls

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _fetch_many(
        self,
        urls: list[str],
        workers: int,
        description: str,
        result_sink: list[FetchResult],
    ) -> list[FetchResult]:
        """Fetch multiple pages concurrently and record request failures.

        :param urls: Urls.
        :param workers: Maximum number of concurrent workers.
        :param description: Progress label for the acquisition stage.
        :param result_sink: Collection that receives completed fetch results.
        :return: Completed fetch results.
        """
        if not urls:
            return []

        results: list[FetchResult] = []

        with ThreadPoolExecutor(
            max_workers=workers
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
                desc=description,
                unit="page",
            ):
                result = future.result()
                results.append(result)
                result_sink.append(result)

                if result.error:
                    self.failed_urls.append(
                        {
                            "url": result.url,
                            "stage": description,
                            "status_code": result.status_code,
                            "error": result.error,
                            "attempts": result.attempts,
                        }
                    )

        return results

    @staticmethod
    def _fetch_url(url: str) -> FetchResult:
        """Fetch one page with bounded retries and backoff.

        :param url: URL to process.
        :return: Fetch result containing response content or failure metadata.
        """
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

        for attempt in range(1, MAX_RETRIES + 1):
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
    # Detail parsing
    # ------------------------------------------------------------------

    def _parse_detail_page(
        self,
        html: str,
        url: str,
    ) -> dict[str, Any] | None:
        """Parse one store detail page into a normalized record.

        :param html: HTML content to parse.
        :param url: URL to process.
        :return: Normalized store record, or None when parsing fails.
        """
        soup = BeautifulSoup(html, "html.parser")

        store_id = self._extract_store_id(url)
        if not store_id:
            return None

        name_node = soup.select_one(
            "#location-name .Core-company"
        ) or soup.select_one("#location-name")

        store_name = (
            name_node.get_text(" ", strip=True)
            if name_node
            else None
        )

        store_name = self._clean_store_name(
            store_name
        )

        address = self._text(
            soup.select_one(
                "#address .Address-line1"
            )
        )
        city = self._text(
            soup.select_one(
                "#address .Address-city"
            )
        )
        state = self._text(
            soup.select_one(
                "#address .Address-region"
            )
        )
        zip_code = self._text(
            soup.select_one(
                "#address .Address-postalCode"
            )
        )

        if not state:
            state = self._state_from_url(url)

        phone = self._extract_store_phone(soup)

        return {
            "retailer": RETAILER,
            "retailer_key": RETAILER_KEY,
            "retailer_store_id": store_id,
            "store_number": store_id,
            "store_name": store_name,
            "address": address,
            "city": city,
            "state": self._normalize_state(state),
            "zip_code": zip_code,
            "full_address": self._build_full_address(
                address=address,
                city=city,
                state=state,
                zip_code=zip_code,
            ),
            "phone": phone,
            "latitude": None,
            "longitude": None,
            "store_url": url,
            "retailer_store_url": (
                "https://www.hannaford.com/departments"
                f"?storeNum={store_id}"
            ),
            "source": (
                "Hannaford official store locator"
            ),
            "source_type": "html",
        }

    @staticmethod
    def _extract_store_phone(
        soup: BeautifulSoup,
    ) -> str | None:
        """Extract the main store phone number from a detail page.

        :param soup: Parsed HTML document.
        :return: Result produced by extract store phone.
        """
        node = soup.select_one(
            ".Core-storeContact .Core-contactLabel"
        )
        if node is None:
            return None

        text = node.get_text(" ", strip=True)
        match = re.search(
            r"Store:\s*(.+)$",
            text,
            flags=re.IGNORECASE,
        )
        return match.group(1).strip() if match else None

    @staticmethod
    def _clean_store_name(
        value: str | None,
    ) -> str | None:
        """Normalize the retailer-prefixed store name.

        :param value: Value to process.
        :return: Result produced by clean store name.
        """
        if not value:
            return None

        text = re.sub(r"\s+", " ", value).strip()
        cleaned = re.sub(
            r"^Hannaford\s*-\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )
        return cleaned or text

    @staticmethod
    def _extract_store_id(
        url: str,
    ) -> str | None:
        """Extract the retailer store ID from a detail URL.

        :param url: URL to process.
        :return: Result produced by extract store id.
        """
        path = urlparse(url).path.rstrip("/")
        match = re.search(r"/(\d+)$", path)
        return match.group(1) if match else None

    @staticmethod
    def _state_from_url(
        url: str,
    ) -> str | None:
        """Derive the state name from a store URL.

        :param url: URL to process.
        :return: Result produced by state from url.
        """
        parts = urlparse(url).path.strip("/").split("/")
        if not parts:
            return None
        return STATE_NAMES.get(parts[0].lower())

    @staticmethod
    def _text(node: Any) -> str | None:
        """Extract non-empty text from an HTML node.

        :param node: Node.
        :return: Trimmed node text, or None.
        """
        if node is None:
            return None
        value = node.get_text(" ", strip=True)
        return value or None

    @staticmethod
    def _build_full_address(
        *,
        address: str | None,
        city: str | None,
        state: str | None,
        zip_code: str | None,
    ) -> str | None:
        """Compose available address components into a full address.

        :param address: Address.
        :param city: City or locality component.
        :param state: State name or abbreviation.
        :param zip_code: Postal code component.
        :return: Combined address string, or None.
        """
        values = [
            str(value).strip()
            for value in (
                address,
                city,
                state,
                zip_code,
            )
            if value
        ]
        return ", ".join(values) if values else None

    @staticmethod
    def _normalize_state(
        value: str | None,
    ) -> str | None:
        """Normalize retailer state labels to postal abbreviations.

        :param value: Value to process.
        :return: Normalized state abbreviation or original value.
        """
        if not value:
            return None

        return {
            "Maine": "ME",
            "Massachusetts": "MA",
            "New Hampshire": "NH",
            "New York": "NY",
            "Vermont": "VT",
        }.get(
            value.strip(),
            value.strip(),
        )

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _canonical_url(url: str) -> str:
        """Normalize a URL to its canonical path form.

        :param url: URL to process.
        :return: Canonical URL without a trailing slash.
        """
        parsed = urlparse(url)
        return (
            f"{parsed.scheme}://"
            f"{parsed.netloc}"
            f"{parsed.path.rstrip('/')}"
        )

    @staticmethod
    def _is_detail_url(url: str) -> bool:
        """Determine whether a URL represents a store detail page.

        :param url: URL to process.
        :return: True when the URL represents a store detail page.
        """
        return (
            DETAIL_PATH_RE.match(
                urlparse(url).path
            )
            is not None
        )

    @staticmethod
    def _is_city_url(url: str) -> bool:
        """Determine whether a URL represents a city directory.

        :param url: URL to process.
        :return: True when the URL represents a city directory.
        """
        parts = urlparse(url).path.strip("/").split("/")
        return (
            len(parts) == 2
            and parts[0].lower() in EXPECTED_STATES
            and bool(parts[1])
        )

    # ------------------------------------------------------------------
    # Merge / validation
    # ------------------------------------------------------------------

    @staticmethod
    def _merge_records(
        first: dict[str, Any],
        second: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge non-empty values from overlapping store records.

        :param first: Existing store record.
        :param second: Overlapping store record.
        :return: Merged store record or deduplicated record list.
        """
        merged = dict(first)

        for key, value in second.items():
            if (
                merged.get(key) in (None, "")
                and value not in (None, "")
            ):
                merged[key] = value

        return merged

    def _validate(
        self,
        *,
        records: list[dict[str, Any]],
        state_urls: list[str],
        city_page_urls: set[str],
        direct_detail_urls: set[str],
        detail_urls: set[str],
        raw_records: int,
        parse_failures: int,
    ) -> dict[str, Any]:
        """Handle validate.

        :param records: Store records to process.
        :param state_urls: Discovered state-directory URLs.
        :param city_page_urls: Discovered city-directory URLs.
        :param direct_detail_urls: Store detail URLs discovered without a city page.
        :param detail_urls: Complete set of discovered store detail URLs.
        :param raw_records: Records collected before deduplication.
        :param parse_failures: Number of records or pages that failed parsing.
        :return: Result produced by validate.
        """
        ids = [
            record["retailer_store_id"]
            for record in records
            if record.get("retailer_store_id")
        ]

        missing_ids = sum(
            not record.get("retailer_store_id")
            for record in records
        )
        missing_addresses = sum(
            not record.get("full_address")
            for record in records
        )
        missing_phones = sum(
            not record.get("phone")
            for record in records
        )
        missing_coordinates = sum(
            record.get("latitude") is None
            or record.get("longitude") is None
            for record in records
        )

        state_counts: dict[str, int] = {}
        for record in records:
            state = record.get("state") or "UNKNOWN"
            state_counts[state] = (
                state_counts.get(state, 0) + 1
            )

        issues: list[str] = []

        if missing_ids:
            issues.append("missing_store_ids")
        if missing_addresses:
            issues.append("missing_addresses")
        if parse_failures:
            issues.append("detail_parse_failures")
        if self.failed_urls:
            issues.append("failed_urls")

        return {
            "valid": not issues,
            "total_records": len(records),
            "unique_store_ids": len(set(ids)),
            "raw_records": raw_records,
            "duplicate_records_merged": (
                raw_records - len(records)
            ),
            "missing_store_ids": missing_ids,
            "missing_addresses": missing_addresses,
            "missing_phones": missing_phones,
            "missing_coordinates": missing_coordinates,
            "state_page_count": len(state_urls),
            "city_page_count": len(city_page_urls),
            "direct_detail_count": len(direct_detail_urls),
            "detail_url_count": len(detail_urls),
            "parse_failures": parse_failures,
            "failed_urls": len(self.failed_urls),
            "state_counts": state_counts,
            "issues": issues,
        }

    @staticmethod
    def _store_sort_key(
        record: dict[str, Any],
    ) -> tuple[int, str]:
        """Build a deterministic sort key for store records.

        :param record: Store record to process.
        :return: Sort key for deterministic store ordering.
        """
        value = str(record.get("retailer_store_id", ""))
        if value.isdigit():
            return 0, f"{int(value):010d}"
        return 1, value

    @staticmethod
    def _print_header() -> None:
        """Print the acquisition configuration summary.

        :return: Result produced by print header.
        """
        print("=" * 72)
        print("Hannaford Acquisition Strategy v1")
        print("=" * 72)
        print(f"Source: {ROOT_URL}")
        print("Method: requests + BeautifulSoup")
        print(
            "Hierarchy: root -> state -> "
            "city/direct detail -> detail"
        )
        print(
            "Store ID: numeric suffix of official detail URL"
        )
        print(
            "Coordinates: not exposed in supplied locator/detail HTML; "
            "left empty"
        )
        print(
            f"Workers: state={WORKERS_STATE}, "
            f"city={WORKERS_CITY}, detail={WORKERS_DETAIL}"
        )
        print(
            f"Retry: max={MAX_RETRIES}, "
            f"backoff={BACKOFFS}"
        )
        print()

    @staticmethod
    def _build_notes() -> list[str]:
        """Return notes describing the acquisition approach.

        :return: Notes describing the acquisition approach.
        """
        return [
            "Official source: Hannaford store locator directory.",
            (
                "The locator hierarchy is root -> state -> city "
                "-> store detail."
            ),
            (
                "Single-store cities can expose a direct detail URL; "
                "multi-store cities are expanded through their city page."
            ),
            (
                "The numeric suffix of the official detail URL is used "
                "as retailer_store_id and store_number."
            ),
            (
                "The detail page corroborates the same identifier via "
                "storeNum in official Hannaford shopping/flyer URLs."
            ),
            (
                "Address, city, state, ZIP, and phone are parsed directly "
                "from the official detail page."
            ),
            (
                "Coordinates are not exposed in the supplied HTML and "
                "are therefore left empty."
            ),
            "Records are deduplicated by retailer_store_id.",
        ]