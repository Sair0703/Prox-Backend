from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin, urlparse, parse_qs
import re
import time

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

from playwright.sync_api import sync_playwright

from services.store_service.capabilities.store_location_acquisition.protocals import (
    AcquisitionArtifact,
    AcquisitionSourceInfo,
    AcquisitionValidationResult,
    StoreLocationAcquisitionStrategy,
)

BASE_URL = "https://www.ulta.com"
DIRECTORY_URL = f"{BASE_URL}/stores/directory"


@dataclass(slots=True)
class _StateSection:
    state_code: str
    section_html: str


@dataclass(slots=True)
class _StoreJob:
    state_code: str
    store_url: str


class UltaAcquisitionStrategy(StoreLocationAcquisitionStrategy):
    retailer_key = "ulta"
    retailer_name = "Ulta Beauty"

    def __init__(
        self,
        *,
        state_workers: int = 8,
        city_workers: int = 16,
        store_workers: int = 32,
        request_timeout: int = 20,
        max_retries: int = 3,
    ) -> None:
        """Initialize acquisition configuration and run state.

        :param state_workers: State workers.
        :param city_workers: City workers.
        :param store_workers: Store workers.
        :param request_timeout: Request timeout.
        :param max_retries: Max retries.
        :return: Result produced by init  .
        """
        self.state_workers = state_workers
        self.city_workers = city_workers
        self.store_workers = store_workers
        self.request_timeout = request_timeout
        self.max_retries = max_retries
        self._failed_store_pages: list[dict[str, Any]] = []

    def discover_source(self) -> AcquisitionSourceInfo:
        """Return metadata describing the retailer's official acquisition source.

        :return: Metadata describing the acquisition source.
        """
        return AcquisitionSourceInfo(
            retailer_key=self.retailer_key,
            retailer_name=self.retailer_name,
            official_website_url="https://www.ulta.com/",
            store_locator_url=DIRECTORY_URL,
            endpoint_url=DIRECTORY_URL,
            source_type="html",
            provider="Playwright + BeautifulSoup",
            notes=(
                "Ulta Beauty store locator is hierarchical HTML: directory -> "
                "state sections -> store detail pages. Store id is recovered "
                "from the Book Appointment URL on the store detail page when available."
            ),
        )

    def fetch_raw_artifacts(self) -> list[AcquisitionArtifact]:
        """Fetch raw artifacts required for store location acquisition.

        :return: Raw acquisition artifacts.
        """
        self._failed_store_pages = []

        session = requests.Session()
        session.headers.update(
            {
                "user-agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/151.0.0.0 Safari/537.36"
                )
            }
        )

        artifacts: list[AcquisitionArtifact] = []

        directory_html = self._render_directory_html_with_playwright(DIRECTORY_URL)
        artifacts.append(
            AcquisitionArtifact(
                artifact_type="html",
                source_url=DIRECTORY_URL,
                content=directory_html,
                metadata={
                    "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                    "page_type": "directory",
                    "http_status": 200,
                },
            )
        )

        state_sections = self._parse_state_sections(directory_html)
        state_pbar = tqdm(
            total=len(state_sections),
            desc="Ulta state sections",
            unit="section",
        )

        try:
            store_jobs: list[_StoreJob] = []
            with ThreadPoolExecutor(max_workers=self.state_workers) as pool:
                futures = {
                    pool.submit(self._parse_store_jobs_from_section, section): section
                    for section in state_sections
                }
                for future in as_completed(futures):
                    store_jobs.extend(future.result())
                    state_pbar.update(1)

            store_jobs = self._dedupe_store_jobs(store_jobs)

            store_pbar = tqdm(
                total=len(store_jobs),
                desc="Ulta store pages",
                unit="store",
            )
            try:
                with ThreadPoolExecutor(max_workers=self.city_workers) as pool:
                    futures = {
                        pool.submit(
                            self._fetch_store_page,
                            session,
                            job.store_url,
                            job.state_code,
                        ): job
                        for job in store_jobs
                    }

                    for future in as_completed(futures):
                        artifact = future.result()
                        artifacts.append(artifact)
                        store_pbar.update(1)
            finally:
                store_pbar.close()

        finally:
            state_pbar.close()
            session.close()

        return artifacts

    def extract_store_payloads(
        self,
        artifacts: Sequence[AcquisitionArtifact],
    ) -> list[dict[str, Any]]:
        """Extract normalized store payloads from acquired artifacts.

        :param artifacts: Acquisition artifacts to process.
        :return: Normalized and deduplicated store payloads.
        """
        store_artifacts = [
            artifact
            for artifact in artifacts
            if artifact.metadata.get("page_type") == "store"
            and artifact.metadata.get("scrape_status") == "success"
        ]

        payloads_by_store_id: dict[str, dict[str, Any]] = {}
        parse_pbar = tqdm(
            total=len(store_artifacts),
            desc="Parsing Ulta stores",
            unit="store",
        )

        try:
            with ThreadPoolExecutor(max_workers=self.store_workers) as pool:
                futures = {
                    pool.submit(self._parse_store_artifact, artifact): artifact
                    for artifact in store_artifacts
                }

                for future in as_completed(futures):
                    row = future.result()
                    store_id = self._clean_text(row.get("retailer_store_id"))
                    if not store_id:
                        continue
                    payloads_by_store_id[store_id] = row
                    parse_pbar.update(1)
        finally:
            parse_pbar.close()

        return list(payloads_by_store_id.values())

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
            if not self._clean_text(row.get("address"))
            or not self._clean_text(row.get("city"))
            or not self._clean_text(row.get("state"))
            or not self._clean_text(row.get("zip_code"))
        )

        missing_phones = sum(
            1 for row in payloads if not self._clean_text(row.get("phone"))
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
        if self._failed_store_pages:
            issue_counts["failed_store_pages"] = len(self._failed_store_pages)

        notes = [
            "Directory page is used only to discover state sections and store URLs.",
            "Store detail pages provide the canonical street address and phone.",
            "Store id is recovered from the Book Appointment URL when available, with fallback to the store page slug.",
            f"Workers: state={self.state_workers}, city={self.city_workers}, store={self.store_workers}",
        ]

        is_valid = (
            total_records > 0
            and missing_store_ids == 0
            and missing_addresses == 0
            and len(self._failed_store_pages) == 0
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
            f"Source: {DIRECTORY_URL}",
            "Method: Playwright + BeautifulSoup",
            "Hierarchy: directory -> state sections -> store detail pages",
            "Parallelism: state section parsing + store detail fetching + store detail parsing",
            "Dedup key: retailer_store_id from Book Appointment URL /stores slug fallback",
        ]

    def _render_directory_html_with_playwright(self, url: str) -> str:
        """Render directory html with playwright.

        :param url: URL to fetch or process.
        :return: Result produced by render directory html with playwright.
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/151.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1440, "height": 1600},
            )
            page = context.new_page()

            try:
                response = page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self.request_timeout * 1000,
                )
                status = response.status if response is not None else "unknown"
                print(f"[Ulta] initial response status: {status}")

                max_wait_seconds = 30
                for second in range(1, max_wait_seconds + 1):
                    loading_count = page.locator('[class*="LoadingWrapper"]').count()
                    directory_count = page.locator('[class*="StoreDirectoryList"]').count()
                    wrapper_count = page.locator('[class*="LocationSectionWrapper"]').count()
                    h2_count = page.locator("h2[id]").count()
                    actual_store_links = page.locator(
                        'a[href^="/stores/"]:not([href^="/stores/directory"])'
                    ).count()

                    print(
                        f"[Ulta] t={second:02d}s | "
                        f"loading={loading_count} | "
                        f"directory={directory_count} | "
                        f"wrappers={wrapper_count} | "
                        f"h2[id]={h2_count} | "
                        f"store_links={actual_store_links}"
                    )

                    if directory_count > 0 and wrapper_count > 1 and actual_store_links > 100:
                        print("[Ulta] directory appears fully rendered.")
                        break

                    page.wait_for_timeout(1000)
                else:
                    raise RuntimeError(
                        "Ulta directory did not become ready within "
                        f"{max_wait_seconds}s."
                    )

                return page.content()

            finally:
                context.close()
                browser.close()

    def _fetch_text(self, session: requests.Session, url: str) -> str:
        """Fetch text.

        :param session: HTTP session used for requests.
        :param url: URL to fetch or process.
        :return: Result produced by fetch text.
        """
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = session.get(url, timeout=self.request_timeout)
                response.raise_for_status()
                return response.text
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries - 1:
                    time.sleep(0.8 * (2**attempt))
        raise RuntimeError(f"Failed to fetch {url}: {last_error}") from last_error

    def _parse_state_sections(self, html: str) -> list[_StateSection]:
        """Parse state sections.

        :param html: HTML content to parse.
        :return: Result produced by parse state sections.
        """
        soup = BeautifulSoup(html, "html.parser")
        sections: list[_StateSection] = []

        directory = soup.select_one('[class*="StoreDirectoryList"]')
        if directory is None:
            raise RuntimeError("Ulta StoreDirectoryList not found in rendered HTML.")

        wrappers = directory.select('[class*="LocationSectionWrapper"]')
        if len(wrappers) <= 1:
            raise RuntimeError(f"Expected multiple Ulta state sections, found {len(wrappers)}.")

        # First wrapper is the "State" index. Skip it.
        for wrapper in wrappers[1:]:
            heading = wrapper.select_one("h2")
            if heading is None:
                continue

            state_code = self._clean_text(heading.get("id"))
            if not state_code:
                continue

            store_links = wrapper.select('a[href^="/stores/"]')
            if not store_links:
                continue

            sections.append(
                _StateSection(
                    state_code=state_code.upper(),
                    section_html=str(wrapper),
                )
            )

        seen: set[str] = set()
        deduped: list[_StateSection] = []
        for section in sections:
            if section.state_code in seen:
                continue
            seen.add(section.state_code)
            deduped.append(section)

        if not deduped:
            raise RuntimeError("Ulta directory rendered successfully, but no state sections were parsed.")

        return deduped

    def _parse_store_jobs_from_section(self, section: _StateSection) -> list[_StoreJob]:
        """Parse store jobs from section.

        :param section: Section.
        :return: Result produced by parse store jobs from section.
        """
        soup = BeautifulSoup(section.section_html, "html.parser")
        jobs: list[_StoreJob] = []

        for a in soup.select('a[href^="/stores/"]'):
            href = self._clean_text(a.get("href"))
            if not href:
                continue
            if not href.startswith("/stores/"):
                continue

            jobs.append(
                _StoreJob(
                    state_code=section.state_code,
                    store_url=urljoin(BASE_URL, href),
                )
            )

        return jobs

    def _dedupe_store_jobs(self, jobs: Sequence[_StoreJob]) -> list[_StoreJob]:
        """Deduplicate store jobs.

        :param jobs: Acquisition jobs to process or deduplicate.
        :return: Result produced by dedupe store jobs.
        """
        deduped: list[_StoreJob] = []
        seen: set[str] = set()
        for job in jobs:
            if job.store_url in seen:
                continue
            seen.add(job.store_url)
            deduped.append(job)
        return deduped

    def _fetch_store_page(
        self,
        session: requests.Session,
        store_url: str,
        state_code: str,
    ) -> AcquisitionArtifact:
        """Fetch store page.

        :param session: HTTP session used for requests.
        :param store_url: Canonical store-detail URL.
        :param state_code: State code associated with the page or record.
        :return: Result produced by fetch store page.
        """
        try:
            html = self._fetch_text(session, store_url)
            return AcquisitionArtifact(
                artifact_type="html",
                source_url=store_url,
                content=html,
                metadata={
                    "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                    "page_type": "store",
                    "state_code": state_code,
                    "http_status": 200,
                    "scrape_status": "success",
                },
            )
        except Exception as exc:
            self._failed_store_pages.append(
                {
                    "store_url": store_url,
                    "state_code": state_code,
                    "error": str(exc),
                }
            )
            return AcquisitionArtifact(
                artifact_type="html",
                source_url=store_url,
                content="",
                metadata={
                    "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                    "page_type": "store",
                    "state_code": state_code,
                    "http_status": 500,
                    "scrape_status": "failed",
                    "error": str(exc),
                },
            )

    def _parse_store_artifact(self, artifact: AcquisitionArtifact) -> dict[str, Any]:
        """Parse store artifact.

        :param artifact: Acquisition artifact to parse.
        :return: Result produced by parse store artifact.
        """
        html = artifact.content or ""
        soup = BeautifulSoup(html, "html.parser")

        store_url = self._clean_text(artifact.source_url)
        state_code = self._clean_text(artifact.metadata.get("state_code"))
        title = self._clean_text(soup.select_one("h1.StyledLocationTitle-sc-1ojht7j"))
        if title is None:
            title = self._clean_text(soup.select_one("h1"))

        address = self._extract_address(soup)
        phone = self._extract_phone(soup)
        store_id = self._extract_store_id_from_appointment_url(soup)
        if not store_id:
            store_id = self._extract_store_id_from_store_url(store_url)

        city = address.get("city")
        parsed_state = address.get("state") or state_code
        store_name = title

        return {
            "retailer": self.retailer_name,
            "retailer_store_id": store_id,
            "store_number": store_id,
            "store_type": "Regular",
            "store_name": store_name,
            "address": address.get("street_address"),
            "street_address": address.get("street_address"),
            "city": city,
            "state": parsed_state,
            "address_city": city,
            "address_state": parsed_state,
            "zip_code": address.get("zip_code"),
            "full_address": address.get("full_address"),
            "phone": phone,
            "store_url": store_url,
            "source_url": store_url,
            "source_sitemap": None,
            "extraction_source": "HTML / BeautifulSoup",
            "scrape_status": "success",
            "http_status": artifact.metadata.get("http_status"),
            "error_message": None,
            "scraped_at_utc": artifact.metadata.get("retrieved_at_utc"),
        }

    @staticmethod
    def _extract_store_id_from_appointment_url(soup: BeautifulSoup) -> str | None:
        """Extract store id from appointment url.

        :param soup: Parsed HTML document.
        :return: Result produced by extract store id from appointment url.
        """
        for a in soup.select('a[href*="/beautyservices/appointment/s/"]'):
            href = a.get("href") or ""
            match = re.search(r"/beautyservices/appointment/s/(\d+)", href)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _extract_store_id_from_store_url(store_url: str | None) -> str | None:
        """Extract store id from store url.

        :param store_url: Canonical store-detail URL.
        :return: Result produced by extract store id from store url.
        """
        if not store_url:
            return None
        path = urlparse(store_url).path.strip("/")
        if not path:
            return None
        slug = path.split("/")[-1]
        match = re.search(r"-(\d+)$", slug)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _extract_address(soup: BeautifulSoup) -> dict[str, str | None]:
        """Extract address.

        :param soup: Parsed HTML document.
        :return: Result produced by extract address.
        """
        node = soup.select_one('div[itemtype="http://schema.org/PostalAddress"]')
        if not node:
            return {
                "street_address": None,
                "city": None,
                "state": None,
                "zip_code": None,
                "full_address": None,
            }

        street_node = node.select_one('[itemprop="streetAddress"]')
        city_node = node.select_one('[itemprop="addressLocality"]')
        state_node = node.select_one('[itemprop="addressRegion"]')
        zip_node = node.select_one('[itemprop="postalCode"]')

        street_address = None
        if street_node:
            street_address = street_node.get_text(" ", strip=True)
            street_address = re.sub(r"\s+", " ", street_address).strip() or None

        city = city_node.get_text(" ", strip=True) if city_node else None
        state = state_node.get_text(" ", strip=True) if state_node else None
        zip_code = zip_node.get_text(" ", strip=True) if zip_node else None

        if city:
            city = re.sub(r"\s+", " ", city).strip() or None
        if state:
            state = re.sub(r"\s+", " ", state).strip().upper() or None
        if zip_code:
            zip_code = re.sub(r"\s+", " ", zip_code).strip() or None

        full_address = UltaAcquisitionStrategy._compose_full_address(
            street_address=street_address,
            city=city,
            state=state,
            zip_code=zip_code,
        )

        return {
            "street_address": street_address,
            "city": city,
            "state": state,
            "zip_code": zip_code,
            "full_address": full_address,
        }

    @staticmethod
    def _extract_phone(soup: BeautifulSoup) -> str | None:
        """Extract phone.

        :param soup: Parsed HTML document.
        :return: Result produced by extract phone.
        """
        phone = soup.select_one('a[href^="tel:"]')
        if not phone:
            return None
        text = phone.get_text(" ", strip=True)
        return text or None

    @staticmethod
    def _compose_full_address(
        *,
        street_address: str | None,
        city: str | None,
        state: str | None,
        zip_code: str | None,
    ) -> str | None:
        """Handle compose full address.

        :param street_address: Street-address component.
        :param city: City or locality component.
        :param state: State name or abbreviation.
        :param zip_code: Postal-code component.
        :return: Result produced by compose full address.
        """
        if not any([street_address, city, state, zip_code]):
            return None

        location_bits: list[str] = []
        if city:
            location_bits.append(city)
        if state:
            location_bits.append(state)

        location = ", ".join(location_bits)
        if zip_code:
            location = f"{location} {zip_code}".strip()

        parts = [part for part in [street_address, location] if part]
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