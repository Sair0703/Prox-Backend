from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin, urlparse
import re
import time

import requests
from bs4 import BeautifulSoup, NavigableString
from tqdm import tqdm

from services.store_service.capabilities.store_location_acquisition.protocals import (
    AcquisitionArtifact,
    AcquisitionSourceInfo,
    AcquisitionValidationResult,
    StoreLocationAcquisitionStrategy,
)

BASE_URL = "https://www.petsmart.com"
DIRECTORY_URL = f"{BASE_URL}/stores/us"


@dataclass(slots=True)
class _StateLink:
    state_code: str
    href: str


@dataclass(slots=True)
class _CityLink:
    state_code: str
    city_slug: str
    href: str


class PetSmartAcquisitionStrategy(StoreLocationAcquisitionStrategy):
    retailer_key = "petsmart"
    retailer_name = "PetSmart"

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

    def discover_source(self) -> AcquisitionSourceInfo:
        """Return metadata describing the retailer's official acquisition source.

        :return: Metadata describing the acquisition source.
        """
        return AcquisitionSourceInfo(
            retailer_key=self.retailer_key,
            retailer_name=self.retailer_name,
            official_website_url="https://www.petsmart.com/",
            store_locator_url=DIRECTORY_URL,
            endpoint_url=DIRECTORY_URL,
            source_type="html",
            provider="BeautifulSoup",
            notes=(
                "PetSmart store locator is hierarchical HTML: directory -> state pages -> city pages -> store cards. "
                "Store id is recovered from the store details URL when available."
            ),
        )

    def fetch_raw_artifacts(self) -> list[AcquisitionArtifact]:
        """Fetch raw artifacts required for store location acquisition.

        :return: Raw acquisition artifacts.
        """
        self._failed_city_pages = []

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

        directory_html = self._fetch_text(session, DIRECTORY_URL)
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

        state_links = self._parse_state_links(directory_html)
        state_pbar = tqdm(total=len(state_links), desc="PetSmart states", unit="state")

        try:
            state_artifacts: list[AcquisitionArtifact] = []
            with ThreadPoolExecutor(max_workers=self.state_workers) as pool:
                futures = {
                    pool.submit(self._fetch_state_page, session, state_link): state_link
                    for state_link in state_links
                }
                for future in as_completed(futures):
                    artifact = future.result()
                    state_artifacts.append(artifact)
                    artifacts.append(artifact)
                    state_pbar.update(1)

            city_jobs: list[_CityLink] = []
            for artifact in state_artifacts:
                if artifact.metadata.get("scrape_status") != "success":
                    continue
                state_code = self._clean_text(artifact.metadata.get("state_code"))
                if not state_code:
                    continue
                city_jobs.extend(self._parse_city_links(artifact.content or "", state_code))

            city_jobs = self._dedupe_city_jobs(city_jobs)
            city_pbar = tqdm(total=len(city_jobs), desc="PetSmart cities", unit="city")

            try:
                with ThreadPoolExecutor(max_workers=self.city_workers) as pool:
                    futures = {
                        pool.submit(
                            self._fetch_city_page,
                            session,
                            job.href,
                            job.state_code,
                            job.city_slug,
                        ): job
                        for job in city_jobs
                    }

                    for future in as_completed(futures):
                        artifact = future.result()
                        artifacts.append(artifact)
                        city_pbar.update(1)
            finally:
                city_pbar.close()

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
        city_artifacts = [
            artifact
            for artifact in artifacts
            if artifact.metadata.get("page_type") == "city"
            and artifact.metadata.get("scrape_status") == "success"
        ]

        payloads_by_store_id: dict[str, dict[str, Any]] = {}
        parse_pbar = tqdm(
            total=len(city_artifacts),
            desc="Parsing PetSmart stores",
            unit="city",
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
        if self._failed_city_pages:
            issue_counts["failed_city_pages"] = len(self._failed_city_pages)

        notes = [
            "Directory page is used only to discover state pages.",
            "State pages are used only to discover city pages.",
            "City store cards provide address, phone, hours, and store details URL.",
            "Store id is recovered from the store details URL when available.",
            f"Workers: state={self.state_workers}, city={self.city_workers}, store={self.store_workers}",
        ]

        is_valid = (
            total_records > 0
            and missing_store_ids == 0
            and missing_addresses == 0
            and len(self._failed_city_pages) == 0
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
            "Method: HTML / BeautifulSoup",
            "Hierarchy: directory -> state pages -> city pages -> store cards",
            "Parallelism: state page fetching + city page fetching + city card parsing",
            "Dedup key: retailer_store_id from store details URL regex fallback",
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

    def _parse_state_links(self, html: str) -> list[_StateLink]:
        """Parse state links.

        :param html: HTML content to parse.
        :return: Result produced by parse state links.
        """
        soup = BeautifulSoup(html, "html.parser")
        links: list[_StateLink] = []

        for a in soup.select('ul.store-directory-list a[href^="/stores/us/"]'):
            href = self._clean_text(a.get("href"))
            if not href:
                continue

            match = re.fullmatch(r"/stores/us/([a-z]{2})/?", href)
            if not match:
                continue

            state_code = match.group(1).upper()
            links.append(_StateLink(state_code=state_code, href=href))

        deduped: list[_StateLink] = []
        seen: set[str] = set()
        for link in links:
            if link.state_code in seen:
                continue
            seen.add(link.state_code)
            deduped.append(link)

        if not deduped:
            raise RuntimeError("PetSmart directory did not yield any state links.")

        return deduped

    def _fetch_state_page(
        self,
        session: requests.Session,
        state_link: _StateLink,
    ) -> AcquisitionArtifact:
        """Fetch state page.

        :param session: HTTP session used for requests.
        :param state_link: State link.
        :return: Result produced by fetch state page.
        """
        url = urljoin(BASE_URL, state_link.href)
        try:
            html = self._fetch_text(session, url)
            return AcquisitionArtifact(
                artifact_type="html",
                source_url=url,
                content=html,
                metadata={
                    "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                    "page_type": "state",
                    "state_code": state_link.state_code,
                    "http_status": 200,
                    "scrape_status": "success",
                },
            )
        except Exception as exc:
            return AcquisitionArtifact(
                artifact_type="html",
                source_url=url,
                content="",
                metadata={
                    "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                    "page_type": "state",
                    "state_code": state_link.state_code,
                    "http_status": 500,
                    "scrape_status": "failed",
                    "error": str(exc),
                },
            )

    def _parse_city_links(self, html: str, state_code: str) -> list[_CityLink]:
        """Parse city links.

        :param html: HTML content to parse.
        :param state_code: State code associated with the page.
        :return: Result produced by parse city links.
        """
        soup = BeautifulSoup(html, "html.parser")
        links: list[_CityLink] = []

        for a in soup.select('ul.store-directory-list a[href^="/stores/us/"]'):
            href = self._clean_text(a.get("href"))
            if not href:
                continue

            match = re.fullmatch(rf"/stores/us/{state_code.lower()}/([^/]+)/?", href)
            if not match:
                continue

            city_slug = match.group(1)
            links.append(_CityLink(state_code=state_code, city_slug=city_slug, href=href))

        deduped: list[_CityLink] = []
        seen: set[str] = set()
        for link in links:
            if link.href in seen:
                continue
            seen.add(link.href)
            deduped.append(link)

        return deduped

    def _dedupe_city_jobs(self, jobs: Sequence[_CityLink]) -> list[_CityLink]:
        """Deduplicate city jobs.

        :param jobs: Acquisition jobs to deduplicate.
        :return: Result produced by dedupe city jobs.
        """
        deduped: list[_CityLink] = []
        seen: set[str] = set()
        for job in jobs:
            if job.href in seen:
                continue
            seen.add(job.href)
            deduped.append(job)
        return deduped

    def _fetch_city_page(
        self,
        session: requests.Session,
        href: str,
        state_code: str,
        city_slug: str,
    ) -> AcquisitionArtifact:
        """Fetch city page.

        :param session: HTTP session used for requests.
        :param href: Link or URL to process.
        :param state_code: State code associated with the page.
        :param city_slug: City slug associated with the page.
        :return: Result produced by fetch city page.
        """
        url = urljoin(BASE_URL, href)
        try:
            html = self._fetch_text(session, url)
            return AcquisitionArtifact(
                artifact_type="html",
                source_url=url,
                content=html,
                metadata={
                    "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                    "page_type": "city",
                    "state_code": state_code,
                    "city_slug": city_slug,
                    "http_status": 200,
                    "scrape_status": "success",
                },
            )
        except Exception as exc:
            self._failed_city_pages.append(
                {
                    "city_url": url,
                    "state_code": state_code,
                    "city_slug": city_slug,
                    "error": str(exc),
                }
            )
            return AcquisitionArtifact(
                artifact_type="html",
                source_url=url,
                content="",
                metadata={
                    "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                    "page_type": "city",
                    "state_code": state_code,
                    "city_slug": city_slug,
                    "http_status": 500,
                    "scrape_status": "failed",
                    "error": str(exc),
                },
            )

    def _parse_city_artifact(self, artifact: AcquisitionArtifact) -> list[dict[str, Any]]:
        """Parse city artifact.

        :param artifact: Acquisition artifact to parse.
        :return: Result produced by parse city artifact.
        """
        html = artifact.content or ""
        soup = BeautifulSoup(html, "html.parser")

        state_code = self._clean_text(artifact.metadata.get("state_code"))
        city_slug = self._clean_text(artifact.metadata.get("city_slug"))
        source_url = self._clean_text(artifact.source_url)

        rows: list[dict[str, Any]] = []
        cards = soup.select('div.store-directory-card[data-testid="store-directory-card"]')

        for card in cards:
            store_name = self._extract_card_store_name(card)
            hours = self._clean_text(
                card.select_one(".store-directory-card__hours .sparky-c-text-passage__inner")
            )

            street_address, city, parsed_state, zip_code, full_address = self._extract_card_address(
                card,
                default_state=state_code,
            )
            phone = self._extract_card_phone(card)
            details_url = self._extract_card_details_url(card)
            retailer_store_id = self._extract_store_id_from_details_url(details_url)
            if not retailer_store_id:
                retailer_store_id = self._extract_store_id_from_store_url(details_url)

            if not retailer_store_id:
                retailer_store_id = self._extract_store_id_from_store_url(source_url)

            directions_url = self._extract_card_directions_url(card)

            rows.append(
                {
                    "retailer": self.retailer_name,
                    "retailer_store_id": retailer_store_id,
                    "store_number": retailer_store_id,
                    "store_type": "Regular",
                    "store_name": store_name,
                    "address": street_address,
                    "street_address": street_address,
                    "city": city,
                    "state": parsed_state or state_code,
                    "zip_code": zip_code,
                    "full_address": full_address,
                    "phone": phone,
                    "store_url": details_url,
                    "source_url": source_url,
                    "source_sitemap": None,
                    "city_slug": city_slug,
                    "store_hours": hours,
                    "directions_url": directions_url,
                    "extraction_source": "PetSmart store-directory card HTML",
                    "scrape_status": "success",
                    "http_status": artifact.metadata.get("http_status"),
                    "error_message": None,
                    "scraped_at_utc": artifact.metadata.get("retrieved_at_utc"),
                }
            )

        return rows

    @staticmethod
    def _extract_card_store_name(card: BeautifulSoup) -> str | None:
        """Extract card store name.

        :param card: Store card HTML element.
        :return: Result produced by extract card store name.
        """
        heading = card.select_one("h4.store-directory-card__heading")
        if heading is None:
            return None

        for child in heading.contents:
            if isinstance(child, NavigableString):
                text = str(child).strip()
                if text:
                    return text
                continue
            text = getattr(child, "get_text", lambda *args, **kwargs: "")(" ", strip=True)
            text = str(text).strip()
            if text:
                return text
        return None

    @staticmethod
    def _extract_card_address(
        card: BeautifulSoup,
        *,
        default_state: str | None,
    ) -> tuple[str | None, str | None, str | None, str | None, str | None]:
        """Extract card address.

        :param card: Store card HTML element.
        :param default_state: Default state.
        :return: Result produced by extract card address.
        """
        body_lines = [
            line.get_text(" ", strip=True)
            for line in card.select(
                ".store-directory-card__body .sparky-c-text-passage__inner > div"
            )
        ]
        body_lines = [re.sub(r"\s+", " ", line).strip() for line in body_lines if line]

        street_address = body_lines[0] if body_lines else None
        city = None
        parsed_state = default_state
        zip_code = None

        if len(body_lines) >= 2:
            match = re.match(
                r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})\s*(?P<zip>\d{5}(?:-\d{4})?)$",
                body_lines[1],
            )
            if match:
                city = match.group("city").strip()
                parsed_state = match.group("state").strip().upper()
                zip_code = match.group("zip").strip()
            else:
                city_state_match = re.match(
                    r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})$",
                    body_lines[1],
                )
                if city_state_match:
                    city = city_state_match.group("city").strip()
                    parsed_state = city_state_match.group("state").strip().upper()

                zip_match = re.search(r"\b\d{5}(?:-\d{4})?\b", body_lines[1])
                if zip_match:
                    zip_code = zip_match.group(0)

        full_address = PetSmartAcquisitionStrategy._compose_full_address(
            street_address=street_address,
            city=city,
            state=parsed_state,
            zip_code=zip_code,
        )

        return street_address, city, parsed_state, zip_code, full_address

    @staticmethod
    def _extract_card_phone(card: BeautifulSoup) -> str | None:
        """Extract card phone.

        :param card: Store card HTML element.
        :return: Result produced by extract card phone.
        """
        phone = card.select_one('.store-directory-card__footer a[href^="tel:"]')
        if not phone:
            return None
        text = phone.get_text(" ", strip=True)
        return text or None

    @staticmethod
    def _extract_card_details_url(card: BeautifulSoup) -> str | None:
        """Extract card details url.

        :param card: Store card HTML element.
        :return: Result produced by extract card details url.
        """
        details = card.select_one('.store-directory-card__footer a[href*="/stores/us/"]')
        if not details:
            return None
        href = details.get("href") or None
        return href

    @staticmethod
    def _extract_card_directions_url(card: BeautifulSoup) -> str | None:
        """Extract card directions url.

        :param card: Store card HTML element.
        :return: Result produced by extract card directions url.
        """
        directions = card.select_one('.store-directory-card__directions[href]')
        if not directions:
            return None
        href = directions.get("href") or None
        return href

    @staticmethod
    def _extract_store_id_from_details_url(details_url: str | None) -> str | None:
        """Extract store id from details url.

        :param details_url: Details url.
        :return: Result produced by extract store id from details url.
        """
        if not details_url:
            return None
        match = re.search(r"store(\d+)\.html", details_url)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _extract_store_id_from_store_url(store_url: str | None) -> str | None:
        """Extract store id from store url.

        :param store_url: Store url.
        :return: Result produced by extract store id from store url.
        """
        if not store_url:
            return None
        path = urlparse(store_url).path.strip("/")
        if not path:
            return None
        slug = path.split("/")[-1]
        match = re.search(r"store(\d+)", slug)
        if match:
            return match.group(1)
        return None

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

        city_state_bits: list[str] = []
        if city:
            city_state_bits.append(city)
        if state:
            city_state_bits.append(state)

        city_state = ", ".join(city_state_bits)
        if zip_code:
            city_state = f"{city_state} {zip_code}".strip()

        parts = [part for part in [street_address, city_state] if part]
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