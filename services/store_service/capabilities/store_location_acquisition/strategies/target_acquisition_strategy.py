from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from queue import Queue
from threading import Lock, Thread, local
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin, urlparse
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

BASE_URL = "https://www.target.com"
DIRECTORY_URL = f"{BASE_URL}/store-locator/store-directory"
STORE_PATH_RE = re.compile(r"^/sl/(?P<slug>[^/]+)/(?P<store_id>\d+)/?$", re.I)
CITY_STATE_ZIP_RE = re.compile(
    r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$"
)


@dataclass(frozen=True, slots=True)
class _StateJob:
    state_name: str
    state_slug: str
    url: str


@dataclass(frozen=True, slots=True)
class _MultiCityJob:
    state_name: str
    state_slug: str
    state_url: str
    city_name: str
    expected_store_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _StoreJob:
    store_id: str
    store_url: str
    city_slug: str | None = None
    state_slug: str | None = None
    city_name: str | None = None


class TargetAcquisitionStrategy(StoreLocationAcquisitionStrategy):
    retailer_key = "target"
    retailer_name = "Target"

    def __init__(
        self,
        *,
        state_workers: int = 24,
        playwright_workers: int = 8,
        store_workers: int = 64,
        parse_workers: int = 32,
        request_timeout: int = 25,
        page_timeout_ms: int = 30_000,
        render_wait_ms: int = 500,
        max_retries: int = 3,
    ) -> None:
        """Initialize acquisition configuration and run state.

        :param state_workers: State workers.
        :param playwright_workers: Playwright workers.
        :param store_workers: Store workers.
        :param parse_workers: Parse workers.
        :param request_timeout: Request timeout.
        :param page_timeout_ms: Page timeout ms.
        :param render_wait_ms: Render wait ms.
        :param max_retries: Max retries.
        :return: Result produced by init  .
        """
        self.state_workers = state_workers
        self.playwright_workers = playwright_workers
        self.store_workers = store_workers
        self.parse_workers = parse_workers
        self.request_timeout = request_timeout
        self.page_timeout_ms = page_timeout_ms
        self.render_wait_ms = render_wait_ms
        self.max_retries = max_retries

        self._thread_local = local()
        self._failed_states: list[dict[str, Any]] = []
        self._failed_multi_cities: list[dict[str, Any]] = []
        self._failed_store_pages: list[dict[str, Any]] = []

    def discover_source(self) -> AcquisitionSourceInfo:
        """Return metadata describing the retailer's official acquisition source.

        :return: Metadata describing the acquisition source.
        """
        return AcquisitionSourceInfo(
            retailer_key=self.retailer_key,
            retailer_name=self.retailer_name,
            official_website_url="https://www.target.com/",
            store_locator_url=DIRECTORY_URL,
            endpoint_url=DIRECTORY_URL,
            source_type="html",
            provider="Target store directory / requests + Playwright + BeautifulSoup",
            notes=(
                "Target directory hierarchy: directory -> state pages. Single-store cities "
                "contain canonical /sl/<slug>/<store_id> links directly. Multi-store cities "
                "contain data-city/data-ids and are expanded with Playwright to discover the "
                "canonical Store info links. Store detail pages provide canonical address and phone."
            ),
        )

    def fetch_raw_artifacts(self) -> list[AcquisitionArtifact]:
        """Fetch raw artifacts required for store location acquisition.

        :return: Raw acquisition artifacts.
        """
        self._failed_states = []
        self._failed_multi_cities = []
        self._failed_store_pages = []
        artifacts: list[AcquisitionArtifact] = []

        directory_html = self._fetch_text(DIRECTORY_URL)
        artifacts.append(self._artifact(DIRECTORY_URL, directory_html, "directory"))
        state_jobs = self._parse_state_jobs(directory_html)
        if not state_jobs:
            raise RuntimeError("Target directory returned no state links.")

        state_artifacts: list[AcquisitionArtifact] = []
        with ThreadPoolExecutor(max_workers=self.state_workers) as pool:
            futures = {pool.submit(self._fetch_state, job): job for job in state_jobs}
            for future in tqdm(as_completed(futures), total=len(futures), desc="Target states", unit="state"):
                artifact = future.result()
                artifacts.append(artifact)
                if artifact.metadata.get("scrape_status") == "success":
                    state_artifacts.append(artifact)

        direct_store_jobs: list[_StoreJob] = []
        multi_city_jobs: list[_MultiCityJob] = []
        for artifact in state_artifacts:
            stores, multi = self._parse_state_artifact(artifact)
            direct_store_jobs.extend(stores)
            multi_city_jobs.extend(multi)

        direct_store_jobs = self._dedupe_store_jobs(direct_store_jobs)
        multi_city_jobs = self._dedupe_multi_city_jobs(multi_city_jobs)
        print(f"[Target] direct store links: {len(direct_store_jobs)}")
        print(f"[Target] multi-store cities: {len(multi_city_jobs)}")

        sidebar_store_jobs = self._discover_multi_city_store_jobs(multi_city_jobs)
        all_store_jobs = self._dedupe_store_jobs([*direct_store_jobs, *sidebar_store_jobs])
        if not all_store_jobs:
            raise RuntimeError("Target state discovery completed but no store URLs were found.")
        print(f"[Target] unique stores discovered: {len(all_store_jobs)}")

        with ThreadPoolExecutor(max_workers=self.store_workers) as pool:
            futures = {pool.submit(self._fetch_store, job): job for job in all_store_jobs}
            for future in tqdm(as_completed(futures), total=len(futures), desc="Target store pages", unit="store"):
                artifacts.append(future.result())

        return artifacts

    def extract_store_payloads(self, artifacts: Sequence[AcquisitionArtifact]) -> list[dict[str, Any]]:
        """Extract normalized store payloads from acquired artifacts.

        :param artifacts: Acquisition artifacts to process.
        :return: Normalized and deduplicated store payloads.
        """
        store_artifacts = [
            a for a in artifacts
            if a.metadata.get("page_type") == "store"
            and a.metadata.get("scrape_status") == "success"
        ]
        rows: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=self.parse_workers) as pool:
            futures = {pool.submit(self._parse_store_artifact, a): a for a in store_artifacts}
            for future in tqdm(as_completed(futures), total=len(futures), desc="Parsing Target stores", unit="store"):
                row = future.result()
                if row and row.get("retailer_store_id"):
                    rows[str(row["retailer_store_id"])] = row
        return list(rows.values())

    def validate_store_payloads(self, payloads: Sequence[Mapping[str, Any]]) -> AcquisitionValidationResult:
        """Validate acquired store payloads for completeness and uniqueness.

        :param payloads: Normalized store payloads to validate.
        :return: Validation result for the acquired payloads.
        """
        ids = [self._clean(row.get("retailer_store_id")) for row in payloads]
        seen: set[str] = set()
        duplicates: list[str] = []
        for store_id in ids:
            if not store_id:
                continue
            if store_id in seen and store_id not in duplicates:
                duplicates.append(store_id)
            seen.add(store_id)

        missing_ids = sum(not x for x in ids)
        missing_addresses = sum(
            1 for row in payloads
            if not self._clean(row.get("street_address"))
            or not self._clean(row.get("city"))
            or not self._clean(row.get("state"))
            or not self._clean(row.get("zip_code"))
        )
        missing_phones = sum(1 for row in payloads if not self._clean(row.get("phone")))
        issues: dict[str, int] = {}
        for name, value in (
            ("missing_store_ids", missing_ids),
            ("missing_addresses", missing_addresses),
            ("missing_phones", missing_phones),
            ("failed_states", len(self._failed_states)),
            ("failed_multi_cities", len(self._failed_multi_cities)),
            ("failed_store_pages", len(self._failed_store_pages)),
        ):
            if value:
                issues[name] = value

        valid = (
            len(payloads) > 0
            and missing_ids == 0
            and missing_addresses == 0
            and not duplicates
            and not self._failed_states
            and not self._failed_multi_cities
            and not self._failed_store_pages
        )
        return AcquisitionValidationResult(
            is_valid=valid,
            total_records=len(payloads),
            unique_store_ids=len(seen),
            missing_store_ids=missing_ids,
            missing_coordinates=0,
            non_us_records=0,
            duplicate_store_ids=duplicates,
            issue_counts=issues,
            notes=[
                "Single-store cities are discovered directly from /sl/<slug>/<store_id> links.",
                "Multi-store cities are expanded only for URL discovery; sidebar Store info links are canonical.",
                "Canonical store fields are parsed from each Target store detail page.",
                f"Workers: state={self.state_workers}, playwright={self.playwright_workers}, store={self.store_workers}, parse={self.parse_workers}",
            ],
        )

    def build_run_notes(self) -> list[str]:
        """Return acquisition source and execution details for the run summary.

        :return: Notes describing the acquisition run.
        """
        return [
            f"Source: {DIRECTORY_URL}",
            "Method: requests + BeautifulSoup; Playwright only for multi-store city expansion",
            "Hierarchy: directory -> state -> direct store links / multi-city sidebar -> store detail",
            "Dedup key: numeric store id from /sl/<slug>/<store_id>",
            "CSV output is written by StoreLocationAcquisitionService under output/target/<run_id>/.",
        ]

    def _get_session(self) -> requests.Session:
        """Return session.

        :return: Result produced by get session.
        """
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            })
            self._thread_local.session = session
        return session

    def _fetch_text(self, url: str) -> str:
        """Fetch text.

        :param url: URL to fetch or process.
        :return: Result produced by fetch text.
        """
        last: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                r = self._get_session().get(url, timeout=self.request_timeout)
                r.raise_for_status()
                if not r.text:
                    raise RuntimeError("empty response")
                return r.text
            except Exception as exc:
                last = exc
                if attempt + 1 < self.max_retries:
                    time.sleep(0.6 * (2 ** attempt))
        raise RuntimeError(f"Failed to fetch {url}: {last}") from last

    def _fetch_state(self, job: _StateJob) -> AcquisitionArtifact:
        """Fetch state.

        :param job: Acquisition job to process.
        :return: Result produced by fetch state.
        """
        try:
            html = self._fetch_text(job.url)
            return self._artifact(job.url, html, "state", state_name=job.state_name, state_slug=job.state_slug)
        except Exception as exc:
            self._failed_states.append({"url": job.url, "state": job.state_name, "error": str(exc)})
            return self._artifact(job.url, "", "state", status="failed", error=str(exc), state_name=job.state_name, state_slug=job.state_slug)

    def _fetch_store(self, job: _StoreJob) -> AcquisitionArtifact:
        """Fetch store.

        :param job: Acquisition job to process.
        :return: Result produced by fetch store.
        """
        try:
            html = self._fetch_text(job.store_url)
            return self._artifact(job.store_url, html, "store", store_id=job.store_id, city_slug=job.city_slug, state_slug=job.state_slug, city_name=job.city_name)
        except Exception as exc:
            self._failed_store_pages.append({"store_id": job.store_id, "url": job.store_url, "error": str(exc)})
            return self._artifact(job.store_url, "", "store", status="failed", error=str(exc), store_id=job.store_id, city_slug=job.city_slug, state_slug=job.state_slug, city_name=job.city_name)

    def _artifact(self, url: str, content: str, page_type: str, *, status: str = "success", error: str | None = None, **metadata: Any) -> AcquisitionArtifact:
        """Handle artifact.

        :param url: URL to fetch or process.
        :param content: Artifact content.
        :param page_type: Acquisition page type.
        :param status: Artifact scrape status.
        :param error: Acquisition error, when present.
        :return: Result produced by artifact.
        """
        return AcquisitionArtifact(
            artifact_type="html",
            source_url=url,
            content=content,
            metadata={
                "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                "page_type": page_type,
                "http_status": 200 if status == "success" else 500,
                "scrape_status": status,
                "error": error,
                **metadata,
            },
        )

    def _parse_state_jobs(self, html: str) -> list[_StateJob]:
        """Parse state jobs.

        :param html: HTML content to parse.
        :return: Result produced by parse state jobs.
        """
        soup = BeautifulSoup(html, "html.parser")
        jobs: list[_StateJob] = []
        for a in soup.select('a[href^="/store-locator/store-directory/"]'):
            href = self._clean(a.get("href"))
            name = self._clean(a.get_text(" ", strip=True))
            if not href or not name:
                continue
            slug = urlparse(href).path.rstrip("/").split("/")[-1]
            jobs.append(_StateJob(name, slug, urljoin(BASE_URL, href)))
        return list({j.url: j for j in jobs}.values())

    def _parse_state_artifact(self, artifact: AcquisitionArtifact) -> tuple[list[_StoreJob], list[_MultiCityJob]]:
        """Parse state artifact.

        :param artifact: Acquisition artifact to parse.
        :return: Result produced by parse state artifact.
        """
        soup = BeautifulSoup(artifact.content or "", "html.parser")
        state_name = self._clean(artifact.metadata.get("state_name")) or ""
        state_slug = self._clean(artifact.metadata.get("state_slug")) or ""
        stores: list[_StoreJob] = []
        multi: list[_MultiCityJob] = []

        for a in soup.select('a[href^="/sl/"]'):
            href = self._clean(a.get("href"))
            if not href:
                continue
            match = STORE_PATH_RE.match(urlparse(href).path)
            if not match:
                continue
            stores.append(_StoreJob(match.group("store_id"), urljoin(BASE_URL, href), match.group("slug"), state_slug, self._clean(a.get_text(" ", strip=True))))

        for node in soup.select('[data-city][data-ids]'):
            city = self._clean(node.get("data-city"))
            raw_ids = self._clean(node.get("data-ids"))
            if not city or not raw_ids:
                continue
            ids = tuple(x.strip() for x in raw_ids.split(",") if x.strip().isdigit())
            if ids:
                multi.append(_MultiCityJob(state_name, state_slug, artifact.source_url, city, ids))
        return self._dedupe_store_jobs(stores), multi

    def _discover_multi_city_store_jobs(self, jobs: Sequence[_MultiCityJob]) -> list[_StoreJob]:
        """Discover multi city store jobs.

        :param jobs: Acquisition jobs to process or deduplicate.
        :return: Result produced by discover multi city store jobs.
        """
        if not jobs:
            return []
        job_queue: Queue[_MultiCityJob | None] = Queue()
        result_queue: Queue[list[_StoreJob]] = Queue()
        lock = Lock()
        pbar = tqdm(total=len(jobs), desc="Target multi-store cities", unit="city")
        for job in jobs:
            job_queue.put(job)
        worker_count = min(self.playwright_workers, len(jobs))
        for _ in range(worker_count):
            job_queue.put(None)

        def worker() -> None:
            """Handle worker.

            :return: Result produced by worker.
            """
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                context = browser.new_context(
                    viewport={"width": 1440, "height": 1200},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
                    locale="en-US",
                )
                page = context.new_page()
                try:
                    while True:
                        job = job_queue.get()
                        if job is None:
                            return
                        found: list[_StoreJob] = []
                        try:
                            page.goto(job.state_url, wait_until="domcontentloaded", timeout=self.page_timeout_ms)
                            selector = f'[data-city="{self._css_escape(job.city_name)}"] button'
                            button = page.locator(selector)
                            if button.count() == 0:
                                button = page.get_by_role("button", name=job.city_name, exact=True)
                            button.first.click(timeout=self.page_timeout_ms)
                            page.wait_for_selector('[data-test="@store-locator/StoreInfoLink"]', timeout=self.page_timeout_ms)
                            page.wait_for_timeout(self.render_wait_ms)
                            html = page.content()
                            soup = BeautifulSoup(html, "html.parser")
                            for a in soup.select('[data-test="@store-locator/StoreInfoLink"][href^="/sl/"]'):
                                href = self._clean(a.get("href"))
                                if not href:
                                    continue
                                match = STORE_PATH_RE.match(urlparse(href).path)
                                if match and match.group("store_id") in job.expected_store_ids:
                                    found.append(_StoreJob(match.group("store_id"), urljoin(BASE_URL, href), match.group("slug"), job.state_slug, job.city_name))
                            found_ids = {x.store_id for x in found}
                            missing = set(job.expected_store_ids) - found_ids
                            if missing:
                                raise RuntimeError(f"expected ids {job.expected_store_ids}, missing {sorted(missing)}, found {sorted(found_ids)}")
                        except Exception as exc:
                            self._failed_multi_cities.append({"state": job.state_name, "city": job.city_name, "ids": list(job.expected_store_ids), "error": str(exc)})
                        finally:
                            result_queue.put(found)
                            with lock:
                                pbar.update(1)
                finally:
                    context.close()
                    browser.close()

        threads = [Thread(target=worker, daemon=True) for _ in range(worker_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        pbar.close()

        output: list[_StoreJob] = []
        while not result_queue.empty():
            output.extend(result_queue.get())
        return self._dedupe_store_jobs(output)

    def _parse_store_artifact(self, artifact: AcquisitionArtifact) -> dict[str, Any] | None:
        """Parse store artifact.

        :param artifact: Acquisition artifact to parse.
        :return: Result produced by parse store artifact.
        """
        soup = BeautifulSoup(artifact.content or "", "html.parser")
        root = soup.select_one('[data-test="@store-locator/StoreInfo"]')
        if root is None:
            return None

        store_url = artifact.source_url
        match = STORE_PATH_RE.match(urlparse(store_url).path)
        store_id = self._clean(artifact.metadata.get("store_id")) or (match.group("store_id") if match else None)
        city_slug = self._clean(artifact.metadata.get("city_slug")) or (match.group("slug") if match else None)
        heading = root.select_one("h1")
        store_name = self._clean(heading.get_text(" ", strip=True)) if heading else None

        paragraphs = [self._clean(p.get_text(" ", strip=True)) for p in root.select(".styles_info__JbkmV p, div[class*='info'] > p")]
        paragraphs = [x for x in paragraphs if x]
        street = paragraphs[0] if paragraphs else None
        city = state = zip_code = None
        phone = None
        for text in paragraphs[1:]:
            m = CITY_STATE_ZIP_RE.match(text)
            if m:
                city, state, zip_code = m.group("city"), m.group("state"), m.group("zip")
                continue
            if text.lower().startswith("phone:"):
                phone = self._clean(text.split(":", 1)[1])

        # Stable fallbacks if CSS module class names change.
        if phone is None:
            tel = root.select_one('a[href^="tel:"]')
            if tel:
                phone = self._clean(tel.get("href", "").replace("tel:", "", 1))
        if not (street and city and state and zip_code):
            text_lines = [self._clean(x) for x in root.stripped_strings]
            text_lines = [x for x in text_lines if x]
            for i, text in enumerate(text_lines):
                m = CITY_STATE_ZIP_RE.match(text)
                if m:
                    city, state, zip_code = m.group("city"), m.group("state"), m.group("zip")
                    if not street and i > 0:
                        street = text_lines[i - 1]
                    break

        full_address = ", ".join(x for x in [street, f"{city}, {state} {zip_code}" if city and state and zip_code else None] if x) or None
        return {
            "retailer": self.retailer_name,
            "retailer_store_id": store_id,
            "store_number": store_id,
            "store_type": "Regular",
            "store_name": store_name,
            "address": street,
            "street_address": street,
            "city": city,
            "address_city": city,
            "state": state,
            "address_state": state,
            "zip_code": zip_code,
            "full_address": full_address,
            "phone": phone,
            "city_slug": city_slug,
            "store_url": store_url,
            "source_url": store_url,
            "source_sitemap": DIRECTORY_URL,
            "extraction_source": "Target official store directory / store detail page",
            "scrape_status": "success",
            "http_status": artifact.metadata.get("http_status"),
            "error_message": None,
            "scraped_at_utc": artifact.metadata.get("retrieved_at_utc"),
        }

    @staticmethod
    def _dedupe_store_jobs(jobs: Sequence[_StoreJob]) -> list[_StoreJob]:
        """Deduplicate store jobs.

        :param jobs: Acquisition jobs to process or deduplicate.
        :return: Result produced by dedupe store jobs.
        """
        return list({job.store_id: job for job in jobs}.values())

    @staticmethod
    def _dedupe_multi_city_jobs(jobs: Sequence[_MultiCityJob]) -> list[_MultiCityJob]:
        """Deduplicate multi city jobs.

        :param jobs: Acquisition jobs to process or deduplicate.
        :return: Result produced by dedupe multi city jobs.
        """
        return list({(job.state_slug, job.city_name): job for job in jobs}.values())

    @staticmethod
    def _clean(value: Any) -> str | None:
        """Handle clean.

        :param value: Value to normalize or convert.
        :return: Result produced by clean.
        """
        if value is None:
            return None
        text = re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()
        return text or None

    @staticmethod
    def _css_escape(value: str) -> str:
        """Handle css escape.

        :param value: Value to normalize or convert.
        :return: Result produced by css escape.
        """
        return value.replace("\\", "\\\\").replace('"', '\\"')


__all__ = ["TargetAcquisitionStrategy"]