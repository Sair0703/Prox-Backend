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


ROOT_URL = "https://stores.giantfood.com/"
RETAILER = "Giant Food"
RETAILER_KEY = "giant_food"

WORKERS_DIRECTORY = 8
WORKERS_DETAIL = 16

REQUEST_TIMEOUT = 30
MAX_RETRIES = 4
BACKOFFS = (1.0, 2.0, 4.0, 8.0)

TOP_LEVEL_PATHS = {
    "dc/washington",
    "de",
    "md",
    "va",
}

EXPECTED_COUNTS = {
    "DC": 7,
    "DE": 6,
    "MD": 94,
    "VA": 58,
}

STATE_CODES = {"dc", "de", "md", "va"}

STATE_NAMES = {
    "dc": "Washington DC",
    "de": "Delaware",
    "md": "Maryland",
    "va": "Virginia",
}


@dataclass(slots=True)
class FetchResult:
    url: str
    html: str | None
    status_code: int | None
    error: str | None = None
    attempts: int = 0


class GiantFoodAcquisitionStrategy:
    """
    Giant Food official store-directory acquisition.

    V2 change:
        State-directory traversal now explicitly supports both:
          1. state -> city -> detail
          2. state -> direct detail

        This is required for Delaware, where links such as:
            /de/bear/300-eden-square-sc
        are already store detail URLs.

    Washington DC is also a special directory page whose cards directly
    expose store detail URLs.

    Store ID:
        No reliable retailer store ID is exposed in the supplied HTML.
        The strategy preserves store_url as the canonical store identity
        and only fills retailer_store_id when an explicit storeNum or
        data-store-id style identifier is actually present.
    """

    retailer = RETAILER
    retailer_key = RETAILER_KEY
    source_type = "html"
    provider = "Giant Food official store locator"

    def __init__(
        self,
        *,
        workers_directory: int = WORKERS_DIRECTORY,
        workers_detail: int = WORKERS_DETAIL,
    ) -> None:
        """Handle init  .

        :param workers_directory: Workers directory.
        :param workers_detail: Workers detail.
        :return: Result produced by init  .
        """
        self.workers_directory = max(
            1,
            workers_directory,
        )
        self.workers_detail = max(
            1,
            workers_detail,
        )

        self.failed_urls: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def acquire(self) -> dict[str, Any]:
        """Run the complete store location acquisition workflow.

        :return: Acquired records, validation results, and run metadata.
        """
        self._print_header()

        top_level_urls, directory_counts = (
            self._discover_top_level_urls()
        )

        directory_results: list[FetchResult] = []

        detail_urls: set[str] = set()
        city_page_urls: set[str] = set()
        direct_detail_urls: set[str] = set()

        # --------------------------------------------------------------
        # Phase 1: top-level directories
        # --------------------------------------------------------------

        dc_url = top_level_urls.get(
            "dc/washington"
        )

        if dc_url:
            dc_results = self._fetch_many(
                [dc_url],
                self.workers_directory,
                "Giant Food DC directory",
            )
            directory_results.extend(
                dc_results
            )

            direct_detail_urls.update(
                self._parse_detail_links(
                    dc_results
                )
            )

        state_urls = [
            url
            for key, url in top_level_urls.items()
            if key != "dc/washington"
        ]

        state_results = self._fetch_many(
            sorted(state_urls),
            self.workers_directory,
            "Giant Food state pages",
        )
        directory_results.extend(
            state_results
        )

        # --------------------------------------------------------------
        # Phase 2: state -> city OR direct detail
        # --------------------------------------------------------------

        for result in state_results:
            cities, direct_details = (
                self._parse_state_directory_links(
                    result
                )
            )

            city_page_urls.update(
                cities
            )
            direct_detail_urls.update(
                direct_details
            )

        # --------------------------------------------------------------
        # Phase 3: city pages -> detail
        # --------------------------------------------------------------

        city_results = self._fetch_many(
            sorted(city_page_urls),
            self.workers_directory,
            "Giant Food city pages",
        )
        directory_results.extend(
            city_results
        )

        direct_detail_urls.update(
            self._parse_detail_links(
                city_results
            )
        )

        detail_urls.update(
            direct_detail_urls
        )

        # --------------------------------------------------------------
        # Phase 4: detail pages
        # --------------------------------------------------------------

        detail_results = self._fetch_many(
            sorted(detail_urls),
            self.workers_detail,
            "Giant Food store details",
        )

        records_by_url: dict[
            str,
            dict[str, Any],
        ] = {}

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
                        "error": (
                            "No Giant Food "
                            "store record parsed"
                        ),
                    }
                )
                continue

            raw_records += 1

            canonical_url = record[
                "store_url"
            ]

            existing = records_by_url.get(
                canonical_url
            )

            if existing is None:
                records_by_url[
                    canonical_url
                ] = record
            else:
                records_by_url[
                    canonical_url
                ] = self._merge_records(
                    existing,
                    record,
                )

        records = sorted(
            records_by_url.values(),
            key=self._record_sort_key,
        )

        validation = self._validate(
            records=records,
            raw_records=raw_records,
            parse_failures=parse_failures,
            directory_counts=directory_counts,
            city_page_urls=city_page_urls,
            detail_urls=detail_urls,
        )

        return {
            "retailer": self.retailer,
            "retailer_key": self.retailer_key,
            "source_type": self.source_type,
            "provider": self.provider,
            "records": records,
            "validation": validation,
            "top_level_urls": top_level_urls,
            "city_page_urls": sorted(
                city_page_urls
            ),
            "direct_detail_urls": sorted(
                direct_detail_urls
            ),
            "detail_urls": sorted(
                detail_urls
            ),
            "failed_urls": self.failed_urls,
            "notes": self._build_notes(),
        }

    # ------------------------------------------------------------------
    # Directory discovery
    # ------------------------------------------------------------------

    def _discover_top_level_urls(
        self,
    ) -> tuple[dict[str, str], dict[str, int]]:
        """Discover supported top-level retailer directory URLs and counts.

        :return: Result produced by discover top level urls.
        """
        result = self._fetch_url(
            ROOT_URL
        )

        if result.error or not result.html:
            raise RuntimeError(
                "Unable to fetch Giant Food root directory: "
                f"{result.error or 'empty response'}"
            )

        soup = BeautifulSoup(
            result.html,
            "html.parser",
        )

        urls: dict[str, str] = {}
        counts: dict[str, int] = {}

        for item in soup.select(
            ".DirectoryList-item"
        ):
            anchor = item.select_one(
                "a.DirectoryList-itemLink[href]"
            )

            if anchor is None:
                continue

            href = anchor.get("href")
            if not href:
                continue

            canonical = self._canonical_url(
                urljoin(ROOT_URL, href)
            )

            path_key = urlparse(
                canonical
            ).path.strip("/").lower()

            if path_key not in TOP_LEVEL_PATHS:
                continue

            urls[path_key] = canonical

            count_node = item.select_one(
                ".DirectoryList-itemCount"
            )

            counts[
                self._state_from_path(
                    path_key
                )
            ] = self._parse_count(
                count_node.get_text(
                    strip=True
                )
                if count_node
                else None
            )

        missing = TOP_LEVEL_PATHS - set(urls)

        if missing:
            raise RuntimeError(
                "Missing expected Giant Food top-level "
                f"directories: {sorted(missing)}"
            )

        return urls, counts

    def _parse_state_directory_links(
        self,
        result: FetchResult,
    ) -> tuple[set[str], set[str]]:
        """Classify state-directory links as city pages or store detail pages.

        :param result: Fetched directory result to parse.
        :return: Result produced by parse state directory links.
        """
        city_urls: set[str] = set()
        detail_urls: set[str] = set()

        if result.error or not result.html:
            return city_urls, detail_urls

        soup = BeautifulSoup(
            result.html,
            "html.parser",
        )

        for anchor in soup.select(
            "a.DirectoryList-itemLink[href]"
        ):
            href = anchor.get("href")
            if not href:
                continue

            full_url = self._canonical_url(
                urljoin(
                    result.url,
                    href,
                )
            )

            if self._is_detail_url(
                full_url
            ):
                detail_urls.add(
                    full_url
                )
            elif self._is_city_url(
                full_url
            ):
                city_urls.add(
                    full_url
                )

        return city_urls, detail_urls

    @staticmethod
    def _parse_detail_links(
        results: list[FetchResult],
    ) -> set[str]:
        """Extract canonical store detail URLs from directory pages.

        :param results: Results.
        :return: Result produced by parse detail links.
        """
        urls: set[str] = set()

        for result in results:
            if result.error or not result.html:
                continue

            soup = BeautifulSoup(
                result.html,
                "html.parser",
            )

            selectors = (
                "a.Teaser-primaryLink[href]",
                "a.Teaser-titleLink[href]",
                "a[href][data-ya-track='view_page']",
            )

            for selector in selectors:
                for anchor in soup.select(
                    selector
                ):
                    href = anchor.get("href")
                    if not href:
                        continue

                    full_url = (
                        GiantFoodAcquisitionStrategy
                        ._canonical_url(
                            urljoin(
                                result.url,
                                href,
                            )
                        )
                    )

                    if GiantFoodAcquisitionStrategy._is_detail_url(
                        full_url
                    ):
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
    ) -> list[FetchResult]:
        """Fetch multiple pages concurrently and record request failures.

        :param urls: Urls.
        :param workers: Maximum number of concurrent workers.
        :param description: Progress label for the acquisition stage.
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
    def _fetch_url(
        url: str,
    ) -> FetchResult:
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

                last_status = (
                    response.status_code
                )

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
        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        address_node = soup.select_one(
            "#address"
        )

        if address_node is None:
            return None

        heading = soup.select_one(
            ".NAP-heading"
        )

        store_name = None

        if heading:
            text = heading.get_text(
                " ",
                strip=True,
            )
            store_name = re.sub(
                r"^Giant Food\s*",
                "",
                text,
                flags=re.IGNORECASE,
            ).strip()

        address = self._text(
            address_node.select_one(
                ".c-address-street-1"
            )
        )
        city = self._text(
            address_node.select_one(
                ".c-address-city"
            )
        )
        state = self._text(
            address_node.select_one(
                ".c-address-state"
            )
        )
        zip_code = self._text(
            address_node.select_one(
                ".c-address-postal-code"
            )
        )

        phone_node = soup.select_one(
            ".c-phone-main-number"
        )

        phone = (
            phone_node.get_text(
                " ",
                strip=True,
            )
            if phone_node
            else None
        )

        retailer_store_id = (
            self._extract_reliable_store_id(
                soup
            )
        )

        state_code = self._normalize_state(
            state
        )

        return {
            "retailer": RETAILER,
            "retailer_key": RETAILER_KEY,
            "retailer_store_id": retailer_store_id,
            "store_number": retailer_store_id,
            "store_name": store_name,
            "address": address,
            "city": city,
            "state": state_code,
            "zip_code": zip_code,
            "full_address": self._build_full_address(
                address=address,
                city=city,
                state=state_code,
                zip_code=zip_code,
            ),
            "phone": phone,
            "latitude": None,
            "longitude": None,
            "store_url": url,
            "source": (
                "Giant Food official store locator"
            ),
            "source_type": "html",
        }

    @staticmethod
    def _extract_reliable_store_id(
        soup: BeautifulSoup,
    ) -> str | None:
        """Extract an explicitly exposed retailer store identifier.

        :param soup: Parsed HTML document.
        :return: Authoritative store identifier when present.
        """
        for anchor in soup.select(
            "a[href]"
        ):
            href = anchor.get("href") or ""

            match = re.search(
                r"[?&]storeNum=(\d+)",
                href,
                flags=re.IGNORECASE,
            )

            if match:
                return match.group(1)

        for node in soup.select(
            "[data-store-number], "
            "[data-storenumber], "
            "[data-store-id], "
            "[data-storeid]"
        ):
            for attr in (
                "data-store-number",
                "data-storenumber",
                "data-store-id",
                "data-storeid",
            ):
                value = node.get(attr)
                if value and str(value).isdigit():
                    return str(value)

        return None

    @staticmethod
    def _text(
        node: Any,
    ) -> str | None:
        """Extract non-empty text from an HTML node.

        :param node: Node.
        :return: Trimmed node text, or None.
        """
        if node is None:
            return None

        value = node.get_text(
            " ",
            strip=True,
        )

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

        return (
            ", ".join(values)
            if values
            else None
        )

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

        text = value.strip()

        return {
            "Washington DC": "DC",
            "Washington, DC": "DC",
            "Delaware": "DE",
            "Maryland": "MD",
            "Virginia": "VA",
        }.get(
            text,
            text,
        )

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _canonical_url(
        url: str,
    ) -> str:
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
    def _is_city_url(
        url: str,
    ) -> bool:
        """Determine whether a URL represents a city directory.

        :param url: URL to process.
        :return: True when the URL represents a city directory.
        """
        parts = (
            urlparse(url)
            .path
            .strip("/")
            .split("/")
        )

        return (
            len(parts) == 2
            and parts[0].lower()
            in STATE_CODES
            and bool(parts[1])
        )

    @staticmethod
    def _is_detail_url(
        url: str,
    ) -> bool:
        """Determine whether a URL represents a store detail page.

        :param url: URL to process.
        :return: True when the URL represents a store detail page.
        """
        parts = (
            urlparse(url)
            .path
            .strip("/")
            .split("/")
        )

        return (
            len(parts) == 3
            and parts[0].lower()
            in STATE_CODES
            and bool(parts[1])
            and bool(parts[2])
        )

    @staticmethod
    def _state_from_path(
        path: str,
    ) -> str:
        """Derive a state abbreviation from a directory path.

        :param path: Path.
        :return: State abbreviation derived from the directory path.
        """
        code = (
            path
            .split("/")
            [0]
            .lower()
        )

        return {
            "dc": "DC",
            "de": "DE",
            "md": "MD",
            "va": "VA",
        }.get(
            code,
            code.upper(),
        )

    @staticmethod
    def _parse_count(
        value: str | None,
    ) -> int:
        """Parse an integer count from retailer directory text.

        :param value: Value to process.
        :return: Parsed integer count.
        """
        if not value:
            return 0

        match = re.search(
            r"(\d+)",
            value,
        )

        return (
            int(match.group(1))
            if match
            else 0
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate(
        self,
        *,
        records: list[dict[str, Any]],
        raw_records: int,
        parse_failures: int,
        directory_counts: dict[str, int],
        city_page_urls: set[str],
        detail_urls: set[str],
    ) -> dict[str, Any]:
        """Handle validate.

        :param records: Store records to process.
        :param raw_records: Records collected before deduplication.
        :param parse_failures: Number of records or pages that failed parsing.
        :param directory_counts: Store counts reported by the retailer directory.
        :param city_page_urls: Discovered city-directory URLs.
        :param detail_urls: Complete set of discovered store detail URLs.
        :return: Result produced by validate.
        """
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
            record.get(
                "latitude"
            ) is None
            or record.get(
                "longitude"
            ) is None
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

        state_differences: dict[
            str,
            dict[str, int],
        ] = {}

        for state, expected in directory_counts.items():
            acquired = state_counts.get(
                state,
                0,
            )

            if acquired != expected:
                state_differences[
                    state
                ] = {
                    "expected": expected,
                    "acquired": acquired,
                }

        issues: list[str] = []

        if missing_addresses:
            issues.append(
                "missing_addresses"
            )

        if parse_failures:
            issues.append(
                "detail_parse_failures"
            )

        if self.failed_urls:
            issues.append(
                "failed_urls"
            )

        if state_differences:
            issues.append(
                "state_count_mismatch"
            )

        return {
            "valid": not issues,
            "total_records": len(records),
            "raw_records": raw_records,
            "missing_store_ids": missing_ids,
            "missing_addresses": missing_addresses,
            "missing_phones": missing_phones,
            "missing_coordinates": missing_coordinates,
            "city_page_count": len(
                city_page_urls
            ),
            "detail_url_count": len(
                detail_urls
            ),
            "parse_failures": parse_failures,
            "failed_urls": len(
                self.failed_urls
            ),
            "directory_counts": directory_counts,
            "acquired_state_counts": state_counts,
            "state_count_differences": (
                state_differences
            ),
            "issues": issues,
        }

    # ------------------------------------------------------------------
    # Presentation
    # ------------------------------------------------------------------

    @staticmethod
    def _print_header() -> None:
        """Print the acquisition configuration summary.

        :return: Result produced by print header.
        """
        print("=" * 72)
        print(
            "Giant Food Acquisition Strategy v2"
        )
        print("=" * 72)
        print(
            f"Source: {ROOT_URL}"
        )
        print(
            "Method: requests + BeautifulSoup"
        )
        print(
            "Hierarchy: root -> state -> "
            "city OR direct detail -> detail"
        )
        print(
            "Store ID: explicit storeNum/data-store-id only; "
            "no synthetic ID"
        )
        print(
            "Coordinates: not exposed in supplied HTML; left empty"
        )
        print(
            f"Workers: directory={WORKERS_DIRECTORY}, "
            f"detail={WORKERS_DETAIL}"
        )
        print(
            f"Retry: max={MAX_RETRIES}, "
            f"backoff={BACKOFFS}"
        )
        print()

    @staticmethod
    def _record_sort_key(
        record: dict[str, Any],
    ) -> tuple[str, str, str, str]:
        """Build a deterministic sort key for store records.

        :param record: Store record to process.
        :return: Sort key for deterministic store ordering.
        """
        return (
            str(
                record.get(
                    "state",
                    "",
                )
            ),
            str(
                record.get(
                    "city",
                    "",
                )
            ),
            str(
                record.get(
                    "store_name",
                    "",
                )
            ),
            str(
                record.get(
                    "store_url",
                    "",
                )
            ),
        )

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
                merged.get(key)
                in (None, "")
                and value
                not in (None, "")
            ):
                merged[key] = value

        return merged

    @staticmethod
    def _build_notes() -> list[str]:
        """Return notes describing the acquisition approach.

        :return: Notes describing the acquisition approach.
        """
        return [
            (
                "Official source: Giant Food store locator."
            ),
            (
                "Washington DC uses a special directory page with "
                "store cards directly."
            ),
            (
                "State directory traversal supports both direct "
                "store detail links and intermediate city pages."
            ),
            (
                "Delaware uses direct state-page links such as "
                "/de/bear/300-eden-square-sc."
            ),
            (
                "Maryland and Virginia use city pages where applicable, "
                "which are expanded into store detail URLs."
            ),
            (
                "Store-card/detail HTML provides store name, address, "
                "phone, and official store URL."
            ),
            (
                "No retailer store ID is invented from the address slug."
            ),
            (
                "Coordinates are not exposed in the supplied HTML and "
                "remain empty."
            ),
            (
                "Store URLs are the canonical deduplication identity."
            ),
        ]


if __name__ == "__main__":
    result = GiantFoodAcquisitionStrategy().acquire()
    print(result["validation"])