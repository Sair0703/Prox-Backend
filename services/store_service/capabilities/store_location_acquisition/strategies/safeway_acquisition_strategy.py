from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urljoin, urlparse
import re
import time

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from tqdm import tqdm

import requests

from services.store_service.capabilities.store_location_acquisition.protocals import (
    AcquisitionArtifact,
    AcquisitionSourceInfo,
    AcquisitionValidationResult,
    StoreLocationAcquisitionStrategy,
)

BASE_URL = "https://local.safeway.com"
ROOT_URL = f"{BASE_URL}/safeway.html"


@dataclass(slots=True)
class _PageJob:
    href: str
    page_type: str  # state | city | detail
    state_code: str | None = None
    city_slug: str | None = None
    city_name: str | None = None


class SafewayAcquisitionStrategy(StoreLocationAcquisitionStrategy):
    retailer_key = "safeway"
    retailer_name = "Safeway"

    def __init__(
        self,
        *,
        state_workers: int = 4,
        city_workers: int = 6,
        store_workers: int = 8,
        request_timeout: int = 30,
        max_retries: int = 2,
        playwright_wait_ms: int = 1500,
    ) -> None:
        """Initialize acquisition configuration and run state.

        :param state_workers: State workers.
        :param city_workers: City workers.
        :param store_workers: Store workers.
        :param request_timeout: Request timeout.
        :param max_retries: Max retries.
        :param playwright_wait_ms: Playwright wait ms.
        :return: Result produced by init  .
        """
        self.state_workers = state_workers
        self.city_workers = city_workers
        self.store_workers = store_workers
        self.request_timeout = request_timeout
        self.max_retries = max_retries
        self.playwright_wait_ms = playwright_wait_ms

        self._failed_state_pages: list[dict[str, Any]] = []
        self._failed_city_pages: list[dict[str, Any]] = []
        self._failed_detail_pages: list[dict[str, Any]] = []

    def discover_source(self) -> AcquisitionSourceInfo:
        """Return metadata describing the retailer's official acquisition source.

        :return: Metadata describing the acquisition source.
        """
        return AcquisitionSourceInfo(
            retailer_key=self.retailer_key,
            retailer_name=self.retailer_name,
            official_website_url="https://www.safeway.com/",
            store_locator_url=ROOT_URL,
            endpoint_url=ROOT_URL,
            source_type="html",
            provider="Playwright + BeautifulSoup",
            notes=(
                "Safeway directory hierarchy: root -> state pages -> city pages / "
                "single-store detail pages. Multi-store city pages render store "
                "cards and the title link points to the canonical detail page."
            ),
        )

    def fetch_raw_artifacts(self) -> list[AcquisitionArtifact]:
        """Fetch raw artifacts required for store location acquisition.

        :return: Raw acquisition artifacts.
        """
        self._failed_state_pages = []
        self._failed_city_pages = []
        self._failed_detail_pages = []

        artifacts: list[AcquisitionArtifact] = []

        root_html = self._render_html(ROOT_URL)
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
                },
            )
        )

        root_jobs = self._parse_directory_jobs(root_html)
        state_jobs = [job for job in root_jobs if job.page_type == "state"]
        root_leaf_jobs = [job for job in root_jobs if job.page_type in {"city", "detail"}]

        if not state_jobs:
            raise RuntimeError(
                "Safeway root page was rendered successfully, but no state links were parsed."
            )

        print(f"[Safeway] discovered states: {len(state_jobs)}")
        print(f"[Safeway] discovered direct leaf pages on root: {len(root_leaf_jobs)}")

        state_artifacts: list[AcquisitionArtifact] = []
        with tqdm(total=len(state_jobs), desc="Safeway states", unit="state") as pbar:
            with ThreadPoolExecutor(max_workers=self.state_workers) as pool:
                futures = {
                    pool.submit(self._fetch_page, job): job
                    for job in state_jobs
                }
                for future in as_completed(futures):
                    artifact = future.result()
                    artifacts.append(artifact)
                    if artifact.metadata.get("scrape_status") == "success":
                        state_artifacts.append(artifact)
                    pbar.update(1)

        state_leaf_jobs: list[_PageJob] = []
        for artifact in state_artifacts:
            state_code = self._clean_text(artifact.metadata.get("state_code"))
            state_leaf_jobs.extend(
                self._parse_state_leaf_jobs(
                    artifact.content or "",
                    state_code=state_code,
                )
            )

        all_leaf_jobs = self._dedupe_page_jobs([*root_leaf_jobs, *state_leaf_jobs])
        if not all_leaf_jobs:
            raise RuntimeError(
                "Safeway state pages were fetched, but no city/detail links were discovered."
            )

        city_jobs = [job for job in all_leaf_jobs if job.page_type == "city"]
        detail_jobs = [job for job in all_leaf_jobs if job.page_type == "detail"]

        print(f"[Safeway] discovered city pages: {len(city_jobs)}")
        print(f"[Safeway] discovered direct detail pages: {len(detail_jobs)}")

        leaf_artifacts: list[AcquisitionArtifact] = []
        with tqdm(total=len(all_leaf_jobs), desc="Safeway city/detail pages", unit="page") as pbar:
            with ThreadPoolExecutor(max_workers=self.city_workers) as pool:
                futures = {
                    pool.submit(self._fetch_page, job): job
                    for job in all_leaf_jobs
                }
                for future in as_completed(futures):
                    artifact = future.result()
                    artifacts.append(artifact)
                    if artifact.metadata.get("scrape_status") == "success":
                        leaf_artifacts.append(artifact)
                    pbar.update(1)

        detail_jobs_from_city: list[_PageJob] = []
        for artifact in leaf_artifacts:
            if artifact.metadata.get("page_type") != "city":
                continue
            detail_jobs_from_city.extend(self._parse_city_detail_jobs(artifact))

        all_detail_jobs = self._dedupe_page_jobs([*detail_jobs, *detail_jobs_from_city])
        if not all_detail_jobs:
            raise RuntimeError(
                "Safeway city pages were fetched, but no detail links were discovered."
            )

        print(f"[Safeway] discovered detail pages to fetch: {len(all_detail_jobs)}")

        with tqdm(total=len(all_detail_jobs), desc="Safeway detail pages", unit="page") as pbar:
            with ThreadPoolExecutor(max_workers=self.store_workers) as pool:
                futures = {
                    pool.submit(self._fetch_page, job): job
                    for job in all_detail_jobs
                }
                for future in as_completed(futures):
                    artifact = future.result()
                    artifacts.append(artifact)
                    pbar.update(1)

        return artifacts

    def extract_store_payloads(
        self,
        artifacts: Sequence[AcquisitionArtifact],
    ) -> list[dict[str, Any]]:
        """Extract normalized store payloads from acquired artifacts.

        :param artifacts: Acquisition artifacts to process.
        :return: Normalized and deduplicated store payloads.
        """
        parse_artifacts = [
            artifact
            for artifact in artifacts
            if artifact.metadata.get("page_type") == "detail"
            and artifact.metadata.get("scrape_status") == "success"
        ]

        rows_by_store_id: dict[str, dict[str, Any]] = {}
        with tqdm(total=len(parse_artifacts), desc="Parsing Safeway stores", unit="page") as pbar:
            with ThreadPoolExecutor(max_workers=self.store_workers) as pool:
                futures = {
                    pool.submit(self._parse_detail_artifact, artifact): artifact
                    for artifact in parse_artifacts
                }
                for future in as_completed(futures):
                    row = future.result()
                    if row is not None:
                        store_id = self._clean_text(row.get("retailer_store_id"))
                        if store_id:
                            rows_by_store_id[store_id] = row
                    pbar.update(1)

        return list(rows_by_store_id.values())

    def validate_store_payloads(
        self,
        payloads: Sequence[Mapping[str, Any]],
    ) -> AcquisitionValidationResult:
        """Validate acquired store payloads for completeness and uniqueness.

        :param payloads: Normalized store payloads to validate.
        :return: Validation result for the acquired payloads.
        """
        total_records = len(payloads)
        store_ids = [self._clean_text(row.get("retailer_store_id")) for row in payloads]
        unique_store_ids = len({sid for sid in store_ids if sid})
        missing_store_ids = sum(1 for sid in store_ids if not sid)

        missing_addresses = sum(
            1
            for row in payloads
            if not self._clean_text(row.get("street_address"))
            or not self._clean_text(row.get("city"))
            or not self._clean_text(row.get("state"))
            or not self._clean_text(row.get("zip_code"))
        )

        missing_phones = sum(
            1
            for row in payloads
            if not self._clean_text(row.get("phone"))
        )

        duplicate_store_ids: list[str] = []
        seen: set[str] = set()
        for sid in store_ids:
            if not sid:
                continue
            if sid in seen and sid not in duplicate_store_ids:
                duplicate_store_ids.append(sid)
            seen.add(sid)

        issue_counts: dict[str, int] = {}
        if missing_store_ids:
            issue_counts["missing_store_ids"] = missing_store_ids
        if missing_addresses:
            issue_counts["missing_addresses"] = missing_addresses
        if missing_phones:
            issue_counts["missing_phones"] = missing_phones
        if self._failed_state_pages:
            issue_counts["failed_state_pages"] = len(self._failed_state_pages)
        if self._failed_city_pages:
            issue_counts["failed_city_pages"] = len(self._failed_city_pages)
        if self._failed_detail_pages:
            issue_counts["failed_detail_pages"] = len(self._failed_detail_pages)

        notes = [
            "Root page is used only to discover state pages and some direct city/detail pages.",
            "State pages may contain city pages or direct detail pages.",
            "City pages are parsed for title links, then the canonical detail pages are fetched.",
            "Store id is recovered from the weekly ad URL query parameter storeId=.",
            f"Workers: state={self.state_workers}, city={self.city_workers}, store={self.store_workers}",
        ]

        is_valid = (
            total_records > 0
            and missing_store_ids == 0
            and missing_addresses == 0
            and len(self._failed_state_pages) == 0
            and len(self._failed_city_pages) == 0
            and len(self._failed_detail_pages) == 0
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

    def build_run_notes(self) -> list[str]:
        """Return acquisition source and execution details for the run summary.

        :return: Notes describing the acquisition run.
        """
        return [
            f"Source: {ROOT_URL}",
            "Method: Playwright rendering + BeautifulSoup",
            "Hierarchy: root -> state pages -> city pages / single-store detail pages",
            "Single-store pages are parsed directly; multi-store city pages are used to discover canonical detail pages.",
            "Dedup key: retailer_store_id from View Weekly Ad URL storeId query parameter.",
        ]

    def _render_html(self, url: str) -> str:
        """Render html.

        :param url: URL to fetch or process.
        :return: Result produced by render html.
        """
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1440, "height": 1600},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/151.0.0.0 Safari/537.36"
                ),
                locale="en-US",
            )
            page = context.new_page()
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=self.request_timeout * 1000)
                if response is not None and response.status >= 400:
                    raise RuntimeError(f"HTTP {response.status} for {url}")
                page.wait_for_timeout(self.playwright_wait_ms)
                html = page.content()
                if not html or "<html" not in html.lower():
                    raise RuntimeError(f"Safeway page returned empty HTML: {url}")
                return html
            finally:
                context.close()
                browser.close()

    def _fetch_page(self, job: _PageJob) -> AcquisitionArtifact:
        """Fetch page.

        :param job: Acquisition job to process.
        :return: Result produced by fetch page.
        """
        url = urljoin(BASE_URL + "/", job.href.lstrip("/"))
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                html = self._render_html(url)
                return AcquisitionArtifact(
                    artifact_type="html",
                    source_url=url,
                    content=html,
                    metadata={
                        "retrieved_at_utc": self._utc_now(),
                        "page_type": job.page_type,
                        "state_code": job.state_code,
                        "city_slug": job.city_slug,
                        "city_name": job.city_name,
                        "http_status": 200,
                        "scrape_status": "success",
                    },
                )
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries - 1:
                    time.sleep(0.8 * (2 ** attempt))

        bucket = self._failed_state_pages if job.page_type == "state" else self._failed_city_pages if job.page_type == "city" else self._failed_detail_pages
        bucket.append({"url": url, "state_code": job.state_code, "city_slug": job.city_slug, "error": str(last_error)})
        return AcquisitionArtifact(
            artifact_type="html",
            source_url=url,
            content="",
            metadata={
                "retrieved_at_utc": self._utc_now(),
                "page_type": job.page_type,
                "state_code": job.state_code,
                "city_slug": job.city_slug,
                "city_name": job.city_name,
                "http_status": 500,
                "scrape_status": "failed",
                "error": str(last_error),
            },
        )

    def _parse_directory_jobs(self, html: str) -> list[_PageJob]:
        """Parse directory jobs.

        :param html: HTML content to parse.
        :return: Result produced by parse directory jobs.
        """
        soup = BeautifulSoup(html, "html.parser")
        jobs: list[_PageJob] = []

        for a in soup.select('a[href*="safeway/"]'):
            href = self._clean_text(a.get("href"))
            if not href:
                continue

            abs_url = urljoin(BASE_URL + "/", href)
            path = urlparse(abs_url).path.strip("/")
            parts = [part for part in path.split("/") if part]
            if not parts or parts[0] != "safeway":
                continue

            job = self._classify_safeway_href(
                href=href,
                parts=parts,
                anchor_text=self._clean_text(a.get_text(" ", strip=True)),
            )
            if job is not None:
                jobs.append(job)

        return self._dedupe_page_jobs(jobs)

    def _parse_state_leaf_jobs(self, html: str, *, state_code: str | None) -> list[_PageJob]:
        """Parse state leaf jobs.

        :param html: HTML content to parse.
        :param state_code: State code associated with the page.
        :return: Result produced by parse state leaf jobs.
        """
        soup = BeautifulSoup(html, "html.parser")
        jobs: list[_PageJob] = []

        for a in soup.select("a[href]"):
            href = self._clean_text(a.get("href"))
            if not href:
                continue

            abs_url = urljoin(BASE_URL + "/", href)
            path = urlparse(abs_url).path.strip("/")
            parts = [part for part in path.split("/") if part]
            if not parts or parts[0] != "safeway":
                continue

            job = self._classify_safeway_href(
                href=href,
                parts=parts,
                anchor_text=self._clean_text(a.get_text(" ", strip=True)),
                default_state_code=state_code,
            )
            if job is not None and job.page_type in {"city", "detail"}:
                jobs.append(job)

        return self._dedupe_page_jobs(jobs)

    def _classify_safeway_href(
        self,
        *,
        href: str,
        parts: list[str],
        anchor_text: str | None,
        default_state_code: str | None = None,
    ) -> _PageJob | None:
        """Classify safeway href.

        :param href: Link or URL to process.
        :param parts: Parsed URL path segments.
        :param anchor_text: Visible anchor text associated with the link.
        :param default_state_code: Fallback state code for discovered links.
        :return: Result produced by classify safeway href.
        """
        state_code: str | None = None
        city_slug: str | None = None
        page_type: str | None = None

        if len(parts) == 2 and parts[0] == "safeway":
            state_code = parts[1].split(".")[0].upper()
            if re.fullmatch(r"[A-Z]{2}", state_code):
                page_type = "state"
                return _PageJob(href=self._normalize_href(href), page_type=page_type, state_code=state_code)
            return None

        if len(parts) >= 3 and parts[0] == "safeway":
            state_code = parts[1].upper()
            if not re.fullmatch(r"[A-Z]{2}", state_code):
                return None

            if len(parts) == 3:
                city_slug = parts[2].removesuffix(".html")
                page_type = "city"
            else:
                city_slug = parts[2]
                page_type = "detail"

            # Use the passed/default state when it exists so city links keep context.
            if default_state_code:
                state_code = default_state_code.upper()

            return _PageJob(
                href=self._normalize_href(href),
                page_type=page_type,
                state_code=state_code,
                city_slug=city_slug,
                city_name=anchor_text,
            )

        return None

    def _parse_city_detail_jobs(self, artifact: AcquisitionArtifact) -> list[_PageJob]:
        """Parse city detail jobs.

        :param artifact: Acquisition artifact to parse.
        :return: Result produced by parse city detail jobs.
        """
        soup = BeautifulSoup(artifact.content or "", "html.parser")
        source_url = self._clean_text(artifact.source_url)
        state_code = self._clean_text(artifact.metadata.get("state_code"))
        city_slug = self._clean_text(artifact.metadata.get("city_slug"))
        city_name = self._clean_text(artifact.metadata.get("city_name"))

        jobs: list[_PageJob] = []
        for a in soup.select("article.Teaser--directory a.Teaser-titleLink[href]"):
            href = self._clean_text(a.get("href"))
            if not href:
                continue
            job = _PageJob(
                href=href,
                page_type="detail",
                state_code=state_code,
                city_slug=city_slug,
                city_name=city_name,
            )
            jobs.append(job)

        # Fallback: if cards aren't matched by the exact selector, grab any detail-like link.
        if not jobs:
            for a in soup.select('a[href$=".html"]'):
                href = self._clean_text(a.get("href"))
                if not href:
                    continue
                abs_url = urljoin(source_url or BASE_URL + "/", href)
                path = urlparse(abs_url).path.strip("/")
                parts = [part for part in path.split("/") if part]
                if len(parts) >= 4 and parts[0] == "safeway":
                    jobs.append(
                        _PageJob(
                            href=href,
                            page_type="detail",
                            state_code=state_code,
                            city_slug=city_slug,
                            city_name=city_name,
                        )
                    )

        return self._dedupe_page_jobs(jobs)

    def _parse_detail_artifact(self, artifact: AcquisitionArtifact) -> dict[str, Any] | None:
        """Parse detail artifact.

        :param artifact: Acquisition artifact to parse.
        :return: Result produced by parse detail artifact.
        """
        soup = BeautifulSoup(artifact.content or "", "html.parser")
        source_url = self._clean_text(artifact.source_url)
        state_code = self._clean_text(artifact.metadata.get("state_code"))
        city_slug = self._clean_text(artifact.metadata.get("city_slug"))
        city_name = self._clean_text(artifact.metadata.get("city_name"))

        weekly_ad_href = self._find_weekly_ad_href(soup)
        store_id = self._extract_store_id_from_weekly_ad(weekly_ad_href)
        if not store_id:
            return None

        store_name = self._extract_store_name(soup)
        street_address, city, state, zip_code = self._extract_address(soup)
        phone = self._extract_phone(soup)

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
            "full_address": self._compose_full_address(
                street_address=street_address,
                city=city,
                state=state,
                zip_code=zip_code,
            ),
            "phone": phone,
            "store_url": source_url,
            "source_url": source_url,
            "source_sitemap": None,
            "state_code": state_code,
            "city_slug": city_slug,
            "city_name": city_name or city,
            "extraction_source": "Playwright / BeautifulSoup",
            "scrape_status": "success",
            "http_status": 200,
            "error_message": None,
            "scraped_at_utc": self._clean_text(artifact.metadata.get("retrieved_at_utc")) or self._utc_now(),
        }

    def _find_weekly_ad_href(self, soup: BeautifulSoup) -> str | None:
        """Find weekly ad href.

        :param soup: Parsed HTML document.
        :return: Result produced by find weekly ad href.
        """
        for a in soup.select('a[href]'):
            text = self._clean_text(a.get_text(" ", strip=True)) or ""
            href = self._clean_text(a.get("href"))
            if not href:
                continue
            href_lower = href.lower()
            if "storeid=" in href_lower and ("weeklyad" in href_lower or "weekly ad" in text.lower()):
                return href
        return None

    @staticmethod
    def _extract_store_id_from_weekly_ad(href: str | None) -> str | None:
        """Extract store id from weekly ad.

        :param href: Link or URL to process.
        :return: Result produced by extract store id from weekly ad.
        """
        if not href:
            return None
        parsed = urlparse(href)
        query = parse_qs(parsed.query)
        store_id = (query.get("storeId") or query.get("storeid") or [None])[0]
        if store_id:
            return store_id.strip() or None
        match = re.search(r"[?&]storeId=(\d+)", href, flags=re.I)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _extract_store_name(soup: BeautifulSoup) -> str | None:
        """Extract store name.

        :param soup: Parsed HTML document.
        :return: Result produced by extract store name.
        """
        subtitle = soup.select_one(".RedesignHero-titleWrapper .RedesignHero-subtitle")
        if subtitle:
            text = subtitle.get_text(" ", strip=True)
            if text:
                return text
        h1 = soup.select_one("h1")
        if h1:
            text = h1.get_text(" ", strip=True)
            if text:
                return text
        return None

    @staticmethod
    def _extract_address(soup: BeautifulSoup) -> tuple[str | None, str | None, str | None, str | None]:
        """Extract address.

        :param soup: Parsed HTML document.
        :return: Result produced by extract address.
        """
        address = soup.select_one("address.c-address, address[itemtype*='PostalAddress']")
        if not address:
            return None, None, None, None

        street = None
        city = None
        state = None
        zip_code = None

        street_node = address.select_one(".c-address-street-1, [itemprop='streetAddress']")
        if street_node:
            street = street_node.get_text(" ", strip=True)

        city_node = address.select_one(".c-address-city, [itemprop='addressLocality']")
        if city_node:
            city = city_node.get_text(" ", strip=True)

        state_node = address.select_one(".c-address-state, [itemprop='addressRegion']")
        if state_node:
            state = state_node.get_text(" ", strip=True).upper()

        zip_node = address.select_one(".c-address-postal-code, [itemprop='postalCode']")
        if zip_node:
            zip_code = zip_node.get_text(" ", strip=True)

        return street, city, state, zip_code

    @staticmethod
    def _extract_phone(soup: BeautifulSoup) -> str | None:
        """Extract phone.

        :param soup: Parsed HTML document.
        :return: Result produced by extract phone.
        """
        phone_node = soup.select_one("#phone-main, .Phone-link[href^='tel:'], a[href^='tel:']")
        if phone_node:
            text = phone_node.get_text(" ", strip=True)
            if text:
                return text
        return None

    def _normalize_href(self, href: str) -> str:
        """Normalize href.

        :param href: Link or URL to process.
        :return: Result produced by normalize href.
        """
        href = href.strip()
        if href.startswith("../"):
            return href
        if href.startswith("./"):
            return href
        return href

    def _dedupe_page_jobs(self, jobs: Sequence[_PageJob]) -> list[_PageJob]:
        """Deduplicate page jobs.

        :param jobs: Acquisition jobs to deduplicate.
        :return: Result produced by dedupe page jobs.
        """
        deduped: list[_PageJob] = []
        seen: set[str] = set()
        for job in jobs:
            absolute_url = urljoin(BASE_URL + "/", job.href.lstrip("/"))
            if absolute_url in seen:
                continue
            seen.add(absolute_url)
            deduped.append(job)
        return deduped

    @staticmethod
    def _compose_full_address(
        *,
        street_address: str | None,
        city: str | None,
        state: str | None,
        zip_code: str | None,
    ) -> str | None:
        """Handle compose full address.

        :param street_address: Street address component.
        :param city: City entry to process.
        :param state: State name or abbreviation.
        :param zip_code: Postal code component.
        :return: Result produced by compose full address.
        """
        if not any([street_address, city, state, zip_code]):
            return None
        parts: list[str] = []
        if street_address:
            parts.append(street_address)
        locality: list[str] = []
        if city:
            locality.append(city)
        if state:
            locality.append(state)
        location = ", ".join(locality)
        if zip_code:
            location = f"{location} {zip_code}".strip()
        if location:
            parts.append(location)
        return ", ".join(parts) if parts else None

    @staticmethod
    def _clean_text(value: Any) -> str | None:
        """Normalize text.

        :param value: Value to normalize or convert.
        :return: Result produced by clean text.
        """
        if value is None:
            return None
        if hasattr(value, "get_text"):
            value = value.get_text(" ", strip=True)
        text = str(value).strip()
        return text or None

    def _utc_now(self) -> str:
        """Handle utc now.

        :return: Result produced by utc now.
        """
        return datetime.now(timezone.utc).isoformat()