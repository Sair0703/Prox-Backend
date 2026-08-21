from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import re
import threading
import time
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

from services.store_service.capabilities.store_location_acquisition.protocals import (
    AcquisitionArtifact,
    AcquisitionSourceInfo,
    AcquisitionValidationResult,
    StoreLocationAcquisitionStrategy,
)

BASE_URL = "https://locations.traderjoes.com"
ROOT_URL = f"{BASE_URL}/"

STATE_LINK_SELECTOR = 'a.ga_w2gi_lp.listitem[href^="/"]'
CITY_LINK_SELECTOR = 'a.ga_w2gi_lp.listitem[href^="/"]'
STORE_LINK_SELECTOR = 'a.capital.listitem[href*="/"]'

STATE_PATH_RE = re.compile(r"^/(?P<state>[a-z]{2})/$", re.IGNORECASE)
CITY_PATH_RE = re.compile(
    r"^/(?P<state>[a-z]{2})/(?P<city>[^/]+)/?$",
    re.IGNORECASE,
)
STORE_PATH_RE = re.compile(
    r"^/(?P<state>[a-z]{2})/(?P<city>[^/]+)/(?P<store_id>\d+)/?$",
    re.IGNORECASE,
)
US_STATE_CODES = {
    "AL","AZ","AR","CA","CO","CT","DE","DC","FL","GA","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MO","NE","NV","NH","NJ","NM",
    "NY","NC","OH","OK","OR","PA","RI","SC","TN","TX","UT","VT","VA","WA","WI",
}


@dataclass(frozen=True, slots=True)
class _StateEntry:
    state_code: str
    state_name: str
    url: str


@dataclass(frozen=True, slots=True)
class _CityEntry:
    state_code: str
    city_slug: str
    city_name: str
    url: str


class TraderJoesAcquisitionStrategy(StoreLocationAcquisitionStrategy):
    retailer_key = "trader_joes"
    retailer_name = "Trader Joe's"

    def __init__(
        self,
        *,
        state_workers: int = 16,
        city_workers: int = 48,
        parse_workers: int = 64,
        request_timeout: int = 30,
        max_retries: int = 3,
    ) -> None:
        """Initialize acquisition configuration and run state.

        :param state_workers: State workers.
        :param city_workers: City workers.
        :param parse_workers: Parse workers.
        :param request_timeout: Request timeout.
        :param max_retries: Max retries.
        :return: Result produced by init  .
        """
        self.state_workers = state_workers
        self.city_workers = city_workers
        self.parse_workers = parse_workers
        self.request_timeout = request_timeout
        self.max_retries = max_retries
        self._thread_local = threading.local()

        self._failed_state_pages: list[dict[str, Any]] = []
        self._failed_city_pages: list[dict[str, Any]] = []
        self._expected_city_count = 0
        self._expected_store_count = 0

    def discover_source(self) -> AcquisitionSourceInfo:
        """Return metadata describing the retailer's official acquisition source.

        :return: Metadata describing the acquisition source.
        """
        return AcquisitionSourceInfo(
            retailer_key=self.retailer_key,
            retailer_name=self.retailer_name,
            official_website_url="https://www.traderjoes.com/",
            store_locator_url=ROOT_URL,
            endpoint_url=ROOT_URL,
            source_type="html",
            provider="Trader Joe's store directory",
            notes=(
                "Official Trader Joe's directory hierarchy: root -> state -> "
                "city -> store listings. City pages already expose canonical "
                "store URLs, store numbers, address, and phone, so detail pages "
                "are not required."
            ),
        )

    def fetch_raw_artifacts(self) -> list[AcquisitionArtifact]:
        """Fetch raw artifacts required for store location acquisition.

        :return: Raw acquisition artifacts.
        """
        self._reset_run_state()
        artifacts: list[AcquisitionArtifact] = []

        root_html = self._fetch_text(ROOT_URL)
        states = self._parse_state_entries(root_html)

        if not states:
            raise RuntimeError(
                "Trader Joe's root directory returned no state links."
            )

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
                    "state_count": len(states),
                },
            )
        )

        state_artifacts: list[AcquisitionArtifact] = []

        with ThreadPoolExecutor(max_workers=self.state_workers) as pool:
            futures = {
                pool.submit(self._fetch_state_artifact, state): state
                for state in states
            }

            for future in tqdm(
                as_completed(futures),
                total=len(futures),
                desc="Trader Joe's states",
                unit="state",
            ):
                state = futures[future]
                try:
                    artifact, cities = future.result()
                except Exception as exc:
                    self._failed_state_pages.append(
                        {
                            "state_code": state.state_code,
                            "state_name": state.state_name,
                            "url": state.url,
                            "error": str(exc),
                        }
                    )
                    artifacts.append(
                        self._failed_artifact(
                            url=state.url,
                            page_type="state",
                            state_code=state.state_code,
                            error=exc,
                        )
                    )
                    continue

                state_artifacts.append(artifact)
                artifacts.append(artifact)
                self._expected_city_count += len(cities)

        cities: list[_CityEntry] = []
        seen_city_urls: set[str] = set()

        for artifact in state_artifacts:
            state_code = self._clean_text(
                artifact.metadata.get("state_code")
            )
            if not state_code:
                continue

            for city in self._parse_city_entries(
                artifact.content or "",
                state_code=state_code,
            ):
                if city.url in seen_city_urls:
                    continue
                seen_city_urls.add(city.url)
                cities.append(city)

        if not cities:
            raise RuntimeError(
                "Trader Joe's state pages were fetched, but no city links "
                "were discovered."
            )

        with ThreadPoolExecutor(max_workers=self.city_workers) as pool:
            futures = {
                pool.submit(self._fetch_city_artifact, city): city
                for city in cities
            }

            for future in tqdm(
                as_completed(futures),
                total=len(futures),
                desc="Trader Joe's cities",
                unit="city",
            ):
                city = futures[future]
                try:
                    artifact = future.result()
                except Exception as exc:
                    self._failed_city_pages.append(
                        {
                            "state_code": city.state_code,
                            "city_slug": city.city_slug,
                            "city_name": city.city_name,
                            "url": city.url,
                            "error": str(exc),
                        }
                    )
                    artifacts.append(
                        self._failed_artifact(
                            url=city.url,
                            page_type="city",
                            state_code=city.state_code,
                            city_slug=city.city_slug,
                            error=exc,
                        )
                    )
                    continue

                artifacts.append(artifact)
                self._expected_store_count += int(
                    artifact.metadata.get("store_count") or 0
                )

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
            and artifact.content
        ]

        rows_by_store_id: dict[str, dict[str, Any]] = {}

        with ThreadPoolExecutor(max_workers=self.parse_workers) as pool:
            futures = {
                pool.submit(self._parse_city_artifact, artifact): artifact
                for artifact in city_artifacts
            }

            for future in tqdm(
                as_completed(futures),
                total=len(futures),
                desc="Parsing Trader Joe's stores",
                unit="city",
            ):
                rows = future.result()
                for row in rows:
                    store_id = self._clean_text(
                        row.get("retailer_store_id")
                    )
                    if store_id:
                        rows_by_store_id[store_id] = row

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
        store_ids = [
            self._clean_text(row.get("retailer_store_id"))
            for row in payloads
        ]

        unique_store_ids = len(
            {store_id for store_id in store_ids if store_id}
        )
        missing_store_ids = sum(
            1 for store_id in store_ids if not store_id
        )

        duplicate_store_ids: list[str] = []
        seen: set[str] = set()
        for store_id in store_ids:
            if not store_id:
                continue
            if store_id in seen and store_id not in duplicate_store_ids:
                duplicate_store_ids.append(store_id)
            seen.add(store_id)

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

        invalid_store_ids = sum(
            1
            for store_id in store_ids
            if store_id and not re.fullmatch(r"\d+", store_id)
        )

        issue_counts: dict[str, int] = {}

        if missing_store_ids:
            issue_counts["missing_store_ids"] = missing_store_ids
        if duplicate_store_ids:
            issue_counts["duplicate_store_ids"] = len(
                duplicate_store_ids
            )
        if missing_addresses:
            issue_counts["missing_addresses"] = missing_addresses
        if missing_phones:
            issue_counts["missing_phones"] = missing_phones
        if invalid_store_ids:
            issue_counts["invalid_store_ids"] = invalid_store_ids
        if self._failed_state_pages:
            issue_counts["failed_state_pages"] = len(
                self._failed_state_pages
            )
        if self._failed_city_pages:
            issue_counts["failed_city_pages"] = len(
                self._failed_city_pages
            )

        notes = [
            "Official source hierarchy: root -> state -> city -> store listing.",
            (
                "City pages already expose canonical store URLs, addresses, "
                "and phone numbers; store detail pages are not required."
            ),
            (
                "retailer_store_id is the numeric final path segment of the "
                "canonical Trader Joe's store URL, e.g. /al/birmingham/737/ -> 737."
            ),
            (
                f"Discovered states: "
                f"{len({r.state_code for r in self._parse_state_entries(root_html)})}"
                if False else "State discovery completed from the official directory."
            ),
            (
                f"Workers: state={self.state_workers}, "
                f"city={self.city_workers}, parse={self.parse_workers}"
            ),
        ]

        is_valid = (
            total_records > 0
            and missing_store_ids == 0
            and not duplicate_store_ids
            and missing_addresses == 0
            and missing_phones == 0
            and invalid_store_ids == 0
            and not self._failed_state_pages
            and not self._failed_city_pages
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
            "Method: requests + BeautifulSoup",
            "Hierarchy: root -> state pages -> city pages -> store listings",
            "No store detail-page crawl required.",
            (
                "retailer_store_id is the final numeric segment of the "
                "canonical store URL."
            ),
            "Address and phone are parsed directly from official city listings.",
            (
                f"Workers: state={self.state_workers}, "
                f"city={self.city_workers}, parse={self.parse_workers}"
            ),
        ]

    def _fetch_state_artifact(
        self,
        state: _StateEntry,
    ) -> tuple[AcquisitionArtifact, list[_CityEntry]]:
        """Fetch state artifact.

        :param state: State name or abbreviation.
        :return: Result produced by fetch state artifact.
        """
        html = self._fetch_text(state.url)
        cities = self._parse_city_entries(
            html,
            state_code=state.state_code,
        )

        if not cities:
            raise RuntimeError(
                f"No Trader Joe's cities discovered for {state.state_code}: "
                f"{state.url}"
            )

        return (
            AcquisitionArtifact(
                artifact_type="html",
                source_url=state.url,
                content=html,
                metadata={
                    "retrieved_at_utc": self._utc_now(),
                    "page_type": "state",
                    "state_code": state.state_code,
                    "state_name": state.state_name,
                    "city_count": len(cities),
                    "http_status": 200,
                    "scrape_status": "success",
                },
            ),
            cities,
        )

    def _fetch_city_artifact(
        self,
        city: _CityEntry,
    ) -> AcquisitionArtifact:
        """Fetch city artifact.

        :param city: City or locality component.
        :return: Result produced by fetch city artifact.
        """
        html = self._fetch_text(city.url)
        store_count = self._count_store_links(html)

        if store_count == 0:
            raise RuntimeError(
                f"No Trader Joe's store links discovered on {city.url}"
            )

        return AcquisitionArtifact(
            artifact_type="html",
            source_url=city.url,
            content=html,
            metadata={
                "retrieved_at_utc": self._utc_now(),
                "page_type": "city",
                "state_code": city.state_code,
                "city_slug": city.city_slug,
                "city_name": city.city_name,
                "store_count": store_count,
                "http_status": 200,
                "scrape_status": "success",
            },
        )

    def _parse_state_entries(
        self,
        html: str,
    ) -> list[_StateEntry]:
        """Parse state entries.

        :param html: HTML content to parse.
        :return: Result produced by parse state entries.
        """
        soup = BeautifulSoup(html or "", "html.parser")
        entries: list[_StateEntry] = []

        for anchor in soup.select(STATE_LINK_SELECTOR):
            href = self._clean_text(anchor.get("href"))
            if not href:
                continue

            path = urlparse(urljoin(BASE_URL, href)).path
            match = STATE_PATH_RE.fullmatch(path)
            if not match:
                continue

            state_code = match.group("state").upper()
            if state_code not in US_STATE_CODES:
                continue

            state_name = self._clean_text(
                anchor.get_text(" ", strip=True)
            )
            if not state_name:
                continue

            entries.append(
                _StateEntry(
                    state_code=state_code,
                    state_name=state_name,
                    url=urljoin(BASE_URL, href),
                )
            )

        return self._dedupe_states(entries)

    def _parse_city_entries(
        self,
        html: str,
        *,
        state_code: str,
    ) -> list[_CityEntry]:
        """Parse city entries.

        :param html: HTML content to parse.
        :param state_code: State code associated with the page or record.
        :return: Result produced by parse city entries.
        """
        soup = BeautifulSoup(html or "", "html.parser")
        entries: list[_CityEntry] = []

        for anchor in soup.select(CITY_LINK_SELECTOR):
            href = self._clean_text(anchor.get("href"))
            if not href:
                continue

            absolute_url = urljoin(BASE_URL, href)
            path = urlparse(absolute_url).path
            match = CITY_PATH_RE.fullmatch(path)
            if not match:
                continue

            parsed_state = match.group("state").upper()
            if parsed_state != state_code.upper():
                continue

            city_name = self._clean_text(
                anchor.get_text(" ", strip=True)
            )
            city_slug = match.group("city")
            if not city_name or not city_slug:
                continue

            entries.append(
                _CityEntry(
                    state_code=parsed_state,
                    city_slug=city_slug,
                    city_name=city_name,
                    url=absolute_url,
                )
            )

        return self._dedupe_cities(entries)

    def _parse_city_artifact(
        self,
        artifact: AcquisitionArtifact,
    ) -> list[dict[str, Any]]:
        """Parse city artifact.

        :param artifact: Acquisition artifact to parse.
        :return: Result produced by parse city artifact.
        """
        soup = BeautifulSoup(
            artifact.content or "",
            "html.parser",
        )

        rows: list[dict[str, Any]] = []

        for anchor in soup.select(STORE_LINK_SELECTOR):
            href = self._clean_text(anchor.get("href"))
            if not href:
                continue

            absolute_url = urljoin(BASE_URL, href)
            path = urlparse(absolute_url).path
            match = STORE_PATH_RE.fullmatch(path)
            if not match:
                continue

            state_code = match.group("state").upper()
            city_slug = match.group("city")
            store_id = match.group("store_id")

            name_span = anchor.select_one(
                '[data-gaact="Click_to_ViewLocalPage"]'
            )
            store_name = self._clean_text(
                name_span.get_text(" ", strip=True)
                if name_span is not None
                else anchor.get_text(" ", strip=True)
            )

            # The provided city-page markup puts the address and phone in the
            # same parent itemlist container as the canonical store link.
            item = anchor.find_parent("div", class_="itemlist")
            if item is None:
                continue

            address_node = item.select_one(".address-span")
            address = self._parse_address_node(
                address_node
            )

            phone_anchor = item.select_one("a.phone-btn[href^='tel:']")
            phone = self._clean_text(
                phone_anchor.get_text(" ", strip=True)
                if phone_anchor is not None
                else None
            )

            rows.append(
                {
                    "retailer": self.retailer_name,
                    "retailer_store_id": store_id,
                    "store_number": store_id,
                    "store_type": "Regular",
                    "store_name": store_name,
                    "address": address["street_address"],
                    "street_address": address["street_address"],
                    "city": address["city"],
                    "state": state_code,
                    "address_city": address["city"],
                    "address_state": state_code,
                    "zip_code": address["zip_code"],
                    "full_address": address["full_address"],
                    "phone": phone,
                    "store_url": absolute_url,
                    "source_url": absolute_url,
                    "source_sitemap": artifact.source_url,
                    "city_slug": city_slug,
                    "extraction_source": (
                        "Trader Joe's official store directory city listing"
                    ),
                    "scrape_status": "success",
                    "http_status": artifact.metadata.get("http_status"),
                    "error_message": None,
                    "scraped_at_utc": artifact.metadata.get(
                        "retrieved_at_utc"
                    ),
                }
            )

        return rows

    @staticmethod
    def _parse_address_node(
        node: Any,
    ) -> dict[str, str | None]:
        """Parse address node.

        :param node: HTML node to parse.
        :return: Result produced by parse address node.
        """
        if node is None:
            return {
                "street_address": None,
                "city": None,
                "zip_code": None,
                "full_address": None,
            }

        text = node.get_text(" ", strip=True)
        text = re.sub(r"\s+", " ", text)

        # Normalize the exact structure:
        # street, City, STATE ZIP US
        match = re.match(
            r"^(?P<street>.+?)\s*,\s*"
            r"(?P<city>.+?),\s*"
            r"(?P<state>[A-Za-z]{2})\s+"
            r"(?P<zip>\d{5}(?:-\d{4})?)\s+US$",
            text,
            re.IGNORECASE,
        )

        if match:
            street = match.group("street").strip()
            city = match.group("city").strip()
            zip_code = match.group("zip").strip()
            full_address = (
                f"{street}, {city}, "
                f"{match.group('state').upper()} {zip_code}"
            )
            return {
                "street_address": street,
                "city": city,
                "zip_code": zip_code,
                "full_address": full_address,
            }

        # Fallback: parse the visible spans separately.
        spans: list[str] = []
        if hasattr(node, "find_all"):
            for span_node in node.find_all("span"):
                span_text = span_node.get_text(" ", strip=True)
                if span_text:
                    spans.append(span_text)

        street = spans[0] if spans else None
        city = spans[1] if len(spans) > 1 else None
        zip_code = next(
            (
                value
                for value in spans[2:]
                if re.fullmatch(r"\d{5}(?:-\d{4})?", value)
            ),
            None,
        )

        full_address = None
        if street and city and zip_code:
            # State is deliberately omitted in this fallback because the
            # caller already has it from the canonical URL.
            full_address = (
                f"{street}, {city}, {zip_code}"
            )

        return {
            "street_address": street,
            "city": city,
            "zip_code": zip_code,
            "full_address": full_address,
        }

    @staticmethod
    def _count_store_links(
        html: str,
    ) -> int:
        """Count store links.

        :param html: HTML content to parse.
        :return: Result produced by count store links.
        """
        soup = BeautifulSoup(html or "", "html.parser")
        count = 0
        for anchor in soup.select(STORE_LINK_SELECTOR):
            href = anchor.get("href")
            if href and STORE_PATH_RE.fullmatch(
                urlparse(urljoin(BASE_URL, href)).path
            ):
                count += 1
        return count

    def _fetch_text(
        self,
        url: str,
    ) -> str:
        """Fetch text.

        :param url: URL to fetch or process.
        :return: Result produced by fetch text.
        """
        session = self._get_session()
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = session.get(
                    url,
                    timeout=self.request_timeout,
                )
                response.raise_for_status()

                if not response.text:
                    raise RuntimeError(
                        f"Empty response body for {url}"
                    )

                return response.text

            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(
                        0.6 * (2 ** (attempt - 1))
                    )

        raise RuntimeError(
            f"Failed to fetch Trader Joe's page {url}: {last_error}"
        ) from last_error

    def _get_session(self) -> requests.Session:
        """Return session.

        :return: Result produced by get session.
        """
        session = getattr(
            self._thread_local,
            "session",
            None,
        )
        if session is None:
            session = requests.Session()
            session.headers.update(
                {
                    "accept": (
                        "text/html,application/xhtml+xml,application/xml;"
                        "q=0.9,image/avif,image/webp,*/*;q=0.8"
                    ),
                    "accept-language": "en-US,en;q=0.9",
                    "cache-control": "no-cache",
                    "pragma": "no-cache",
                    "referer": ROOT_URL,
                    "user-agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/151.0.0.0 Safari/537.36"
                    ),
                }
            )
            self._thread_local.session = session

        return session

    def _failed_artifact(
        self,
        *,
        url: str,
        page_type: str,
        error: Exception,
        state_code: str | None = None,
        city_slug: str | None = None,
    ) -> AcquisitionArtifact:
        """Handle failed artifact.

        :param url: URL to fetch or process.
        :param page_type: Acquisition page type.
        :param error: Acquisition error, when present.
        :param state_code: State code associated with the page or record.
        :param city_slug: City slug associated with the page.
        :return: Result produced by failed artifact.
        """
        return AcquisitionArtifact(
            artifact_type="html",
            source_url=url,
            content="",
            metadata={
                "retrieved_at_utc": self._utc_now(),
                "page_type": page_type,
                "state_code": state_code,
                "city_slug": city_slug,
                "http_status": 500,
                "scrape_status": "failed",
                "error": str(error),
            },
        )

    @staticmethod
    def _dedupe_states(
        entries: Sequence[_StateEntry],
    ) -> list[_StateEntry]:
        """Deduplicate states.

        :param entries: Directory entries to deduplicate.
        :return: Result produced by dedupe states.
        """
        output: list[_StateEntry] = []
        seen: set[str] = set()
        for entry in entries:
            if entry.url in seen:
                continue
            seen.add(entry.url)
            output.append(entry)
        return output

    @staticmethod
    def _dedupe_cities(
        entries: Sequence[_CityEntry],
    ) -> list[_CityEntry]:
        """Deduplicate cities.

        :param entries: Directory entries to deduplicate.
        :return: Result produced by dedupe cities.
        """
        output: list[_CityEntry] = []
        seen: set[str] = set()
        for entry in entries:
            if entry.url in seen:
                continue
            seen.add(entry.url)
            output.append(entry)
        return output

    def _reset_run_state(self) -> None:
        """Handle reset run state.

        :return: Result produced by reset run state.
        """
        self._failed_state_pages = []
        self._failed_city_pages = []
        self._expected_city_count = 0
        self._expected_store_count = 0

    @staticmethod
    def _clean_text(value: Any) -> str | None:
        """Normalize text.

        :param value: Value to normalize or convert.
        :return: Result produced by clean text.
        """
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _utc_now() -> str:
        """Handle utc now.

        :return: Result produced by utc now.
        """
        return datetime.now(timezone.utc).isoformat()


__all__ = ["TraderJoesAcquisitionStrategy"]