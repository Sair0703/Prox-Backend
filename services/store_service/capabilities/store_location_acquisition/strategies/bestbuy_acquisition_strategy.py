from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin, urlparse
import re
import time

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

from services.store_service.capabilities.store_location_acquisition.protocals import (
    AcquisitionArtifact,
    AcquisitionSourceInfo,
    AcquisitionValidationResult,
    StoreLocationAcquisitionStrategy,
)

BASE_URL = "https://stores.bestbuy.com"
ROOT_URL = f"{BASE_URL}/"
STATE_CODE_RE = re.compile(r"^[a-z]{2}$", re.IGNORECASE)
STORE_DETAIL_PATH_RE = re.compile(r"^/[a-z]{2}/[^/]+/[^/]+-\d+\.html$", re.IGNORECASE)


@dataclass(slots=True)
class _Job:
    """Represent Job data used by the acquisition strategy."""
    url: str
    page_type: str
    state_code: str | None = None
    city_slug: str | None = None
    city_name: str | None = None


class BestBuyAcquisitionStrategy(StoreLocationAcquisitionStrategy):
    """Represent BestBuyAcquisitionStrategy data used by the acquisition strategy."""
    retailer_key = "best_buy"
    retailer_name = "Best Buy"

    def __init__(
        self,
        *,
        state_workers: int = 8,
        city_workers: int = 16,
        store_workers: int = 32,
        request_timeout: int = 20,
        max_retries: int = 3,
    ) -> None:
        """Initialize acquisition configuration and run state."""
        self.state_workers = state_workers
        self.city_workers = city_workers
        self.store_workers = store_workers
        self.request_timeout = request_timeout
        self.max_retries = max_retries
        self._failed_city_pages: list[dict[str, Any]] = []
        self._failed_store_pages: list[dict[str, Any]] = []

    def discover_source(self) -> AcquisitionSourceInfo:
        """Return metadata describing the retailer's official acquisition source."""
        return AcquisitionSourceInfo(
            retailer_key=self.retailer_key,
            retailer_name=self.retailer_name,
            official_website_url="https://www.bestbuy.com/",
            store_locator_url=ROOT_URL,
            endpoint_url=ROOT_URL,
            source_type="html",
            provider="BeautifulSoup",
            notes=(
                "Best Buy store locator is hierarchical HTML: root -> state pages -> "
                "city pages / store detail pages. Some states link directly to a "
                "single store detail page from the root or state page. Multi-store "
                "city pages render store cards and each card contains a View Store Page link."
            ),
        )

    def fetch_raw_artifacts(self) -> list[AcquisitionArtifact]:
        """Fetch raw artifacts required for store location acquisition."""
        self._failed_city_pages = []
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

        root_html = self._fetch_text(session, ROOT_URL)
        artifacts.append(
            AcquisitionArtifact(
                artifact_type="html",
                source_url=ROOT_URL,
                content=root_html,
                metadata={
                    "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                    "page_type": "root",
                    "http_status": 200,
                    "scrape_status": "success",
                },
            )
        )

        root_state_jobs, root_store_jobs = self._parse_root_jobs(root_html)
        if not root_state_jobs and not root_store_jobs:
            raise RuntimeError(
                "Best Buy root page rendered, but no state or store links were parsed."
            )

        state_pbar = tqdm(total=len(root_state_jobs), desc="Best Buy states", unit="state")
        city_pbar = tqdm(desc="Best Buy cities", unit="page")
        store_pbar = tqdm(desc="Best Buy stores", unit="page")

        state_jobs = list(root_state_jobs)
        city_jobs: list[_Job] = []
        store_jobs: list[_Job] = list(root_store_jobs)

        try:
            with ThreadPoolExecutor(max_workers=self.state_workers) as pool:
                futures = {
                    pool.submit(self._fetch_text, session, job.url): job
                    for job in state_jobs
                }
                for future in as_completed(futures):
                    job = futures[future]
                    state_html = future.result()
                    artifacts.append(
                        AcquisitionArtifact(
                            artifact_type="html",
                            source_url=job.url,
                            content=state_html,
                            metadata={
                                "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                                "page_type": "state",
                                "state_code": job.state_code,
                                "http_status": 200,
                                "scrape_status": "success",
                            },
                        )
                    )
                    state_pbar.update(1)

                    parsed_city_jobs, parsed_store_jobs = self._parse_state_jobs(
                        state_html=state_html,
                        state_code=job.state_code or "",
                    )
                    city_jobs.extend(parsed_city_jobs)
                    store_jobs.extend(parsed_store_jobs)

            city_jobs = self._dedupe_jobs(city_jobs)
            if city_jobs:
                city_pbar.total = len(city_jobs)
                city_pbar.refresh()

            with ThreadPoolExecutor(max_workers=self.city_workers) as pool:
                futures = {
                    pool.submit(self._fetch_text, session, job.url): job
                    for job in city_jobs
                }
                for future in as_completed(futures):
                    job = futures[future]
                    try:
                        city_html = future.result()
                        artifacts.append(
                            AcquisitionArtifact(
                                artifact_type="html",
                                source_url=job.url,
                                content=city_html,
                                metadata={
                                    "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                                    "page_type": "city",
                                    "state_code": job.state_code,
                                    "city_slug": job.city_slug,
                                    "city_name": job.city_name,
                                    "http_status": 200,
                                    "scrape_status": "success",
                                },
                            )
                        )

                        store_jobs.extend(
                            self._parse_store_jobs_from_city_html(
                                city_html=city_html,
                                state_code=job.state_code or "",
                                city_slug=job.city_slug,
                                city_name=job.city_name,
                            )
                        )
                    except Exception as exc:
                        self._failed_city_pages.append(
                            {
                                "city_url": job.url,
                                "state_code": job.state_code,
                                "city_slug": job.city_slug,
                                "error": str(exc),
                            }
                        )
                        artifacts.append(
                            AcquisitionArtifact(
                                artifact_type="html",
                                source_url=job.url,
                                content="",
                                metadata={
                                    "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                                    "page_type": "city",
                                    "state_code": job.state_code,
                                    "city_slug": job.city_slug,
                                    "city_name": job.city_name,
                                    "http_status": 500,
                                    "scrape_status": "failed",
                                    "error": str(exc),
                                },
                            )
                        )
                    finally:
                        city_pbar.update(1)

            store_jobs = self._dedupe_jobs(store_jobs)
            if store_jobs:
                store_pbar.total = len(store_jobs)
                store_pbar.refresh()

            with ThreadPoolExecutor(max_workers=self.store_workers) as pool:
                futures = {
                    pool.submit(self._fetch_store_page, session, job): job
                    for job in store_jobs
                }
                for future in as_completed(futures):
                    artifact = future.result()
                    artifacts.append(artifact)
                    store_pbar.update(1)

        finally:
            state_pbar.close()
            city_pbar.close()
            store_pbar.close()
            session.close()

        return artifacts

    def extract_store_payloads(
        self,
        artifacts: Sequence[AcquisitionArtifact],
    ) -> list[dict[str, Any]]:
        """Extract normalized store payloads from acquired artifacts."""
        store_artifacts = [
            artifact
            for artifact in artifacts
            if artifact.metadata.get("page_type") == "store"
            and artifact.metadata.get("scrape_status") == "success"
        ]

        rows_by_store_id: dict[str, dict[str, Any]] = {}
        parse_pbar = tqdm(
            total=len(store_artifacts),
            desc="Parsing Best Buy stores",
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
                    if not row:
                        parse_pbar.update(1)
                        continue

                    store_id = self._clean_text(row.get("retailer_store_id"))
                    if not store_id:
                        parse_pbar.update(1)
                        continue

                    rows_by_store_id[store_id] = row
                    parse_pbar.update(1)
        finally:
            parse_pbar.close()

        return list(rows_by_store_id.values())

    def validate_store_payloads(
        self,
        payloads: Sequence[Mapping[str, Any]],
    ) -> AcquisitionValidationResult:
        """Validate acquired store payloads for completeness and uniqueness."""
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
        if self._failed_city_pages:
            issue_counts["failed_city_pages"] = len(self._failed_city_pages)
        if self._failed_store_pages:
            issue_counts["failed_store_pages"] = len(self._failed_store_pages)

        notes = [
            "Root page is used to discover state pages and some single-store pages.",
            "State pages may contain city pages or direct store detail pages.",
            "City pages with cards are parsed for View Store Page links, which are then fetched as detail pages.",
            "Store number is recovered from the trailing numeric suffix in the detail URL.",
            f"Workers: state={self.state_workers}, city={self.city_workers}, store={self.store_workers}",
        ]

        is_valid = (
            total_records > 0
            and missing_store_ids == 0
            and missing_addresses == 0
            and missing_phones == 0
            and len(self._failed_city_pages) == 0
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
        """Return acquisition source and execution details for the run summary."""
        return [
            f"Source: {ROOT_URL}",
            "Method: HTML / BeautifulSoup",
            "Hierarchy: root -> state pages -> city pages / store detail pages",
            "Single-store locations can appear directly on the root or state pages.",
            "Multi-store city pages render cards, and the View Store Page link points to the canonical store detail page.",
        ]

    def _fetch_text(self, session: requests.Session, url: str) -> str:
        """Fetch text."""
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

    def _fetch_store_page(self, session: requests.Session, job: _Job) -> AcquisitionArtifact:
        """Fetch store page."""
        try:
            html = self._fetch_text(session, job.url)
            return AcquisitionArtifact(
                artifact_type="html",
                source_url=job.url,
                content=html,
                metadata={
                    "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                    "page_type": "store",
                    "state_code": job.state_code,
                    "city_slug": job.city_slug,
                    "city_name": job.city_name,
                    "http_status": 200,
                    "scrape_status": "success",
                },
            )
        except Exception as exc:
            self._failed_store_pages.append(
                {
                    "store_url": job.url,
                    "state_code": job.state_code,
                    "city_slug": job.city_slug,
                    "error": str(exc),
                }
            )
            return AcquisitionArtifact(
                artifact_type="html",
                source_url=job.url,
                content="",
                metadata={
                    "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                    "page_type": "store",
                    "state_code": job.state_code,
                    "city_slug": job.city_slug,
                    "city_name": job.city_name,
                    "http_status": 500,
                    "scrape_status": "failed",
                    "error": str(exc),
                },
            )

    def _parse_root_jobs(self, html: str) -> tuple[list[_Job], list[_Job]]:
        """Parse root jobs."""
        soup = BeautifulSoup(html, "html.parser")
        state_jobs: list[_Job] = []
        store_jobs: list[_Job] = []

        for a in soup.select("a[href]"):
            href = self._clean_text(a.get("href"))
            if not href:
                continue

            absolute_url = urljoin(ROOT_URL, href)
            path = urlparse(absolute_url).path.strip("/")
            parts = [part for part in path.split("/") if part]

            if len(parts) == 1 and parts[0].lower().endswith(".html"):
                state_code = parts[0][:-5]
                if STATE_CODE_RE.fullmatch(state_code):
                    state_jobs.append(
                        _Job(
                            url=absolute_url,
                            page_type="state",
                            state_code=state_code.upper(),
                        )
                    )
                continue

            if self._is_store_detail_path(path):
                state_code = parts[0] if parts else None
                city_slug = parts[1] if len(parts) >= 2 else None
                store_jobs.append(
                    _Job(
                        url=absolute_url,
                        page_type="store",
                        state_code=state_code.upper() if state_code else None,
                        city_slug=city_slug,
                        city_name=self._clean_text(a.get_text(" ", strip=True)),
                    )
                )

        return self._dedupe_jobs(state_jobs), self._dedupe_jobs(store_jobs)

    def _parse_state_jobs(self, state_html: str, state_code: str) -> tuple[list[_Job], list[_Job]]:
        """Parse state jobs."""
        soup = BeautifulSoup(state_html, "html.parser")
        city_jobs: list[_Job] = []
        store_jobs: list[_Job] = []
        state_code_lower = state_code.lower()

        for a in soup.select("a[href]"):
            href = self._clean_text(a.get("href"))
            if not href:
                continue

            absolute_url = urljoin(ROOT_URL, href)
            path = urlparse(absolute_url).path.strip("/")
            parts = [part for part in path.split("/") if part]
            if not parts:
                continue

            if parts[0].lower() != state_code_lower:
                continue

            city_name = self._clean_text(a.get_text(" ", strip=True))
            if city_name:
                city_name = re.sub(r"\s*\(\d+\)\s*$", "", city_name).strip() or None

            if len(parts) == 2 and parts[1].lower().endswith(".html"):
                city_jobs.append(
                    _Job(
                        url=absolute_url,
                        page_type="city",
                        state_code=state_code.upper(),
                        city_slug=parts[1][:-5],
                        city_name=city_name,
                    )
                )
                continue

            if self._is_store_detail_path(path):
                store_jobs.append(
                    _Job(
                        url=absolute_url,
                        page_type="store",
                        state_code=state_code.upper(),
                        city_slug=parts[1] if len(parts) >= 2 else None,
                        city_name=city_name,
                    )
                )

        return self._dedupe_jobs(city_jobs), self._dedupe_jobs(store_jobs)

    def _parse_store_links_from_city_html(
        self,
        *,
        city_html: str,
        state_code: str,
        city_slug: str | None,
        city_name: str | None,
    ) -> list[_Job]:
        """Parse store links from city html."""
        soup = BeautifulSoup(city_html, "html.parser")
        jobs: list[_Job] = []

        for card in soup.select('.Directorycard, [data-testid="store-directory-card"]'):
            candidate_urls: list[str] = []
            for a in card.select("a[href]"):
                href = self._clean_text(a.get("href"))
                if not href:
                    continue
                absolute_url = urljoin(ROOT_URL, href)
                path = urlparse(absolute_url).path
                if self._is_store_detail_path(path):
                    candidate_urls.append(absolute_url)

            if not candidate_urls:
                continue

            store_url = candidate_urls[0]
            jobs.append(
                _Job(
                    url=store_url,
                    page_type="store",
                    state_code=state_code.upper(),
                    city_slug=city_slug,
                    city_name=city_name,
                )
            )

        return jobs

    def _parse_store_artifact(self, artifact: AcquisitionArtifact) -> dict[str, Any] | None:
        """Parse store artifact."""
        html = artifact.content or ""
        soup = BeautifulSoup(html, "html.parser")

        source_url = self._clean_text(artifact.source_url)
        state_code = self._clean_text(artifact.metadata.get("state_code"))
        city_slug = self._clean_text(artifact.metadata.get("city_slug"))
        city_name = self._clean_text(artifact.metadata.get("city_name"))

        store_id = self._extract_store_id_from_url(source_url)
        if not store_id:
            store_id = self._extract_store_id_from_detail_page(soup)
        if not store_id:
            return None

        title = self._clean_text(soup.select_one("h1"))
        if not title:
            title = self._clean_text(soup.select_one("h3"))

        phone = self._clean_text(soup.select_one('a[href^="tel:"]'))
        address = self._extract_address_from_detail_page(soup)

        return {
            "retailer": self.retailer_name,
            "retailer_store_id": store_id,
            "store_number": store_id,
            "store_type": "Regular",
            "store_name": title,
            "address": address.get("street_address"),
            "street_address": address.get("street_address"),
            "city": address.get("city"),
            "state": address.get("state"),
            "address_city": address.get("city"),
            "address_state": address.get("state"),
            "zip_code": address.get("zip_code"),
            "full_address": address.get("full_address"),
            "phone": phone,
            "store_url": source_url,
            "source_url": source_url,
            "source_sitemap": None,
            "state_code": state_code,
            "city_slug": city_slug,
            "city_name": city_name,
            "extraction_source": "HTML / BeautifulSoup",
            "scrape_status": "success",
            "http_status": artifact.metadata.get("http_status", 200),
            "error_message": None,
            "scraped_at_utc": artifact.metadata.get("retrieved_at_utc"),
        }

    @staticmethod
    def _is_store_detail_path(path: str) -> bool:
        """Return whether store detail path."""
        return bool(STORE_DETAIL_PATH_RE.fullmatch(f"/{path.lstrip('/')}"))

    @staticmethod
    def _extract_store_id_from_url(url: str | None) -> str | None:
        """Extract store id from url."""
        if not url:
            return None
        path = urlparse(url).path.strip("/")
        if not path:
            return None
        match = re.search(r"-(\d+)\.html$", path)
        if match:
            return match.group(1)
        return None

    def _extract_store_id_from_detail_page(self, soup: BeautifulSoup) -> str | None:
        """Extract store id from detail page."""
        for a in soup.select('a[href$=".html"]'):
            href = self._clean_text(a.get("href"))
            if not href:
                continue
            store_id = self._extract_store_id_from_url(href)
            if store_id:
                return store_id
        return None

    @staticmethod
    def _extract_address_from_detail_page(soup: BeautifulSoup) -> dict[str, str | None]:
        """Extract address from detail page."""
        lines = [
            re.sub(r"\s+", " ", line.get_text(" ", strip=True)).strip()
            for line in soup.select(".address-line")
        ]
        lines = [line for line in lines if line]

        if not lines:
            return {
                "street_address": None,
                "city": None,
                "state": None,
                "zip_code": None,
                "full_address": None,
            }

        street_address = lines[0]
        locality_text = lines[-1] if len(lines) >= 2 else None

        city, state, zip_code = BestBuyAcquisitionStrategy._parse_city_state_zip(locality_text)
        full_address = BestBuyAcquisitionStrategy._compose_full_address(
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
    def _parse_city_state_zip(locality_text: str | None) -> tuple[str | None, str | None, str | None]:
        """Parse city state zip."""
        if not locality_text:
            return None, None, None

        text = re.sub(r"\s+", " ", locality_text).strip()
        patterns = [
            r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$",
            r"^(?P<city>.+?)\s+(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$",
            r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})$",
            r"^(?P<city>.+?)\s+(?P<state>[A-Z]{2})$",
        ]

        for pattern in patterns:
            match = re.match(pattern, text)
            if not match:
                continue
            city = match.groupdict().get("city")
            state = match.groupdict().get("state")
            zip_code = match.groupdict().get("zip")
            return (
                city.strip() if city else None,
                state.strip().upper() if state else None,
                zip_code.strip() if zip_code else None,
            )

        zip_match = re.search(r"(\d{5}(?:-\d{4})?)", text)
        zip_code = zip_match.group(1) if zip_match else None
        state_match = re.search(r"\b([A-Z]{2})\b", text)
        state = state_match.group(1) if state_match else None
        city = text
        if state:
            city = re.sub(rf"\b{re.escape(state)}\b", "", city).strip(" ,")
        if zip_code:
            city = city.replace(zip_code, "").strip(" ,")
        city = re.sub(r"\s+", " ", city).strip() or None
        return city, state, zip_code

    @staticmethod
    def _compose_full_address(
        *,
        street_address: str | None,
        city: str | None,
        state: str | None,
        zip_code: str | None,
    ) -> str | None:
        """Compose full address."""
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
        """Normalize text."""
        if value is None:
            return None
        if hasattr(value, "get_text"):
            value = value.get_text(" ", strip=True)
        text = str(value).strip()
        return text or None

    def _dedupe_jobs(self, jobs: Sequence[_Job]) -> list[_Job]:
        """Deduplicate jobs."""
        deduped: list[_Job] = []
        seen: set[str] = set()
        for job in jobs:
            if job.url in seen:
                continue
            seen.add(job.url)
            deduped.append(job)
        return deduped