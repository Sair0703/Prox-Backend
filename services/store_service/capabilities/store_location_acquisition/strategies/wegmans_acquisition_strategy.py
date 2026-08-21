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


ROOT_URL = "https://www.wegmans.com/stores"
BASE_URL = "https://www.wegmans.com"

RETAILER = "Wegmans"
RETAILER_KEY = "wegmans"

WORKERS = 16
REQUEST_TIMEOUT = 30
MAX_RETRIES = 4
BACKOFFS = (1.0, 2.0, 4.0, 8.0)


@dataclass(slots=True)
class FetchResult:
    url: str
    html: str | None
    status_code: int | None
    error: str | None = None
    attempts: int = 0


class WegmansAcquisitionStrategy:
    """
    Wegmans official store-directory acquisition.

    Observed hierarchy:
        /stores
          -> state sections
          -> direct store links:
                 /stores/woodmore-md
          -> store detail page

    The store directory itself contains one direct store URL per location.
    No intermediate city page and no pagination are required.

    Important:
        The supplied HTML does not expose a reliable numeric retailer store
        ID. The strategy therefore does not synthesize one from the slug.
        It tries explicit structured identifiers first and otherwise leaves
        retailer_store_id empty.

    Canonical deduplication:
        store_url.
    """

    retailer = RETAILER
    retailer_key = RETAILER_KEY
    source_type = "html"
    provider = "Wegmans official store locator"

    def __init__(
        self,
        *,
        workers: int = WORKERS,
    ) -> None:
        """Initialize acquisition configuration and run state.

        :param workers: Maximum number of concurrent workers.
        :return: Result produced by init  .
        """
        self.workers = max(1, workers)
        self.failed_urls: list[dict[str, Any]] = []

    def acquire(self) -> dict[str, Any]:
        """Run the complete store location acquisition workflow.

        :return: Acquired records, validation results, and run metadata.
        """
        self._print_header()

        root_result = self._fetch_url(
            ROOT_URL
        )

        if (
            root_result.error
            or not root_result.html
        ):
            raise RuntimeError(
                "Unable to fetch Wegmans store directory: "
                f"{root_result.error or 'empty response'}"
            )

        store_urls = self._discover_store_urls(
            root_result.html
        )

        detail_results = self._fetch_many(
            store_urls
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
                            "No Wegmans store record parsed"
                        ),
                    }
                )
                continue

            raw_records += 1

            key = record[
                "store_url"
            ]

            existing = records_by_url.get(
                key
            )

            if existing is None:
                records_by_url[key] = record
            else:
                records_by_url[key] = (
                    self._merge_records(
                        existing,
                        record,
                    )
                )

        records = sorted(
            records_by_url.values(),
            key=self._record_sort_key,
        )

        validation = self._validate(
            records=records,
            raw_records=raw_records,
            parse_failures=parse_failures,
            store_url_count=len(store_urls),
        )

        return {
            "retailer": self.retailer,
            "retailer_key": self.retailer_key,
            "source_type": self.source_type,
            "provider": self.provider,
            "records": records,
            "validation": validation,
            "store_urls": store_urls,
            "failed_urls": self.failed_urls,
            "notes": self._build_notes(),
        }

    # ------------------------------------------------------------------
    # Directory discovery
    # ------------------------------------------------------------------

    @staticmethod
    def _discover_store_urls(
        html: str,
    ) -> list[str]:
        """Discover store urls.

        :param html: HTML content to parse.
        :return: Result produced by discover store urls.
        """
        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        urls: set[str] = set()

        for anchor in soup.select(
            'a[href^="/stores/"]'
        ):
            href = anchor.get(
                "href"
            )

            if not href:
                continue

            full_url = WegmansAcquisitionStrategy._canonical_url(
                urljoin(
                    ROOT_URL,
                    href,
                )
            )

            path = urlparse(
                full_url
            ).path.strip("/")

            parts = path.split("/")

            if (
                len(parts) == 2
                and parts[0].lower() == "stores"
                and parts[1]
            ):
                # /stores/<store-slug>
                urls.add(full_url)

        return sorted(urls)

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _fetch_many(
        self,
        urls: list[str],
    ) -> list[FetchResult]:
        """Fetch many.

        :param urls: Store URLs to fetch.
        :return: Result produced by fetch many.
        """
        if not urls:
            return []

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
                desc="Wegmans store details",
                unit="page",
            ):
                result = future.result()
                results.append(result)

                if result.error:
                    self.failed_urls.append(
                        {
                            "url": result.url,
                            "stage": "detail_fetch",
                            "status_code": (
                                result.status_code
                            ),
                            "error": result.error,
                            "attempts": result.attempts,
                        }
                    )

        return results

    @staticmethod
    def _fetch_url(
        url: str,
    ) -> FetchResult:
        """Fetch url.

        :param url: URL to fetch or process.
        :return: Result produced by fetch url.
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

                last_status = response.status_code

                if response.status_code != 200:
                    last_error = (
                        f"HTTP {response.status_code}"
                    )
                    continue

                if not response.text.strip():
                    last_error = (
                        "Empty response body"
                    )
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
        """Parse detail page.

        :param html: HTML content to parse.
        :param url: URL to fetch or process.
        :return: Result produced by parse detail page.
        """
        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        info_card = soup.select_one(
            ".component--store-detail-info-card.store-info-card"
        )

        if info_card is None:
            info_card = soup.select_one(
                ".store-info-card"
            )

        if info_card is None:
            return None

        name_node = info_card.select_one(
            "h1"
        )

        store_name = (
            self._clean_text(
                name_node.get_text(
                    " ",
                    strip=True,
                )
            )
            if name_node
            else None
        )

        address_node = info_card.select_one(
            "address.store-address"
        )

        address = None
        city = None
        state = None
        zip_code = None

        if address_node:
            lines = [
                self._clean_text(
                    p.get_text(
                        " ",
                        strip=True,
                    )
                )
                for p in address_node.select("p")
            ]
            lines = [
                line
                for line in lines
                if line
            ]

            if lines:
                address = lines[0]

            if len(lines) >= 2:
                city, state, zip_code = (
                    self._parse_city_state_zip(
                        lines[1]
                    )
                )

        phone_node = info_card.select_one(
            "a.store-phone-number[href^='tel:']"
        )

        phone = (
            self._clean_text(
                phone_node.get_text(
                    " ",
                    strip=True,
                )
            )
            if phone_node
            else None
        )

        retailer_store_id = (
            self._extract_reliable_store_id(
                soup
            )
        )

        latitude, longitude = (
            self._extract_coordinates(
                soup
            )
        )

        return {
            "retailer": RETAILER,
            "retailer_key": RETAILER_KEY,
            "retailer_store_id": retailer_store_id,
            "store_number": retailer_store_id,
            "store_name": store_name,
            "address": address,
            "city": city,
            "state": state,
            "zip_code": zip_code,
            "full_address": self._build_full_address(
                address=address,
                city=city,
                state=state,
                zip_code=zip_code,
            ),
            "phone": phone,
            "latitude": latitude,
            "longitude": longitude,
            "store_url": self._canonical_url(
                url
            ),
            "source": (
                "Wegmans official store locator"
            ),
            "source_type": "html",
        }

    @staticmethod
    def _parse_city_state_zip(
        value: str,
    ) -> tuple[str | None, str | None, str | None]:
        """Parse city state zip.

        :param value: Value to normalize or convert.
        :return: Result produced by parse city state zip.
        """
        match = re.match(
            r"^\s*(.+?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\s*$",
            value,
        )

        if not match:
            return (
                value,
                None,
                None,
            )

        return (
            match.group(1).strip(),
            match.group(2).strip(),
            match.group(3).strip(),
        )

    @staticmethod
    def _extract_reliable_store_id(
        soup: BeautifulSoup,
    ) -> str | None:
        # Do not infer IDs from /stores/<slug>.
        # Search only for explicit structured identifiers.
        """Extract reliable store id.

        :param soup: Soup.
        :return: Result produced by extract reliable store id.
        """
        candidates = (
            "storeId",
            "storeID",
            "store_id",
            "storeNumber",
            "store_number",
            "locationId",
            "locationID",
            "location_id",
            "locationNumber",
            "location_number",
        )

        for script in soup.find_all(
            "script"
        ):
            text = script.string or script.get_text()
            if not text:
                continue

            for key in candidates:
                patterns = (
                    rf'"{re.escape(key)}"\s*:\s*"([^"]+)"',
                    rf'"{re.escape(key)}"\s*:\s*(\d+)',
                    rf"'{re.escape(key)}'\s*:\s*'([^']+)'",
                    rf"'{re.escape(key)}'\s*:\s*(\d+)",
                )

                for pattern in patterns:
                    match = re.search(
                        pattern,
                        text,
                        flags=re.IGNORECASE,
                    )

                    if match:
                        value = match.group(1)
                        return str(value).strip()

        for node in soup.select(
            "[data-store-id],"
            "[data-storeid],"
            "[data-store-number],"
            "[data-storenumber],"
            "[data-location-id],"
            "[data-locationid]"
        ):
            for attr in (
                "data-store-id",
                "data-storeid",
                "data-store-number",
                "data-storenumber",
                "data-location-id",
                "data-locationid",
            ):
                value = node.get(attr)
                if value:
                    return str(value).strip()

        return None

    @staticmethod
    def _extract_coordinates(
        soup: BeautifulSoup,
    ) -> tuple[float | None, float | None]:
        # Support explicit structured coordinate attributes if present.
        """Extract coordinates.

        :param soup: Soup.
        :return: Result produced by extract coordinates.
        """
        for node in soup.select(
            "[data-latitude][data-longitude]"
        ):
            lat = WegmansAcquisitionStrategy._parse_float(
                node.get("data-latitude")
            )
            lon = WegmansAcquisitionStrategy._parse_float(
                node.get("data-longitude")
            )

            if lat is not None and lon is not None:
                return lat, lon

        # Also inspect inline JSON for common coordinate keys.
        scripts = soup.find_all(
            "script"
        )

        latitude_patterns = (
            r'"latitude"\s*:\s*(-?\d+(?:\.\d+)?)',
            r'"lat"\s*:\s*(-?\d+(?:\.\d+)?)',
        )
        longitude_patterns = (
            r'"longitude"\s*:\s*(-?\d+(?:\.\d+)?)',
            r'"lng"\s*:\s*(-?\d+(?:\.\d+)?)',
        )

        lat_value: float | None = None
        lon_value: float | None = None

        for script in scripts:
            text = script.string or script.get_text()
            if not text:
                continue

            if lat_value is None:
                for pattern in latitude_patterns:
                    match = re.search(
                        pattern,
                        text,
                    )
                    if match:
                        lat_value = float(
                            match.group(1)
                        )
                        break

            if lon_value is None:
                for pattern in longitude_patterns:
                    match = re.search(
                        pattern,
                        text,
                    )
                    if match:
                        lon_value = float(
                            match.group(1)
                        )
                        break

            if (
                lat_value is not None
                and lon_value is not None
            ):
                return (
                    lat_value,
                    lon_value,
                )

        return None, None

    @staticmethod
    def _parse_float(
        value: str | None,
    ) -> float | None:
        """Parse float.

        :param value: Value to normalize or convert.
        :return: Result produced by parse float.
        """
        if value is None:
            return None

        try:
            return float(value)
        except (
            TypeError,
            ValueError,
        ):
            return None

    @staticmethod
    def _clean_text(
        value: str | None,
    ) -> str | None:
        """Normalize text.

        :param value: Value to normalize or convert.
        :return: Result produced by clean text.
        """
        if value is None:
            return None

        text = re.sub(
            r"\s+",
            " ",
            value,
        ).strip()

        return text or None

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate(
        self,
        *,
        records: list[dict[str, Any]],
        raw_records: int,
        parse_failures: int,
        store_url_count: int,
    ) -> dict[str, Any]:
        """Handle validate.

        :param records: Store records to validate or process.
        :param raw_records: Number of successfully parsed raw records.
        :param parse_failures: Number of detail pages that failed parsing.
        :param store_url_count: Number of store URLs discovered from the directory.
        :return: Result produced by validate.
        """
        unique_urls = {
            record.get(
                "store_url"
            )
            for record in records
            if record.get(
                "store_url"
            )
        }

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

        issues: list[str] = []

        if missing_addresses:
            issues.append(
                "missing_addresses"
            )

        if missing_phones:
            issues.append(
                "missing_phones"
            )

        if parse_failures:
            issues.append(
                "detail_parse_failures"
            )

        if self.failed_urls:
            issues.append(
                "failed_urls"
            )

        if len(records) != store_url_count:
            issues.append(
                "store_count_mismatch"
            )

        # Missing retailer IDs are informational for now because the
        # supplied HTML does not establish that the slug itself is the ID.
        return {
            "valid": not issues,
            "total_records": len(records),
            "unique_store_urls": len(
                unique_urls
            ),
            "raw_records": raw_records,
            "duplicate_records_merged": (
                raw_records - len(records)
            ),
            "missing_store_ids": missing_ids,
            "missing_addresses": missing_addresses,
            "missing_phones": missing_phones,
            "missing_coordinates": (
                missing_coordinates
            ),
            "store_directory_links": (
                store_url_count
            ),
            "parse_failures": parse_failures,
            "failed_urls": len(
                self.failed_urls
            ),
            "issues": issues,
        }

    # ------------------------------------------------------------------
    # Helpers / presentation
    # ------------------------------------------------------------------

    @staticmethod
    def _canonical_url(
        url: str,
    ) -> str:
        """Handle canonical url.

        :param url: URL to fetch or process.
        :return: Result produced by canonical url.
        """
        parsed = urlparse(
            url
        )

        return (
            f"{parsed.scheme}://"
            f"{parsed.netloc}"
            f"{parsed.path.rstrip('/')}"
        )

    @staticmethod
    def _build_full_address(
        *,
        address: str | None,
        city: str | None,
        state: str | None,
        zip_code: str | None,
    ) -> str | None:
        """Build full address.

        :param address: Street-address component.
        :param city: City or locality component.
        :param state: State name or abbreviation.
        :param zip_code: Postal-code component.
        :return: Result produced by build full address.
        """
        values = [
            value.strip()
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
    def _record_sort_key(
        record: dict[str, Any],
    ) -> tuple[str, str, str]:
        """Build sort key.

        :param record: Record.
        :return: Result produced by record sort key.
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
        )

    @staticmethod
    def _merge_records(
        first: dict[str, Any],
        second: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge records.

        :param first: Existing store record.
        :param second: Overlapping store record.
        :return: Result produced by merge records.
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
    def _print_header() -> None:
        """Handle print header.

        :return: Result produced by print header.
        """
        print("=" * 72)
        print(
            "Wegmans Acquisition Strategy v1"
        )
        print("=" * 72)
        print(
            f"Source: {ROOT_URL}"
        )
        print(
            "Method: requests + BeautifulSoup"
        )
        print(
            "Hierarchy: /stores -> direct store links -> detail page"
        )
        print(
            "Store ID: explicit structured ID only; "
            "slug is not treated as a store ID"
        )
        print(
            "Coordinates: explicit structured coordinates if exposed"
        )
        print(
            f"Workers: {WORKERS}"
        )
        print(
            f"Retry: max={MAX_RETRIES}, "
            f"backoff={BACKOFFS}"
        )
        print()

    @staticmethod
    def _build_notes() -> list[str]:
        """Build notes.

        :return: Result produced by build notes.
        """
        return [
            (
                "Official source: Wegmans store directory."
            ),
            (
                "The root /stores page organizes locations by state and "
                "links directly to individual store pages."
            ),
            (
                "A store page such as /stores/woodmore-md exposes the "
                "store name, street address, city/state/ZIP, phone, and "
                "hours directly in the store information card."
            ),
            (
                "The store URL slug is preserved as store_url but is not "
                "treated as retailer_store_id unless an explicit ID is "
                "found in structured page data."
            ),
            (
                "Coordinates are populated only when explicit structured "
                "latitude/longitude values are found."
            ),
            (
                "Store URLs are the canonical deduplication identity."
            ),
        ]


if __name__ == "__main__":
    result = WegmansAcquisitionStrategy().acquire()
    print(result["validation"])