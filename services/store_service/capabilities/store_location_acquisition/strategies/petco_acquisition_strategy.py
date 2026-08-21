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

BASE_URL = "https://stores.petco.com"
ROOT_URL = f"{BASE_URL}/us"


@dataclass(slots=True)
class _StateLink:
    state_code: str
    href: str


@dataclass(slots=True)
class _CityLink:
    state_code: str
    city_slug: str
    href: str
    city_name: str | None
    is_detail_page: bool


class PetcoAcquisitionStrategy(StoreLocationAcquisitionStrategy):
    retailer_key = "petco"
    retailer_name = "Petco"

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

        self._failed_city_pages: list[dict[str, Any]] = []
        self._failed_store_pages: list[dict[str, Any]] = []

    def discover_source(self) -> AcquisitionSourceInfo:
        """Return metadata describing the retailer's official acquisition source.

        :return: Metadata describing the acquisition source.
        """
        return AcquisitionSourceInfo(
            retailer_key=self.retailer_key,
            retailer_name=self.retailer_name,
            official_website_url="https://www.petco.com/",
            store_locator_url=ROOT_URL,
            endpoint_url=ROOT_URL,
            source_type="html",
            provider="BeautifulSoup",
            notes=(
                "Petco store locator is hierarchical HTML: /us -> state pages -> "
                "city pages. Non-.html city URLs are multi-store pages rendered as "
                "cards. .html city URLs are single-store detail pages. Store number "
                "is recovered from the View Details URL suffix."
            ),
        )

    def fetch_raw_artifacts(self) -> list[AcquisitionArtifact]:
        """Fetch raw artifacts required for store location acquisition.

        :return: Raw acquisition artifacts.
        """
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
                },
            )
        )

        state_links = self._parse_state_links(root_html)
        if not state_links:
            raise RuntimeError(
                "Petco root page rendered, but no state links were parsed. "
                "Check the selector or page source."
            )

        state_pbar = tqdm(total=len(state_links), desc="Petco states", unit="state")
        city_pbar = tqdm(desc="Petco cities", unit="page")

        try:
            city_jobs: list[_CityLink] = []

            with ThreadPoolExecutor(max_workers=self.state_workers) as pool:
                futures = {
                    pool.submit(
                        self._fetch_text,
                        session,
                        urljoin(BASE_URL + "/", state_link.href.lstrip("/")),
                    ): state_link
                    for state_link in state_links
                }

                for future in as_completed(futures):
                    state_link = futures[future]
                    state_html = future.result()

                    artifacts.append(
                        AcquisitionArtifact(
                            artifact_type="html",
                            source_url=urljoin(BASE_URL + "/", state_link.href.lstrip("/")),
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

                    city_jobs.extend(
                        self._parse_city_links(
                            state_html=state_html,
                            state_code=state_link.state_code,
                        )
                    )

            city_jobs = self._dedupe_city_links(city_jobs)
            if not city_jobs:
                raise RuntimeError(
                    "Petco state pages were fetched, but no city links were parsed."
                )

            city_pbar.total = len(city_jobs)
            city_pbar.refresh()

            with ThreadPoolExecutor(max_workers=self.city_workers) as pool:
                futures: dict[Any, _CityLink] = {}

                for job in city_jobs:
                    absolute_url = urljoin(BASE_URL + "/", job.href.lstrip("/"))
                    if job.is_detail_page:
                        futures[
                            pool.submit(
                                self._fetch_store_page,
                                session,
                                absolute_url,
                                job.state_code,
                                job.city_slug,
                                job.city_name,
                            )
                        ] = job
                    else:
                        futures[
                            pool.submit(
                                self._fetch_city_page,
                                session,
                                absolute_url,
                                job.state_code,
                                job.city_slug,
                                job.city_name,
                            )
                        ] = job

                for future in as_completed(futures):
                    artifact = future.result()
                    artifacts.append(artifact)
                    city_pbar.update(1)

        finally:
            state_pbar.close()
            city_pbar.close()
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
        parse_artifacts = [
            artifact
            for artifact in artifacts
            if artifact.metadata.get("page_type") in {"city", "store"}
            and artifact.metadata.get("scrape_status") == "success"
        ]

        rows_by_store_id: dict[str, dict[str, Any]] = {}

        parse_pbar = tqdm(
            total=len(parse_artifacts),
            desc="Parsing Petco pages",
            unit="page",
        )

        try:
            with ThreadPoolExecutor(max_workers=self.store_workers) as pool:
                futures = {
                    pool.submit(self._parse_artifact_to_rows, artifact): artifact
                    for artifact in parse_artifacts
                }

                for future in as_completed(futures):
                    rows = future.result()
                    for row in rows:
                        store_id = self._clean_text(row.get("retailer_store_id"))
                        if not store_id:
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
            "State pages are used only to discover city links.",
            "City URLs ending in .html are single-store detail pages.",
            "City URLs without .html are multi-store pages and are parsed directly from cards.",
            "Store number is recovered from the View Details URL suffix.",
            f"Workers: state={self.state_workers}, city={self.city_workers}, store={self.store_workers}",
        ]

        is_valid = (
            total_records > 0
            and missing_store_ids == 0
            and missing_addresses == 0
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
        """Return acquisition source and execution details for the run summary.

        :return: Notes describing the acquisition run.
        """
        return [
            f"Source: {ROOT_URL}",
            "Method: HTML / BeautifulSoup",
            "Hierarchy: state pages -> city pages -> store cards/detail pages",
            "Single-store city URLs end with .html; multi-store city URLs do not.",
            "Store number is extracted from the View Details URL suffix.",
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

    def _fetch_city_page(
        self,
        session: requests.Session,
        city_url: str,
        state_code: str,
        city_slug: str,
        city_name: str | None,
    ) -> AcquisitionArtifact:
        """Fetch city page.

        :param session: HTTP session used for requests.
        :param city_url: City url.
        :param state_code: State code associated with the page.
        :param city_slug: City slug associated with the page.
        :param city_name: City name associated with the page.
        :return: Result produced by fetch city page.
        """
        try:
            html = self._fetch_text(session, city_url)
            return AcquisitionArtifact(
                artifact_type="html",
                source_url=city_url,
                content=html,
                metadata={
                    "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                    "page_type": "city",
                    "state_code": state_code,
                    "city_slug": city_slug,
                    "city_name": city_name,
                    "http_status": 200,
                    "scrape_status": "success",
                },
            )
        except Exception as exc:
            self._failed_city_pages.append(
                {
                    "city_url": city_url,
                    "state_code": state_code,
                    "city_slug": city_slug,
                    "error": str(exc),
                }
            )
            return AcquisitionArtifact(
                artifact_type="html",
                source_url=city_url,
                content="",
                metadata={
                    "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                    "page_type": "city",
                    "state_code": state_code,
                    "city_slug": city_slug,
                    "city_name": city_name,
                    "http_status": 500,
                    "scrape_status": "failed",
                    "error": str(exc),
                },
            )

    def _fetch_store_page(
        self,
        session: requests.Session,
        store_url: str,
        state_code: str,
        city_slug: str,
        city_name: str | None,
    ) -> AcquisitionArtifact:
        """Fetch store page.

        :param session: HTTP session used for requests.
        :param store_url: Store url.
        :param state_code: State code associated with the page.
        :param city_slug: City slug associated with the page.
        :param city_name: City name associated with the page.
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
                    "city_slug": city_slug,
                    "city_name": city_name,
                    "http_status": 200,
                    "scrape_status": "success",
                },
            )
        except Exception as exc:
            self._failed_store_pages.append(
                {
                    "store_url": store_url,
                    "state_code": state_code,
                    "city_slug": city_slug,
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
                    "city_slug": city_slug,
                    "city_name": city_name,
                    "http_status": 500,
                    "scrape_status": "failed",
                    "error": str(exc),
                },
            )

    def _parse_state_links(self, html: str) -> list[_StateLink]:
        """Parse state links.

        :param html: HTML content to parse.
        :return: Result produced by parse state links.
        """
        soup = BeautifulSoup(html, "html.parser")
        links: list[_StateLink] = []

        anchors = soup.select("a[href]")
        for a in anchors:
            href = self._clean_text(a.get("href"))
            if not href:
                continue

            path = urlparse(urljoin(BASE_URL + "/", href.lstrip("/"))).path.strip("/")
            parts = [part for part in path.split("/") if part]

            state_code: str | None = None
            if len(parts) == 1 and re.fullmatch(r"[a-z]{2}", parts[0]):
                state_code = parts[0].upper()
            elif len(parts) == 2 and parts[0].lower() == "us" and re.fullmatch(r"[a-z]{2}", parts[1]):
                state_code = parts[1].upper()

            if not state_code:
                continue

            links.append(
                _StateLink(
                    state_code=state_code,
                    href=href,
                )
            )

        deduped: list[_StateLink] = []
        seen: set[str] = set()
        for link in links:
            if link.state_code in seen:
                continue
            seen.add(link.state_code)
            deduped.append(link)

        return deduped

    def _parse_city_links(self, state_html: str, state_code: str) -> list[_CityLink]:
        """Parse city links.

        :param state_html: State html.
        :param state_code: State code associated with the page.
        :return: Result produced by parse city links.
        """
        soup = BeautifulSoup(state_html, "html.parser")
        jobs: list[_CityLink] = []

        anchors = soup.select("a[href]")
        for a in anchors:
            href = self._clean_text(a.get("href"))
            if not href:
                continue

            absolute_url = urljoin(BASE_URL + "/", href.lstrip("/"))
            path = urlparse(absolute_url).path.strip("/")
            parts = [part for part in path.split("/") if part]

            city_slug: str | None = None
            is_detail_page = False

            if len(parts) >= 2 and parts[0].lower() == state_code.lower():
                city_slug = parts[1]
                is_detail_page = path.lower().endswith(".html")
            elif len(parts) >= 3 and parts[0].lower() == "us" and parts[1].lower() == state_code.lower():
                city_slug = parts[2]
                is_detail_page = path.lower().endswith(".html")

            if not city_slug:
                continue

            if not (len(parts) == 2 or len(parts) == 3):
                # Keep the common city/detail shapes only.
                if not is_detail_page:
                    continue

            city_name = self._clean_text(a.get_text(" ", strip=True))
            if city_name:
                city_name = re.sub(r"\s*\(\d+\)\s*$", "", city_name).strip() or None

            jobs.append(
                _CityLink(
                    state_code=state_code.upper(),
                    city_slug=city_slug,
                    href=href,
                    city_name=city_name,
                    is_detail_page=is_detail_page,
                )
            )

        return jobs

    def _dedupe_city_links(self, jobs: Sequence[_CityLink]) -> list[_CityLink]:
        """Deduplicate city links.

        :param jobs: Acquisition jobs to deduplicate.
        :return: Result produced by dedupe city links.
        """
        deduped: list[_CityLink] = []
        seen: set[str] = set()
        for job in jobs:
            absolute_url = urljoin(BASE_URL + "/", job.href.lstrip("/"))
            if absolute_url in seen:
                continue
            seen.add(absolute_url)
            deduped.append(job)
        return deduped

    def _parse_artifact_to_rows(self, artifact: AcquisitionArtifact) -> list[dict[str, Any]]:
        """Parse artifact to rows.

        :param artifact: Acquisition artifact to parse.
        :return: Result produced by parse artifact to rows.
        """
        page_type = artifact.metadata.get("page_type")
        if page_type == "city":
            return self._parse_city_artifact(artifact)
        if page_type == "store":
            row = self._parse_store_artifact(artifact)
            return [row] if row is not None else []
        return []

    def _parse_city_artifact(self, artifact: AcquisitionArtifact) -> list[dict[str, Any]]:
        """Parse city artifact.

        :param artifact: Acquisition artifact to parse.
        :return: Result produced by parse city artifact.
        """
        html = artifact.content or ""
        soup = BeautifulSoup(html, "html.parser")

        source_url = self._clean_text(artifact.source_url)
        state_code = self._clean_text(artifact.metadata.get("state_code"))
        city_slug = self._clean_text(artifact.metadata.get("city_slug"))
        city_name = self._clean_text(artifact.metadata.get("city_name"))

        cards = soup.select('[data-testid="store-directory-card"]')
        if cards:
            rows: list[dict[str, Any]] = []
            for card in cards:
                row = self._parse_store_card(
                    card=card,
                    source_url=source_url,
                    state_code=state_code,
                    city_slug=city_slug,
                    city_name=city_name,
                )
                if row is not None:
                    rows.append(row)
            return rows

        detail_row = self._parse_store_detail_page(
            soup=soup,
            source_url=source_url,
            state_code=state_code,
            city_slug=city_slug,
            city_name=city_name,
        )
        return [detail_row] if detail_row is not None else []

    def _parse_store_artifact(self, artifact: AcquisitionArtifact) -> dict[str, Any] | None:
        """Parse store artifact.

        :param artifact: Acquisition artifact to parse.
        :return: Result produced by parse store artifact.
        """
        html = artifact.content or ""
        soup = BeautifulSoup(html, "html.parser")

        return self._parse_store_detail_page(
            soup=soup,
            source_url=self._clean_text(artifact.source_url),
            state_code=self._clean_text(artifact.metadata.get("state_code")),
            city_slug=self._clean_text(artifact.metadata.get("city_slug")),
            city_name=self._clean_text(artifact.metadata.get("city_name")),
        )

    def _parse_store_card(
        self,
        *,
        card: Any,
        source_url: str | None,
        state_code: str | None,
        city_slug: str | None,
        city_name: str | None,
    ) -> dict[str, Any] | None:
        """Parse store card.

        :param card: Store card HTML element.
        :param source_url: Source URL associated with the record.
        :param state_code: State code associated with the page.
        :param city_slug: City slug associated with the page.
        :param city_name: City name associated with the page.
        :return: Result produced by parse store card.
        """
        details_href: str | None = None

        for a in card.find_all("a", href=True):
            href = self._clean_text(a.get("href"))
            if not href:
                continue

            text = self._clean_text(a.get_text(" ", strip=True))
            if text and "view details" in text.lower() and href.lower().endswith(".html"):
                details_href = href
                break

        if not details_href:
            for a in card.find_all("a", href=True):
                href = self._clean_text(a.get("href"))
                if not href:
                    continue
                if "pet-supplies-" in href and href.lower().endswith(".html"):
                    details_href = href
                    break

        if not details_href:
            return None

        store_id = self._extract_store_id_from_url(details_href)
        if not store_id:
            return None

        store_name = self._clean_text(card.select_one("h3"))
        phone = self._clean_text(card.select_one('a[href^="tel:"]'))
        address = self._extract_address_from_card(card)

        store_url = urljoin(BASE_URL + "/", details_href.lstrip("/"))

        return {
            "retailer": self.retailer_name,
            "retailer_store_id": store_id,
            "store_number": store_id,
            "store_type": "Regular",
            "store_name": store_name,
            "address": address.get("street_address"),
            "street_address": address.get("street_address"),
            "city": address.get("city"),
            "state": address.get("state"),
            "address_city": address.get("city"),
            "address_state": address.get("state"),
            "zip_code": address.get("zip_code"),
            "full_address": address.get("full_address"),
            "phone": phone,
            "store_url": store_url,
            "source_url": source_url,
            "state_code": state_code,
            "city_slug": city_slug,
            "city_name": city_name,
            "extraction_source": "HTML / BeautifulSoup",
            "scrape_status": "success",
            "http_status": 200,
            "error_message": None,
            "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
        }

    def _parse_store_detail_page(
        self,
        *,
        soup: BeautifulSoup,
        source_url: str | None,
        state_code: str | None,
        city_slug: str | None,
        city_name: str | None,
    ) -> dict[str, Any] | None:
        """Parse store detail page.

        :param soup: Parsed HTML document.
        :param source_url: Source URL associated with the record.
        :param state_code: State code associated with the page.
        :param city_slug: City slug associated with the page.
        :param city_name: City name associated with the page.
        :return: Result produced by parse store detail page.
        """
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
            "state_code": state_code,
            "city_slug": city_slug,
            "city_name": city_name,
            "extraction_source": "HTML / BeautifulSoup",
            "scrape_status": "success",
            "http_status": 200,
            "error_message": None,
            "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _extract_store_id_from_url(url: str | None) -> str | None:
        """Extract store id from url.

        :param url: URL to fetch or process.
        :return: Result produced by extract store id from url.
        """
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
        """Extract store id from detail page.

        :param soup: Parsed HTML document.
        :return: Result produced by extract store id from detail page.
        """
        for a in soup.select('a[href*=".html"]'):
            href = self._clean_text(a.get("href"))
            if not href:
                continue
            store_id = self._extract_store_id_from_url(href)
            if store_id:
                return store_id
        return None

    def _extract_address_from_detail_page(self, soup: BeautifulSoup) -> dict[str, str | None]:
        """Extract address from detail page.

        :param soup: Parsed HTML document.
        :return: Result produced by extract address from detail page.
        """
        lines = soup.select(".address-line")
        if len(lines) < 2:
            return {
                "street_address": None,
                "city": None,
                "state": None,
                "zip_code": None,
                "full_address": None,
            }

        street_address = self._clean_text(lines[0])
        street_address = re.sub(r"\s+", " ", street_address or "").strip() or None

        locality_node = lines[1]
        locality_text = self._clean_text(locality_node.get_text(" ", strip=True))
        state_from_abbr = None
        abbr = locality_node.select_one("abbr")
        if abbr:
            state_from_abbr = self._clean_text(abbr.get_text(" ", strip=True))
            if state_from_abbr:
                state_from_abbr = state_from_abbr.upper()

        city, state, zip_code = self._parse_city_state_zip(
            locality_text,
            state_from_abbr=state_from_abbr,
        )

        full_address = self._compose_full_address(
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

    def _extract_address_from_card(self, card: Any) -> dict[str, str | None]:
        """Extract address from card.

        :param card: Store card HTML element.
        :return: Result produced by extract address from card.
        """
        line_nodes = card.select(
            ".store-directory-card__body .sparky-l-linelength-container > div"
        )
        if len(line_nodes) < 2:
            fallback_text = self._clean_text(
                card.select_one(".store-directory-card__body")
            )
            if not fallback_text:
                return {
                    "street_address": None,
                    "city": None,
                    "state": None,
                    "zip_code": None,
                    "full_address": None,
                }

            lines = [
                self._clean_text(line)
                for line in fallback_text.splitlines()
                if self._clean_text(line)
            ]
            if len(lines) < 2:
                return {
                    "street_address": None,
                    "city": None,
                    "state": None,
                    "zip_code": None,
                    "full_address": None,
                }

            street_address = self._clean_text(lines[0])
            locality_text = self._clean_text(lines[1])
        else:
            street_address = self._clean_text(line_nodes[0])
            locality_text = self._clean_text(line_nodes[1])

        street_address = re.sub(r"\s+", " ", street_address or "").strip() or None
        city, state, zip_code = self._parse_city_state_zip(locality_text)

        full_address = self._compose_full_address(
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
    def _parse_city_state_zip(
        locality_text: str | None,
        *,
        state_from_abbr: str | None = None,
    ) -> tuple[str | None, str | None, str | None]:
        """Parse city state zip.

        :param locality_text: Locality text.
        :param state_from_abbr: State from abbr.
        :return: Result produced by parse city state zip.
        """
        if not locality_text:
            return None, state_from_abbr, None

        text = re.sub(r"\s+", " ", locality_text).strip()
        text = text.replace(" ,", ",")

        patterns = [
            r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$",
            r"^(?P<city>.+?)\s+(?P<state>[A-Z]{2})\s*,?\s*(?P<zip>\d{5}(?:-\d{4})?)$",
            r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})$",
            r"^(?P<city>.+?)\s+(?P<state>[A-Z]{2})$",
        ]

        for pattern in patterns:
            match = re.match(pattern, text)
            if not match:
                continue

            city = match.group("city").strip() if match.groupdict().get("city") else None
            state = match.group("state").strip().upper() if match.groupdict().get("state") else None
            zip_code = match.group("zip").strip() if match.groupdict().get("zip") else None

            if state_from_abbr:
                state = state_from_abbr.upper()

            return city or None, state or None, zip_code or None

        zip_match = re.search(r"(\d{5}(?:-\d{4})?)", text)
        zip_code = zip_match.group(1) if zip_match else None

        state = state_from_abbr.upper() if state_from_abbr else None
        if state is None:
            state_match = re.search(r"\b([A-Z]{2})\b", text)
            if state_match:
                state = state_match.group(1)

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
        """Handle compose full address.

        :param street_address: Street address component.
        :param city: City entry to process.
        :param state: State name or abbreviation.
        :param zip_code: Postal code component.
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

    def _dedupe_rows(self, rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Deduplicate rows.

        :param rows: Store rows to deduplicate.
        :return: Result produced by dedupe rows.
        """
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            store_id = self._clean_text(row.get("retailer_store_id"))
            if not store_id:
                continue
            if store_id in seen:
                continue
            seen.add(store_id)
            deduped.append(row)
        return deduped