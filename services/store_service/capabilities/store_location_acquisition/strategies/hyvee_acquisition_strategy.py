from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse, parse_qs
import re
import time

from bs4 import BeautifulSoup
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)
from tqdm import tqdm


ROOT_URL = (
    "https://www.hy-vee.com/stores/"
    "store-finder-results.aspx"
)

RETAILER = "Hy-Vee"
RETAILER_KEY = "hy_vee"

EXPECTED_PAGES = 44

REQUEST_TIMEOUT_MS = 30_000
PAGE_SETTLE_MS = 500


@dataclass(slots=True)
class PageResult:
    page_number: int
    url: str
    records: list[dict[str, Any]]
    current_page: int | None
    total_pages: int | None
    error: str | None = None


class HyVeeAcquisitionStrategy:
    """
    Hy-Vee store-finder acquisition.

    Source:
        https://www.hy-vee.com/stores/store-finder-results.aspx

    The supplied HTML shows a stateful ASP.NET WebForms paginator:
        __doPostBack(...Page2...)
        __doPostBack(...btnNext...)

    Therefore pages are acquired sequentially in one browser page so that
    the server-side postback state is preserved.

    Each store card already exposes:
        - storecode
        - storeid
        - store name
        - address
        - city/state/ZIP
        - main phone
        - store detail URL

    The store detail URL is:
        /stores/detail.aspx?s=<storeid>

    `storeid` / the `s=` query parameter is used as retailer_store_id.
    `storecode` is preserved separately because both identifiers are exposed
    by the official locator and may represent different internal concepts.

    No store detail-page traversal is required.
    """

    retailer = RETAILER
    retailer_key = RETAILER_KEY
    source_type = "html"
    provider = "Hy-Vee official store finder"

    def __init__(
        self,
        *,
        expected_pages: int = EXPECTED_PAGES,
        headless: bool = True,
    ) -> None:
        """Handle init  .

        :param expected_pages: Expected number of locator result pages.
        :param headless: Whether to run the browser without a visible window.
        :return: Result produced by init  .
        """
        self.expected_pages = expected_pages
        self.headless = headless

        self.page_results: list[PageResult] = []
        self.failed_pages: list[int] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self) -> dict[str, Any]:
        """Run the complete store location acquisition workflow.

        :return: Acquired records, validation results, and run metadata.
        """
        print("=" * 72)
        print("Hy-Vee Acquisition v1")
        print("=" * 72)
        print(f"Source: {ROOT_URL}")
        print("Method: Playwright + BeautifulSoup")
        print(
            "Hierarchy: store finder page -> 44 ASP.NET postback pages"
        )
        print(
            "Store ID: storeid / detail URL query parameter `s`"
        )
        print(
            "Store Code: official `storecode` attribute preserved separately"
        )
        print(
            "Store detail pages: not required; all required fields are in cards"
        )
        print(
            f"Expected pages: {self.expected_pages}"
        )
        print()

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=self.headless,
            )

            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/151.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                viewport={
                    "width": 1440,
                    "height": 900,
                },
            )

            page = context.new_page()
            page.set_default_timeout(
                REQUEST_TIMEOUT_MS
            )

            try:
                page.goto(
                    ROOT_URL,
                    wait_until="domcontentloaded",
                    timeout=REQUEST_TIMEOUT_MS,
                )

                page.wait_for_timeout(
                    PAGE_SETTLE_MS
                )

                records_by_store_id: dict[
                    str,
                    dict[str, Any],
                ] = {}

                for page_number in tqdm(
                    range(1, self.expected_pages + 1),
                    desc="Hy-Vee pages",
                    unit="page",
                ):
                    result = self._parse_current_page(
                        page=page,
                        expected_page=page_number,
                    )

                    self.page_results.append(
                        result
                    )

                    if result.error:
                        self.failed_pages.append(
                            page_number
                        )
                        continue

                    for record in result.records:
                        store_id = record.get(
                            "retailer_store_id"
                        )

                        if not store_id:
                            continue

                        records_by_store_id[
                            str(store_id)
                        ] = record

                    if page_number >= self.expected_pages:
                        break

                    moved = self._go_to_next_page(
                        page=page,
                        current_page=page_number,
                    )

                    if not moved:
                        self.failed_pages.append(
                            page_number + 1
                        )
                        break

                records = sorted(
                    records_by_store_id.values(),
                    key=self._store_sort_key,
                )

            finally:
                context.close()
                browser.close()

        validation = self._validate(
            records=records,
            page_results=self.page_results,
        )

        return {
            "retailer": self.retailer,
            "retailer_key": self.retailer_key,
            "source_type": self.source_type,
            "provider": self.provider,
            "records": records,
            "validation": validation,
            "page_results": [
                {
                    "page_number": item.page_number,
                    "url": item.url,
                    "records": len(item.records),
                    "current_page": item.current_page,
                    "total_pages": item.total_pages,
                    "error": item.error,
                }
                for item in self.page_results
            ],
            "failed_pages": self.failed_pages,
            "notes": self._build_notes(),
        }

    # ------------------------------------------------------------------
    # Page parsing
    # ------------------------------------------------------------------

    def _parse_current_page(
        self,
        *,
        page: Page,
        expected_page: int,
    ) -> PageResult:
        """Parse the currently rendered Hy-Vee results page.

        :param page: Playwright page used for browser interaction.
        :param expected_page: Expected one-based page number.
        :return: Parsed page result with store records and pagination metadata.
        """
        html = page.content()
        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        current_page = self._parse_current_page_number(
            soup
        )
        total_pages = self._parse_total_page_count(
            soup
        )

        records: list[dict[str, Any]] = []

        table = soup.select_one(
            "#ctl00_cph_main_content_spuStoreFinderResults_gvStores"
        )

        if table is None:
            return PageResult(
                page_number=expected_page,
                url=page.url,
                records=[],
                current_page=current_page,
                total_pages=total_pages,
                error="Store result table not found",
            )

        for row in table.select("tr"):
            record = self._parse_store_row(
                row,
                source_url=page.url,
            )

            if record is None:
                continue

            records.append(record)

        if not records:
            return PageResult(
                page_number=expected_page,
                url=page.url,
                records=[],
                current_page=current_page,
                total_pages=total_pages,
                error="No store cards parsed",
            )

        if (
            current_page is not None
            and current_page != expected_page
        ):
            return PageResult(
                page_number=expected_page,
                url=page.url,
                records=records,
                current_page=current_page,
                total_pages=total_pages,
                error=(
                    f"Expected page {expected_page}, "
                    f"but locator reports page {current_page}"
                ),
            )

        return PageResult(
            page_number=expected_page,
            url=page.url,
            records=records,
            current_page=current_page,
            total_pages=total_pages,
        )

    def _parse_store_row(
        self,
        row: Any,
        *,
        source_url: str,
    ) -> dict[str, Any] | None:
        """Parse one Hy-Vee result row into a normalized store record.

        :param row: HTML row containing store information.
        :param source_url: Source page URL associated with the record.
        :return: Normalized store record, or None for a non-store row.
        """
        card_link = row.select_one(
            'a[id$="_aLink2"][storeid]'
        )

        detail_link = row.select_one(
            'a[id$="_aStoreDetails"]'
        )

        if card_link is None or detail_link is None:
            return None

        store_id = (
            card_link.get("storeid")
            or self._extract_store_id_from_url(
                detail_link.get("href")
            )
        )

        if not store_id:
            return None

        store_code = card_link.get(
            "storecode"
        )

        detail_href = detail_link.get(
            "href"
        )

        detail_url = (
            urljoin(
                source_url,
                detail_href,
            )
            if detail_href
            else None
        )

        name = self._parse_store_name(
            card_link
        )

        address = self._parse_card_address(
            row
        )

        phone = self._parse_phone(
            row
        )

        return {
            "retailer": self.retailer,
            "retailer_key": self.retailer_key,
            "retailer_store_id": str(
                store_id
            ),
            "store_number": str(
                store_id
            ),
            "store_code": (
                str(store_code)
                if store_code
                else None
            ),
            "store_name": name,
            "address": address[
                "address"
            ],
            "city": address[
                "city"
            ],
            "state": address[
                "state"
            ],
            "zip_code": address[
                "zip_code"
            ],
            "full_address": address[
                "full_address"
            ],
            "phone": phone,
            "latitude": None,
            "longitude": None,
            "store_url": detail_url,
            "source": (
                "Hy-Vee official store finder"
            ),
            "source_type": "html",
        }

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def _go_to_next_page(
        self,
        *,
        page: Page,
        current_page: int,
    ) -> bool:
        """Advance the ASP.NET paginator to the next result page.

        :param page: Playwright page used for browser interaction.
        :param current_page: Current one-based page number.
        :return: True when pagination successfully advances.
        """
        next_locator = page.locator(
            'a[id$="_btnNext"]'
        ).first

        if next_locator.count() == 0:
            return False

        class_name = (
            next_locator.get_attribute(
                "class"
            )
            or ""
        )

        if "aspNetDisabled" in class_name:
            return False

        previous_signature = (
            self._page_signature(page)
        )

        try:
            next_locator.click(
                timeout=REQUEST_TIMEOUT_MS
            )
        except PlaywrightTimeoutError:
            return False

        # ASP.NET WebForms postback may perform a normal document navigation.
        try:
            page.wait_for_load_state(
                "domcontentloaded",
                timeout=REQUEST_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
            pass

        deadline = time.time() + 15.0

        while time.time() < deadline:
            time.sleep(0.25)

            current_signature = (
                self._page_signature(page)
            )

            if (
                current_signature
                and current_signature
                != previous_signature
            ):
                reported = (
                    self._parse_current_page_number_from_html(
                        page.content()
                    )
                )

                if (
                    reported is None
                    or reported > current_page
                ):
                    return True

        return False

    @staticmethod
    def _page_signature(
        page: Page,
    ) -> str:
        """Build a signature used to detect page changes after postback.

        :param page: Playwright page used for browser interaction.
        :return: Signature representing the currently rendered result page.
        """
        hrefs = page.locator(
            'a[id$="_aStoreDetails"]'
        ).evaluate_all(
            "(nodes) => nodes.map(n => n.getAttribute('href')).join('|')"
        )

        active = page.locator(
            "a.current_page"
        ).all_inner_texts()

        return (
            f"{'|'.join(active)}::"
            f"{hrefs}"
        )

    # ------------------------------------------------------------------
    # HTML helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_store_name(
        card_link: Any,
    ) -> str:
        """Extract the store name from a locator card.

        :param card_link: Store-card link containing retailer metadata.
        :return: Normalized store name.
        """
        strong = card_link.select_one(
            "strong"
        )

        if strong is None:
            return card_link.get_text(
                " ",
                strip=True,
            )

        return " ".join(
            strong.stripped_strings
        )

    @staticmethod
    def _parse_card_address(
        row: Any,
    ) -> dict[str, str | None]:
        """Parse address fields from a locator result row.

        :param row: HTML row containing store information.
        :return: Parsed address components from the store card.
        """
        paragraph = row.select_one(
            'a[id$="_aLink2"]'
        )

        if paragraph is None:
            return {
                "address": None,
                "city": None,
                "state": None,
                "zip_code": None,
                "full_address": None,
            }

        # Remove link text so only the address/phone area remains.
        cloned = BeautifulSoup(
            str(
                paragraph.parent
            ),
            "html.parser",
        )

        for node in cloned.select(
            'a[id$="_aLink2"]'
        ):
            node.decompose()

        for node in cloned.select(
            'a[id$="_aStoreDetails"]'
        ):
            node.decompose()

        text = " ".join(
            cloned.stripped_strings
        )

        text = re.sub(
            r"Pharmacy:\s*[\d()\-\s]+$",
            "",
            text,
            flags=re.IGNORECASE,
        )

        text = re.sub(
            r"Main:\s*[\d()\-\s]+.*$",
            "",
            text,
            flags=re.IGNORECASE,
        )

        text = re.sub(
            r"\s+",
            " ",
            text,
        ).strip()

        match = re.search(
            r"(.+?)\s+"
            r"([A-Za-z .'-]+),\s*"
            r"([A-Za-z]{2})\s+"
            r"(\d{5}(?:-\d{4})?)$",
            text,
        )

        if match:
            street = match.group(
                1
            ).strip()

            city = match.group(
                2
            ).strip()

            state = match.group(
                3
            ).upper()

            zip_code = match.group(
                4
            )

            return {
                "address": street,
                "city": city,
                "state": state,
                "zip_code": zip_code,
                "full_address": (
                    f"{street}, "
                    f"{city}, "
                    f"{state} "
                    f"{zip_code}"
                ),
            }

        return {
            "address": text or None,
            "city": None,
            "state": None,
            "zip_code": None,
            "full_address": text or None,
        }

    @staticmethod
    def _parse_phone(
        row: Any,
    ) -> str | None:
        """Extract the main store phone number from a locator row.

        :param row: HTML row containing store information.
        :return: Main store phone number when present.
        """
        paragraph = row.find(
            "p"
        )

        if paragraph is None:
            return None

        text = " ".join(
            paragraph.stripped_strings
        )

        match = re.search(
            r"Main:\s*"
            r"(\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4})",
            text,
            flags=re.IGNORECASE,
        )

        if not match:
            return None

        return match.group(
            1
        ).strip()

    @staticmethod
    def _extract_store_id_from_url(
        href: str | None,
    ) -> str | None:
        """Extract the store ID from the detail URL query parameter.

        :param href: Store detail link or URL.
        :return: Store identifier from the detail URL query string.
        """
        if not href:
            return None

        parsed = urlparse(
            href
        )

        values = parse_qs(
            parsed.query
        ).get(
            "s"
        )

        if values:
            return values[0]

        return None

    @staticmethod
    def _parse_current_page_number(
        soup: BeautifulSoup,
    ) -> int | None:
        """Parse the active page number from paginator markup.

        :param soup: Parsed HTML document.
        :return: Current page number reported by the locator.
        """
        active = soup.select_one(
            "a.current_page"
        )

        if active is None:
            return None

        text = active.get_text(
            " ",
            strip=True,
        )

        return (
            int(text)
            if text.isdigit()
            else None
        )

    @classmethod
    def _parse_current_page_number_from_html(
        cls,
        html: str,
    ) -> int | None:
        """Parse the active page number from raw HTML.

        :param html: HTML content to parse.
        :return: Current page number parsed from HTML.
        """
        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        return cls._parse_current_page_number(
            soup
        )

    @staticmethod
    def _parse_total_page_count(
        soup: BeautifulSoup,
    ) -> int | None:
        """Parse the total number of pages exposed by the paginator.

        :param soup: Parsed HTML document.
        :return: Largest page number exposed by the paginator.
        """
        paging = soup.select_one(
            "div.paging"
        )

        if paging is None:
            return None

        values = []

        for anchor in paging.select(
            "a"
        ):
            text = anchor.get_text(
                " ",
                strip=True,
            )

            if text.isdigit():
                values.append(
                    int(text)
                )

        if not values:
            return None

        return max(values)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate(
        self,
        *,
        records: list[dict[str, Any]],
        page_results: list[PageResult],
    ) -> dict[str, Any]:
        """Handle validate.

        :param records: Store records to process.
        :param page_results: Results collected from each acquired page.
        :return: Result produced by validate.
        """
        ids = [
            str(
                record["retailer_store_id"]
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

        missing_store_codes = sum(
            not record.get(
                "store_code"
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

        duplicate_store_ids = (
            len(ids)
            - len(set(ids))
        )

        parsed_pages = len(
            [
                result
                for result in page_results
                if result.records
                and not result.error
            ]
        )

        issues: list[str] = []

        if parsed_pages != self.expected_pages:
            issues.append(
                "page_count_mismatch"
            )

        if missing_ids:
            issues.append(
                "missing_store_ids"
            )

        if missing_addresses:
            issues.append(
                "missing_addresses"
            )

        if duplicate_store_ids:
            issues.append(
                "duplicate_store_ids"
            )

        valid = not issues

        return {
            "valid": valid,
            "total_records": len(records),
            "unique_store_ids": len(
                set(ids)
            ),
            "missing_store_ids": missing_ids,
            "missing_store_codes": (
                missing_store_codes
            ),
            "missing_addresses": (
                missing_addresses
            ),
            "missing_phones": (
                missing_phones
            ),
            "missing_coordinates": (
                missing_coordinates
            ),
            "duplicate_store_ids": (
                duplicate_store_ids
            ),
            "parsed_pages": parsed_pages,
            "expected_pages": (
                self.expected_pages
            ),
            "failed_pages": len(
                self.failed_pages
            ),
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

    # ------------------------------------------------------------------
    # Notes
    # ------------------------------------------------------------------

    @staticmethod
    def _build_notes() -> list[str]:
        """Return notes describing the acquisition approach.

        :return: Notes describing the acquisition approach.
        """
        return [
            (
                "Official source: Hy-Vee store finder results page."
            ),
            (
                "The locator uses ASP.NET WebForms postback pagination "
                "via __doPostBack, so pages are acquired sequentially "
                "within one browser session."
            ),
            (
                "The supplied HTML exposes 44 total pages."
            ),
            (
                "Each store card directly exposes storecode, storeid, "
                "store name, address, phone, and a store details URL."
            ),
            (
                "The store details URL uses /stores/detail.aspx?s=<storeid>."
            ),
            (
                "storeid / the `s` query parameter is used as "
                "retailer_store_id."
            ),
            (
                "storecode is preserved separately as an official "
                "retailer-side identifier exposed on the locator card."
            ),
            (
                "No detail-page traversal is required for acquisition."
            ),
            (
                "Coordinates are not exposed in the supplied store-card "
                "HTML and are left empty rather than inferred."
            ),
            (
                "Records are deduplicated by retailer_store_id across "
                "all 44 pages."
            ),
        ]