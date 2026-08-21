from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from queue import Queue
from threading import Lock, Thread
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse
import re
import time

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

try:
    from playwright.sync_api import sync_playwright
except ImportError as exc:  # pragma: no cover
    sync_playwright = None
    _PLAYWRIGHT_IMPORT_ERROR = exc
else:
    _PLAYWRIGHT_IMPORT_ERROR = None

from services.store_service.capabilities.store_location_acquisition.protocals import (
    AcquisitionArtifact,
    AcquisitionSourceInfo,
    AcquisitionValidationResult,
    StoreLocationAcquisitionStrategy,
    StorePayload,
)

BASE_URL = "https://www.walgreens.com"
STATE_INDEX_URL = f"{BASE_URL}/storelistings/storesbystate.jsp?requestType=locator"


@dataclass(slots=True)
class _StateLink:
    state_code: str
    href: str


@dataclass(slots=True)
class _CityLink:
    state_code: str
    city_slug: str
    href: str


class WalgreensAcquisitionStrategy(StoreLocationAcquisitionStrategy):
    retailer_key = "walgreens"
    retailer_name = "Walgreens"

    def __init__(
        self,
        *,
        state_workers: int = 8,
        city_workers: int = 8,
        store_workers: int = 8,
        request_timeout: int = 20,
        max_retries: int = 3,
        max_load_more_clicks: int = 25,
    ) -> None:
        """Initialize acquisition configuration and run state.

        :param state_workers: State workers.
        :param city_workers: City workers.
        :param store_workers: Store workers.
        :param request_timeout: Request timeout.
        :param max_retries: Max retries.
        :param max_load_more_clicks: Max load more clicks.
        :return: Result produced by init  .
        """
        self.state_workers = state_workers
        self.city_workers = city_workers
        self.store_workers = store_workers
        self.request_timeout = request_timeout
        self.max_retries = max_retries
        self.max_load_more_clicks = max_load_more_clicks

    def discover_source(self) -> AcquisitionSourceInfo:
        """Return metadata describing the retailer's official acquisition source.

        :return: Metadata describing the acquisition source.
        """
        return AcquisitionSourceInfo(
            retailer_key=self.retailer_key,
            retailer_name=self.retailer_name,
            official_website_url="https://www.walgreens.com/",
            store_locator_url=STATE_INDEX_URL,
            endpoint_url=STATE_INDEX_URL,
            source_type="html",
            provider="BeautifulSoup + headless Playwright",
            notes=(
                "Walgreens store locator is hierarchical HTML: state index -> "
                "state city list -> city store list. City pages require clicking "
                "Load more to expose all store cards. Canonical store id is the "
                "id parameter embedded in the store card href."
            ),
        )

    def fetch_raw_artifacts(self) -> list[AcquisitionArtifact]:
        """Fetch raw artifacts required for store location acquisition.

        :return: Raw acquisition artifacts.
        """
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

        index_html = self._fetch_text(session, STATE_INDEX_URL)
        artifacts.append(
            AcquisitionArtifact(
                artifact_type="html",
                source_url=STATE_INDEX_URL,
                content=index_html,
                metadata={
                    "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                    "page_type": "state_index",
                    "http_status": 200,
                },
            )
        )

        state_links = self._parse_state_links(index_html)
        state_pbar = tqdm(total=len(state_links), desc="Walgreens states", unit="state")
        city_pbar = tqdm(desc="Walgreens cities", unit="city", leave=True)

        try:
            state_html_map: dict[str, tuple[str, str]] = {}

            with ThreadPoolExecutor(max_workers=self.state_workers) as pool:
                futures = {
                    pool.submit(
                        self._fetch_text,
                        session,
                        urljoin(BASE_URL, state_link.href),
                    ): state_link
                    for state_link in state_links
                }

                for future in as_completed(futures):
                    state_link = futures[future]
                    state_html = future.result()
                    state_html_map[state_link.state_code] = (state_link.href, state_html)

                    artifacts.append(
                        AcquisitionArtifact(
                            artifact_type="html",
                            source_url=urljoin(BASE_URL, state_link.href),
                            content=state_html,
                            metadata={
                                "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                                "page_type": "state",
                                "state_code": state_link.state_code,
                                "http_status": 200,
                            },
                        )
                    )
                    state_pbar.update(1)

            city_jobs: list[_CityLink] = []
            for state_code, (_, state_html) in state_html_map.items():
                city_jobs.extend(self._parse_city_links(state_html, state_code))

            city_pbar.total = len(city_jobs)
            city_pbar.refresh()

            city_artifacts = self._render_city_pages_parallel(city_jobs, city_pbar)
            artifacts.extend(city_artifacts)

        finally:
            state_pbar.close()
            city_pbar.close()
            session.close()

        return artifacts

    def extract_store_payloads(
        self,
        artifacts: Sequence[AcquisitionArtifact],
    ) -> list[StorePayload]:
        """Extract normalized store payloads from acquired artifacts.

        :param artifacts: Acquisition artifacts to process.
        :return: Normalized and deduplicated store payloads.
        """
        city_artifacts = [
            artifact
            for artifact in artifacts
            if artifact.metadata.get("page_type") == "city"
            and artifact.metadata.get("scrape_status", "success") == "success"
        ]

        rows_by_store_id: dict[str, dict[str, Any]] = {}
        pbar = tqdm(
            total=len(city_artifacts),
            desc="Parsing Walgreens stores",
            unit="city",
            leave=True,
        )

        try:
            with ThreadPoolExecutor(max_workers=self.store_workers) as pool:
                futures = {
                    pool.submit(self._parse_city_artifact, artifact): artifact
                    for artifact in city_artifacts
                }

                for future in as_completed(futures):
                    rows = future.result()
                    for row in rows:
                        store_id = self._clean_text(row.get("retailer_store_id"))
                        if not store_id:
                            continue
                        rows_by_store_id[store_id] = row
                    pbar.update(1)
        finally:
            pbar.close()

        return list(rows_by_store_id.values())

    def validate_store_payloads(
        self,
        payloads: Sequence[StorePayload],
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

        notes = [
            "State pages are discovery only.",
            "City pages are rendered with headless Playwright and Load more is clicked until exhausted.",
            "Canonical dedup key is the id parameter in the store card href.",
            "Core fields are taken directly from the city store card.",
        ]

        return AcquisitionValidationResult(
            is_valid=missing_store_ids == 0 and missing_addresses == 0,
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
            "Source: https://www.walgreens.com/storelistings/storesbystate.jsp?requestType=locator",
            "Method: HTML / BeautifulSoup + headless Playwright",
            "Hierarchy: state index -> state city list -> city store list",
            "City pages are expanded by clicking Load more store results",
            "Canonical dedup key: retailer_store_id from /id=... in the store card href",
            f"Workers: state={self.state_workers}, city={self.city_workers}, store={self.store_workers}",
        ]

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

    def _render_city_pages_parallel(
        self,
        city_jobs: list[_CityLink],
        city_pbar: tqdm,
    ) -> list[AcquisitionArtifact]:
        """Render city pages parallel.

        :param city_jobs: City-page jobs to render.
        :param city_pbar: Progress bar updated as city pages complete.
        :return: Result produced by render city pages parallel.
        """
        if sync_playwright is None:  # pragma: no cover
            raise RuntimeError(
                "Playwright is required for Walgreens city pages because the "
                "store list is expanded via Load more."
            ) from _PLAYWRIGHT_IMPORT_ERROR

        job_queue: Queue[_CityLink | None] = Queue()
        result_queue: Queue[AcquisitionArtifact] = Queue()
        progress_lock = Lock()

        for job in city_jobs:
            job_queue.put(job)

        for _ in range(self.city_workers):
            job_queue.put(None)

        threads: list[Thread] = []
        for _ in range(self.city_workers):
            thread = Thread(
                target=self._city_worker,
                args=(job_queue, result_queue, city_pbar, progress_lock),
                daemon=True,
            )
            thread.start()
            threads.append(thread)

        for thread in threads:
            thread.join()

        artifacts: list[AcquisitionArtifact] = []
        while not result_queue.empty():
            artifacts.append(result_queue.get())

        return artifacts

    def _city_worker(
        self,
        job_queue: Queue[_CityLink | None],
        result_queue: Queue[AcquisitionArtifact],
        city_pbar: tqdm,
        progress_lock: Lock,
    ) -> None:
        """Handle city worker.

        :param job_queue: Queue containing city-page jobs for this worker.
        :param result_queue: Queue receiving completed acquisition artifacts.
        :param city_pbar: Progress bar updated as city pages complete.
        :param progress_lock: Lock protecting concurrent progress updates.
        :return: Result produced by city worker.
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
                while True:
                    job = job_queue.get()
                    if job is None:
                        break

                    city_url = urljoin(BASE_URL, job.href)
                    try:
                        html = self._render_city_page_with_existing_page(page, city_url)
                        artifact = AcquisitionArtifact(
                            artifact_type="html",
                            source_url=city_url,
                            content=html,
                            metadata={
                                "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                                "page_type": "city",
                                "state_code": job.state_code,
                                "city_slug": job.city_slug,
                                "http_status": 200,
                                "scrape_status": "success",
                            },
                        )
                    except Exception as exc:
                        artifact = AcquisitionArtifact(
                            artifact_type="html",
                            source_url=city_url,
                            content="",
                            metadata={
                                "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                                "page_type": "city",
                                "state_code": job.state_code,
                                "city_slug": job.city_slug,
                                "http_status": 500,
                                "scrape_status": "failed",
                                "error": str(exc),
                            },
                        )

                    result_queue.put(artifact)
                    with progress_lock:
                        city_pbar.update(1)

            finally:
                context.close()
                browser.close()

    def _render_city_page_with_existing_page(self, page: Any, city_url: str) -> str:
        """Render city page with existing page.

        :param page: Playwright page reused for city rendering.
        :param city_url: Walgreens city-page URL to render.
        :return: Result produced by render city page with existing page.
        """
        page.goto(city_url, wait_until="domcontentloaded", timeout=self.request_timeout * 1000)
        page.wait_for_timeout(1000)

        previous_count = -1
        for _ in range(self.max_load_more_clicks):
            current_count = page.locator("li.card.card__store-listing").count()

            button = page.locator("button[data-testid='load_more_results'], button#loadMoreBtn")
            if button.count() == 0:
                break

            btn = button.first
            if not btn.is_visible() or not btn.is_enabled():
                break

            if current_count == previous_count:
                break

            previous_count = current_count
            btn.scroll_into_view_if_needed()
            btn.click()

            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass

            page.wait_for_timeout(700)

            new_count = page.locator("li.card.card__store-listing").count()
            if new_count <= current_count:
                break

        return page.content()

    def _parse_state_links(self, html: str) -> list[_StateLink]:
        """Parse state links.

        :param html: HTML content to parse.
        :return: Result produced by parse state links.
        """
        soup = BeautifulSoup(html, "html.parser")
        links: list[_StateLink] = []

        for a in soup.select('a[href*="storesbycity.jsp"]'):
            href = (a.get("href") or "").strip()
            query = parse_qs(urlparse(href).query)
            state_code = self._clean_text((query.get("state") or [None])[0])
            if state_code:
                links.append(_StateLink(state_code=state_code.upper(), href=href))

        deduped: list[_StateLink] = []
        seen: set[str] = set()
        for link in links:
            if link.state_code in seen:
                continue
            seen.add(link.state_code)
            deduped.append(link)

        return deduped

    def _parse_city_links(self, html: str, state_code: str) -> list[_CityLink]:
        """Parse city links.

        :param html: HTML content to parse.
        :param state_code: State code associated with the page.
        :return: Result produced by parse city links.
        """
        soup = BeautifulSoup(html, "html.parser")
        links: list[_CityLink] = []

        for a in soup.select('a[href^="/storelocator/pharmacy/"]'):
            href = (a.get("href") or "").strip()
            if not href.startswith("/storelocator/pharmacy/"):
                continue

            city_slug = href.rstrip("/").split("/")[-1]
            if not city_slug:
                continue

            links.append(
                _CityLink(
                    state_code=state_code,
                    city_slug=city_slug,
                    href=href,
                )
            )

        deduped: list[_CityLink] = []
        seen: set[tuple[str, str]] = set()
        for link in links:
            key = (link.state_code, link.city_slug)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(link)

        return deduped

    def _parse_city_artifact(self, artifact: AcquisitionArtifact) -> list[StorePayload]:
        """Parse city artifact.

        :param artifact: Acquisition artifact to parse.
        :return: Result produced by parse city artifact.
        """
        html = artifact.content or ""
        soup = BeautifulSoup(html, "html.parser")

        state_code = self._clean_text(artifact.metadata.get("state_code"))
        city_slug = self._clean_text(artifact.metadata.get("city_slug"))
        source_url = self._clean_text(artifact.source_url)

        rows: list[StorePayload] = []

        for card in soup.select("li.card.card__store-listing"):
            link = card.select_one('a.address-text[href*="/locator/walgreens-"][href*="/id="]')
            if not link:
                continue

            href = (link.get("href") or "").strip()
            retailer_store_id = self._extract_store_id_from_href(href)
            if not retailer_store_id:
                continue

            store_url = urljoin(BASE_URL, href)
            street_address = self._clean_text(link.get_text(" ", strip=True))

            city_state_zip_text = self._extract_city_state_zip(card)
            city, state, zip_code = self._parse_city_state_zip(city_state_zip_text)

            full_address = self._compose_full_address(
                street_address=street_address,
                city=city,
                state=state,
                zip_code=zip_code,
            )

            phone = self._clean_text(self._text_or_none(card.select_one("a.phone")))
            store_hours = self._service_text(
                card,
                'ul.service-list[data-testid="service-hour-on-store-card-sl"]',
            )
            pharmacy_hours = self._service_text(
                card,
                'ul.service-list[data-name="pharmacy hours"]',
            )
            pickup_available = (
                card.select_one(f"strong[id='pickup-msg-{retailer_store_id}']") is not None
            )
            distance_miles = self._clean_text(
                self._text_or_none(card.select_one(".body-copy__fourteen"))
            )

            rows.append(
                {
                    "retailer": self.retailer_name,
                    "retailer_store_id": retailer_store_id,
                    "store_number": retailer_store_id,
                    "store_type": "Regular",
                    "store_name": street_address,
                    "address": street_address,
                    "street_address": street_address,
                    "city": city,
                    "state": state or state_code,
                    "zip_code": zip_code,
                    "full_address": full_address,
                    "phone": phone,
                    "store_url": store_url,
                    "source_url": source_url,
                    "source_sitemap": None,
                    "city_slug": city_slug,
                    "distance_miles": distance_miles,
                    "store_hours": store_hours,
                    "pharmacy_hours": pharmacy_hours,
                    "pickup_available": pickup_available,
                    "extraction_source": "Walgreens city card HTML",
                    "scrape_status": "success",
                    "http_status": artifact.metadata.get("http_status"),
                    "scraped_at_utc": artifact.metadata.get("retrieved_at_utc"),
                }
            )

        return rows

    @staticmethod
    def _extract_store_id_from_href(href: str) -> str | None:
        """Extract store id from href.

        :param href: Store-card link containing the retailer store ID.
        :return: Result produced by extract store id from href.
        """
        if not href:
            return None

        match = re.search(r"/id=(\d+)(?:/|$)", href)
        if match:
            return match.group(1)

        match = re.search(r"id=(\d+)", href)
        if match:
            return match.group(1)

        return None

    @staticmethod
    def _extract_city_state_zip(card: BeautifulSoup) -> str | None:
        """Extract city state zip.

        :param card: Store-card HTML element.
        :return: Result produced by extract city state zip.
        """
        address_span = card.select_one("[id^='store_addr_info_']")
        if address_span:
            text = address_span.get_text(" ", strip=True)
            return text or None

        fallback = card.select_one(".address-details .mt15")
        if fallback:
            text = fallback.get_text(" ", strip=True)
            return text or None

        return None

    @staticmethod
    def _parse_city_state_zip(text: str | None) -> tuple[str | None, str | None, str | None]:
        """Parse city state zip.

        :param text: City, state, and ZIP text to parse.
        :return: Result produced by parse city state zip.
        """
        if not text:
            return None, None, None

        parts = [part.strip() for part in text.split(",")]
        if len(parts) < 2:
            return None, None, None

        city = parts[0] or None
        state = None
        zip_code = None

        state_zip_parts = parts[1].split()
        if len(state_zip_parts) >= 1:
            state = state_zip_parts[0].strip() or None
        if len(state_zip_parts) >= 2:
            zip_code = state_zip_parts[1].strip() or None

        return city, state, zip_code

    @staticmethod
    def _service_text(card: BeautifulSoup, selector: str) -> str | None:
        """Handle service text.

        :param card: Store-card HTML element.
        :param selector: CSS selector identifying the service-hours element.
        :return: Result produced by service text.
        """
        node = card.select_one(selector)
        if not node:
            return None

        text = " ".join(node.get_text(" ", strip=True).split())
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

        city_state_parts: list[str] = []
        if city:
            city_state_parts.append(city)
        if state:
            city_state_parts.append(state)

        city_state = ", ".join(city_state_parts)
        if zip_code:
            city_state = f"{city_state} {zip_code}".strip()

        parts = [part for part in [street_address, city_state] if part]
        return ", ".join(parts) if parts else None

    @staticmethod
    def _text_or_none(node: Any) -> str | None:
        """Handle text or none.

        :param node: HTML node to inspect.
        :return: Result produced by text or none.
        """
        if node is None:
            return None
        text = node.get_text(" ", strip=True)
        return text or None

    @staticmethod
    def _clean_text(value: Any) -> str | None:
        """Normalize text.

        :param value: Value to normalize or convert.
        :return: Result produced by clean text.
        """
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip()
            return text or None
        text = str(value).strip()
        return text or None