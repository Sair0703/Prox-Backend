from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re
import threading
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

from services.store_service.capabilities.store_location_acquisition.protocals import (
    AcquisitionArtifact,
    AcquisitionSourceInfo,
    AcquisitionValidationResult,
    StoreLocationAcquisitionStrategy,
)

STATE_LINK_SELECTOR = 'a[href^="/store-locator/cvs-pharmacy-locations/"]'
CITY_LINK_SELECTOR = 'a[href^="/store-locator/cvs-pharmacy-locations/"]'
STORE_CARD_SELECTOR = "cvs-store-search-tile"
ADDRESS_SELECTOR = "h2.address-sec"
PHONE_SELECTOR = 'div.phone-number a[href^="tel:"]'
DETAILS_SELECTOR = 'a[href*="/store-locator/"][href*="storeid="]'
HOURS_SELECTOR = "cvs-store-hours ul.hours-list li"
SERVICES_SELECTOR = "cvs-store-services ul.store-services-sec li"

PHONE_RE = re.compile(
    r"(?<!\d)(?:\+1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)"
)
CITY_STATE_ZIP_RE = re.compile(
    r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2}),\s*(?P<zip>\d{5}(?:-\d{4})?)$"
)
STORE_ID_RE = re.compile(r"storeid=(?P<store_id>\d+)$", re.IGNORECASE)
COUNT_RE = re.compile(r"\((\d+)\)\s*$")


@dataclass(frozen=True, slots=True)
class StateEntry:
    """Represent StateEntry data used by the acquisition strategy."""
    state_name: str
    state_slug: str
    state_url: str
    store_count: int | None = None


@dataclass(frozen=True, slots=True)
class CityEntry:
    """Represent CityEntry data used by the acquisition strategy."""
    state_name: str
    state_slug: str
    city_name: str
    city_slug: str
    city_url: str
    store_count: int | None = None


class CVSAcquisitionStrategy(StoreLocationAcquisitionStrategy):
    """Represent CVSAcquisitionStrategy data used by the acquisition strategy."""
    retailer_key = "cvs"
    retailer_name = "CVS Pharmacy"

    official_website_url = "https://www.cvs.com/"
    store_locator_url = "https://www.cvs.com/store-locator/cvs-pharmacy-locations"
    store_index_url = store_locator_url

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        user_agent: str | None = None,
        state_workers: int = 12,
        city_workers: int = 20,
    ) -> None:
        """Initialize acquisition configuration and run state."""
        self._timeout_seconds = timeout_seconds
        self._state_workers = state_workers
        self._city_workers = city_workers
        self._thread_local = threading.local()
        self._headers = {
            "accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,*/*;q=0.8"
            ),
            "accept-language": "en-US,en;q=0.9",
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "referer": self.store_index_url,
            "user-agent": user_agent
            or (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/151.0.0.0 Safari/537.36"
            ),
        }

    def discover_source(self) -> AcquisitionSourceInfo:
        """Return metadata describing the retailer's official acquisition source."""
        return AcquisitionSourceInfo(
            retailer_key=self.retailer_key,
            retailer_name=self.retailer_name,
            official_website_url=self.official_website_url,
            store_locator_url=self.store_locator_url,
            endpoint_url=self.store_index_url,
            source_type="html",
            provider="www.cvs.com",
            notes=(
                "Public HTML store index, state pages, city pages, and store tiles. "
                "The state index page lists state links; each state page lists city links; "
                "each city page renders store cards with address, phone, hours, services, "
                "and a store details link containing the store id."
            ),
        )

    def fetch_raw_artifacts(self) -> list[AcquisitionArtifact]:
        """Fetch raw artifacts required for store location acquisition."""
        artifacts: list[AcquisitionArtifact] = []

        index_html = self._fetch_html(self.store_index_url)
        states = self._extract_state_entries(index_html)

        artifacts.append(
            AcquisitionArtifact(
                artifact_type="raw_html",
                source_url=self.store_index_url,
                file_path=None,
                content=index_html,
                metadata={
                    "retailer_key": self.retailer_key,
                    "retailer_name": self.retailer_name,
                    "page_type": "state_index",
                    "state_count": len(states),
                    "http_status": 200,
                    "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                },
            )
        )

        print(f"Found {len(states)} states")

        all_cities: list[CityEntry] = []
        state_artifacts: list[AcquisitionArtifact] = []

        with ThreadPoolExecutor(max_workers=self._state_workers) as executor:
            futures = {
                executor.submit(self._fetch_state_artifact, state): state
                for state in states
            }

            for future in tqdm(
                as_completed(futures),
                total=len(futures),
                desc="Fetching states",
                unit="state",
            ):
                artifact, cities = future.result()
                state_artifacts.append(artifact)
                all_cities.extend(cities)

        artifacts.extend(state_artifacts)

        print(f"Found {len(all_cities)} cities")
        expected_store_count = sum(city.store_count or 0 for city in all_cities)
        print(f"Expected stores: {expected_store_count}")

        city_artifacts: list[AcquisitionArtifact] = []

        with ThreadPoolExecutor(max_workers=self._city_workers) as executor:
            futures = {
                executor.submit(self._fetch_city_artifact, city): city
                for city in all_cities
            }

            for future in tqdm(
                as_completed(futures),
                total=len(futures),
                desc="Fetching city stores",
                unit="city",
            ):
                artifact = future.result()
                city_artifacts.append(artifact)

        artifacts.extend(city_artifacts)
        return artifacts

    def extract_store_payloads(
        self,
        artifacts: Sequence[AcquisitionArtifact],
    ) -> list[dict[str, Any]]:
        """Extract normalized store payloads from acquired artifacts."""
        payloads: list[dict[str, Any]] = []
        seen_keys: set[str] = set()

        city_artifacts = [
            artifact
            for artifact in artifacts
            if self._clean_text(artifact.metadata.get("page_type")) == "city_page"
        ]

        with ThreadPoolExecutor(max_workers=self._city_workers) as executor:
            futures = {
                executor.submit(self._extract_city_payloads_from_artifact, artifact): artifact
                for artifact in city_artifacts
            }

            for future in tqdm(
                as_completed(futures),
                total=len(futures),
                desc="Extracting stores",
                unit="city_page",
            ):
                page_payloads = future.result()
                for payload in page_payloads:
                    dedupe_key = self._dedupe_key(payload)
                    if dedupe_key in seen_keys:
                        continue
                    seen_keys.add(dedupe_key)
                    payloads.append(payload)

        return payloads

    def validate_store_payloads(
        self,
        payloads: Sequence[dict[str, Any]],
    ) -> AcquisitionValidationResult:
        """Validate acquired store payloads for completeness and uniqueness."""
        seen_store_ids: set[str] = set()
        duplicate_store_ids: list[str] = []
        issue_counts: dict[str, int] = {}

        missing_store_ids = 0
        missing_address_components = 0

        for payload in payloads:
            store_id = self._clean_text(payload.get("retailer_store_id"))
            address = self._clean_text(payload.get("address"))
            city = self._clean_text(payload.get("city"))
            state = self._clean_text(payload.get("state"))
            zip_code = self._clean_text(payload.get("zip_code"))

            if store_id is None:
                missing_store_ids += 1
                issue_counts["missing_store_id"] = issue_counts.get("missing_store_id", 0) + 1
            elif store_id in seen_store_ids:
                duplicate_store_ids.append(store_id)
            else:
                seen_store_ids.add(store_id)

            if not (address and city and state and zip_code):
                missing_address_components += 1
                issue_counts["missing_address"] = issue_counts.get("missing_address", 0) + 1

        total_records = len(payloads)
        unique_store_ids = len(seen_store_ids)

        is_valid = (
            total_records > 0
            and missing_store_ids == 0
            and missing_address_components == 0
            and not duplicate_store_ids
        )

        notes: list[str] = []
        if total_records == 0:
            notes.append("No payloads were collected from the CVS source.")
        if duplicate_store_ids:
            notes.append(
                f"Duplicate retailer_store_id values detected: {sorted(set(duplicate_store_ids))[:10]}"
            )
        if unique_store_ids != total_records:
            notes.append(
                f"Payload count ({total_records}) differs from unique store id count ({unique_store_ids}); deduplication was required."
            )

        return AcquisitionValidationResult(
            is_valid=is_valid,
            total_records=total_records,
            unique_store_ids=unique_store_ids,
            missing_store_ids=missing_store_ids,
            missing_coordinates=0,
            non_us_records=0,
            duplicate_store_ids=sorted(set(duplicate_store_ids)),
            issue_counts=issue_counts,
            notes=notes,
        )

    def build_run_notes(self) -> list[str]:
        """Return acquisition source and execution details for the run summary."""
        return [
            "Discovery source: CVS Pharmacy store index, state pages, and city pages.",
            "Acquisition mechanism: HTML parsing with BeautifulSoup.",
            "Normalization key: retailer store id extracted from the store details URL with fallback dedupe on store URL and address.",
            "Coverage plan: parse all state links from the index page, all city links from each state page, then parse each city store tile.",
        ]

    def _get_session(self) -> requests.Session:
        """Return session."""
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(self._headers)
            self._thread_local.session = session
        return session

    def _fetch_html(self, url: str) -> str:
        """Fetch html."""
        session = self._get_session()
        response = session.get(url, timeout=self._timeout_seconds)
        response.raise_for_status()
        return response.text

    def _fetch_state_artifact(
        self,
        state: StateEntry,
    ) -> tuple[AcquisitionArtifact, list[CityEntry]]:
        """Fetch state artifact."""
        state_html = self._fetch_html(state.state_url)
        cities = self._extract_city_entries(
            state_html,
            state_slug=state.state_slug,
            state_name=state.state_name,
        )

        artifact = AcquisitionArtifact(
            artifact_type="raw_html",
            source_url=state.state_url,
            file_path=None,
            content=state_html,
            metadata={
                "retailer_key": self.retailer_key,
                "retailer_name": self.retailer_name,
                "page_type": "state_page",
                "state_name": state.state_name,
                "state_slug": state.state_slug,
                "expected_city_count": state.store_count,
                "city_count": len(cities),
                "http_status": 200,
                "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
            },
        )

        return artifact, cities

    def _fetch_city_artifact(
        self,
        city: CityEntry,
    ) -> AcquisitionArtifact:
        """Fetch city artifact."""
        city_html = self._fetch_html(city.city_url)
        store_count = self._count_store_cards(city_html)

        return AcquisitionArtifact(
            artifact_type="raw_html",
            source_url=city.city_url,
            file_path=None,
            content=city_html,
            metadata={
                "retailer_key": self.retailer_key,
                "retailer_name": self.retailer_name,
                "page_type": "city_page",
                "state_name": city.state_name,
                "state_slug": city.state_slug,
                "city_name": city.city_name,
                "city_slug": city.city_slug,
                "expected_store_count": city.store_count,
                "store_count": store_count,
                "http_status": 200,
                "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
            },
        )

    def _extract_city_payloads_from_artifact(
        self,
        artifact: AcquisitionArtifact,
    ) -> list[dict[str, Any]]:
        """Extract city payloads from artifact."""
        state_name = self._clean_text(artifact.metadata.get("state_name"))
        state_slug = self._clean_text(artifact.metadata.get("state_slug"))
        city_name = self._clean_text(artifact.metadata.get("city_name"))
        city_slug = self._clean_text(artifact.metadata.get("city_slug"))
        source_page_url = self._clean_text(artifact.source_url)

        if not source_page_url:
            return []

        return self._extract_store_payloads_from_city_html(
            html=artifact.content,
            source_page_url=source_page_url,
            state_name=state_name,
            state_slug=state_slug,
            city_name=city_name,
            city_slug=city_slug,
        )

    def _extract_state_entries(self, html: str) -> list[StateEntry]:
        """Extract state entries."""
        soup = BeautifulSoup(html, "html.parser")
        entries: list[StateEntry] = []
        seen_urls: set[str] = set()

        for anchor in soup.select(STATE_LINK_SELECTOR):
            href = self._clean_text(anchor.get("href"))
            if not href:
                continue

            state_url = urljoin(self.store_index_url, href)
            if state_url in seen_urls:
                continue

            state_slug = self._state_slug_from_url(state_url)
            if not state_slug:
                continue

            anchor_text = self._clean_text(anchor.get_text(" ", strip=True)) or ""
            state_name = anchor_text
            if state_name.lower().endswith(" stores"):
                state_name = state_name[:-7].strip()
            state_name = re.sub(r"\s*\(\d+\)\s*$", "", state_name).strip() or state_slug.replace("-", " ")
            store_count = self._parse_count_from_anchor_text(anchor_text)

            entries.append(
                StateEntry(
                    state_name=state_name,
                    state_slug=state_slug,
                    state_url=state_url,
                    store_count=store_count,
                )
            )
            seen_urls.add(state_url)

        return entries

    def _extract_city_entries(
        self,
        html: str,
        *,
        state_name: str,
        state_slug: str,
    ) -> list[CityEntry]:
        """Extract city entries."""
        soup = BeautifulSoup(html, "html.parser")
        entries: list[CityEntry] = []
        seen_urls: set[str] = set()

        for anchor in soup.select(CITY_LINK_SELECTOR):
            href = self._clean_text(anchor.get("href"))
            if not href:
                continue

            city_url = urljoin(self.store_index_url, href)
            if city_url in seen_urls:
                continue

            city_slug = self._city_slug_from_url(city_url, state_slug=state_slug)
            if not city_slug:
                continue

            anchor_text = self._clean_text(anchor.get_text(" ", strip=True)) or ""
            city_name = anchor_text
            if city_name.lower().endswith(" stores"):
                city_name = city_name[:-7].strip()
            city_name = re.sub(r"\s*\(\d+\)\s*$", "", city_name).strip()
            if not city_name:
                continue

            store_count = self._parse_count_from_anchor_text(anchor_text)
            entries.append(
                CityEntry(
                    state_name=state_name,
                    state_slug=state_slug,
                    city_name=city_name,
                    city_slug=city_slug,
                    city_url=city_url,
                    store_count=store_count,
                )
            )
            seen_urls.add(city_url)

        return entries

    def _extract_store_payloads_from_city_html(
        self,
        *,
        html: str,
        source_page_url: str,
        state_name: str | None,
        state_slug: str | None,
        city_name: str | None,
        city_slug: str | None,
    ) -> list[dict[str, Any]]:
        """Extract store payloads from city html."""
        soup = BeautifulSoup(html, "html.parser")
        payloads: list[dict[str, Any]] = []

        for card in soup.select(STORE_CARD_SELECTOR):
            payload = self._parse_store_card(
                card,
                source_page_url=source_page_url,
                state_name=state_name,
                state_slug=state_slug,
                city_name=city_name,
                city_slug=city_slug,
            )
            if payload is not None:
                payloads.append(payload)

        return payloads

    def _parse_store_card(
        self,
        card: Any,
        *,
        source_page_url: str,
        state_name: str | None,
        state_slug: str | None,
        city_name: str | None,
        city_slug: str | None,
    ) -> dict[str, Any] | None:
        """Parse store card."""
        address_tag = card.select_one(ADDRESS_SELECTOR)
        if address_tag is None:
            return None

        address_lines = [
            self._clean_text(line)
            for line in address_tag.get_text("\n", strip=True).splitlines()
        ]
        address_lines = [line for line in address_lines if line]
        if not address_lines:
            return None

        street_address, address_city, address_state, zip_code = self._parse_address_lines(address_lines)
        full_address = self._join_address_parts(street_address, address_city, address_state, zip_code)

        phone_tag = card.select_one(PHONE_SELECTOR)
        phone = self._clean_text(phone_tag.get_text(" ", strip=True)) if phone_tag else None

        details_tag = card.select_one(DETAILS_SELECTOR)
        store_url = None
        store_number = None
        if details_tag is not None:
            href = self._clean_text(details_tag.get("href"))
            if href:
                store_url = urljoin(source_page_url, href)
                store_number = self._store_id_from_details_url(store_url)

        hours = self._parse_hours(card)
        services = self._parse_services(card)

        store_name = street_address

        return {
            "retailer": self.retailer_name,
            "retailer_key": self.retailer_key,
            "retailer_store_id": store_number,
            "store_number": store_number,
            "store_name": store_name,
            "store_url": store_url,
            "source_url": source_page_url,
            "source_type": "html",
            "provider": "www.cvs.com",
            "state_code": state_slug,
            "state_name": state_name,
            "city_code": city_slug,
            "city_name": city_name,
            "status": hours.get("status"),
            "store_type": "Regular",
            "address": street_address,
            "street_address": street_address,
            "address_line1": street_address,
            "address_line2": None,
            "city": address_city,
            "state": address_state,
            "zip_code": zip_code,
            "phone": phone,
            "full_address": full_address,
            "services": services,
            "store_hours": hours.get("store_hours"),
            "pharmacy_hours": hours.get("pharmacy_hours"),
            "pharmacy_break": hours.get("pharmacy_break"),
            "country": "United States",
            "latitude": self._extract_latitude_from_directions(card),
            "longitude": self._extract_longitude_from_directions(card),
            "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
            "extraction_source": "city_page",
        }

    def _parse_address_lines(
        self,
        lines: Sequence[str],
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Parse address lines."""
        street_address = None
        address_city = None
        address_state = None
        zip_code = None

        if len(lines) == 1:
            street_address = lines[0]
            return street_address, None, None, None

        street_address = " | ".join(lines[:-1]).strip() if len(lines) > 2 else lines[0]

        last_line = lines[-1]
        match = CITY_STATE_ZIP_RE.match(last_line)
        if match:
            address_city = self._clean_text(match.group("city"))
            address_state = self._clean_text(match.group("state"))
            zip_code = self._clean_text(match.group("zip"))
        elif len(lines) > 1:
            street_address = " | ".join(lines[:-1]).strip()

        return street_address, address_city, address_state, zip_code

    def _parse_hours(self, card: Any) -> dict[str, str | None]:
        """Parse hours."""
        store_hours: list[str] = []
        pharmacy_hours: list[str] = []
        status = None
        pharmacy_break = None

        for li in card.select(HOURS_SELECTOR):
            text = self._clean_text(li.get_text(" ", strip=True))
            if not text:
                continue

            lowered = text.lower()
            if lowered.startswith("store & photo:"):
                store_hours.append(text)
                if "open" in lowered:
                    status = "open"
                elif "closed" in lowered:
                    status = "closed"
            elif lowered.startswith("pharmacy:"):
                pharmacy_hours.append(text)
                if "open" in lowered:
                    status = status or "open"
                elif "closed" in lowered:
                    status = status or "closed"
            elif "lunch" in lowered:
                pharmacy_break = text

        return {
            "status": status,
            "store_hours": " | ".join(store_hours) if store_hours else None,
            "pharmacy_hours": " | ".join(pharmacy_hours) if pharmacy_hours else None,
            "pharmacy_break": pharmacy_break,
        }

    def _parse_services(self, card: Any) -> list[str]:
        """Parse services."""
        services: list[str] = []
        seen: set[str] = set()

        for li in card.select(SERVICES_SELECTOR):
            text = self._clean_text(li.get_text(" ", strip=True))
            if not text:
                continue

            text = re.sub(r"^Available\s*", "", text, flags=re.IGNORECASE).strip()
            if not text:
                continue

            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            services.append(text)

        return services

    def _extract_latitude_from_directions(self, card: Any) -> float | None:
        """Extract latitude from directions."""
        directions = card.select_one('a[href*="bing.com/maps/default.aspx"]')
        if directions is None:
            return None
        href = directions.get("href") or ""
        parsed = urlparse(href)
        query = parse_qs(parsed.query)
        cp = query.get("cp", [])
        if not cp:
            return None
        parts = cp[0].split("~")
        if not parts:
            return None
        return self._coerce_float(parts[0])

    def _extract_longitude_from_directions(self, card: Any) -> float | None:
        """Extract longitude from directions."""
        directions = card.select_one('a[href*="bing.com/maps/default.aspx"]')
        if directions is None:
            return None
        href = directions.get("href") or ""
        parsed = urlparse(href)
        query = parse_qs(parsed.query)
        cp = query.get("cp", [])
        if not cp:
            return None
        parts = cp[0].split("~")
        if len(parts) < 2:
            return None
        return self._coerce_float(parts[1])

    def _dedupe_key(self, payload: Mapping[str, Any]) -> str:
        """Deduplicate key."""
        store_id = self._clean_text(payload.get("retailer_store_id"))
        if store_id:
            return f"store_id:{store_id}"

        store_url = self._normalize_key_piece(payload.get("store_url"))
        address = self._normalize_key_piece(payload.get("address"))
        city = self._normalize_key_piece(payload.get("city"))
        state = self._normalize_key_piece(payload.get("state"))
        zip_code = self._normalize_key_piece(payload.get("zip_code"))
        return f"fallback:{store_url}|{address}|{city}|{state}|{zip_code}"

    def _count_store_cards(self, html: str) -> int:
        """Count store cards."""
        soup = BeautifulSoup(html, "html.parser")
        return len(soup.select(STORE_CARD_SELECTOR))

    @staticmethod
    def _state_slug_from_url(url: str) -> str | None:
        """Handle state slug from url."""
        path = urlparse(url).path.strip("/")
        parts = [part for part in path.split("/") if part]
        if len(parts) != 3:
            return None
        if parts[0] != "store-locator":
            return None
        if parts[1] != "cvs-pharmacy-locations":
            return None
        return parts[2]

    @staticmethod
    def _city_slug_from_url(url: str, *, state_slug: str) -> str | None:
        """Handle city slug from url."""
        path = urlparse(url).path.strip("/")
        parts = [part for part in path.split("/") if part]
        if len(parts) != 4:
            return None
        if parts[0] != "store-locator":
            return None
        if parts[1] != "cvs-pharmacy-locations":
            return None
        if parts[2] != state_slug:
            return None
        return parts[3]

    @staticmethod
    def _store_id_from_details_url(url: str | None) -> str | None:
        """Handle store id from details url."""
        if not url:
            return None
        match = STORE_ID_RE.search(url)
        if not match:
            return None
        return match.group("store_id")

    @staticmethod
    def _parse_count_from_anchor_text(text: str) -> int | None:
        """Parse count from anchor text."""
        match = COUNT_RE.search(text)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    @staticmethod
    def _join_address_parts(*parts: str | None) -> str | None:
        """Join address parts."""
        values = [part for part in parts if part]
        if not values:
            return None
        return ", ".join(values)

    @staticmethod
    def _clean_text(value: Any) -> str | None:
        """Normalize text."""
        if value is None:
            return None
        if isinstance(value, str):
            text = value.replace("\xa0", " ").strip()
            text = re.sub(r"\s+", " ", text)
            return text or None
        text = str(value).replace("\xa0", " ").strip()
        text = re.sub(r"\s+", " ", text)
        return text or None

    @staticmethod
    def _normalize_key_piece(value: Any) -> str:
        """Normalize key piece."""
        text = CVSAcquisitionStrategy._clean_text(value) or ""
        text = text.lower()
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"[^a-z0-9 ]", "", text)
        return text.strip()

    @staticmethod
    def _coerce_float(value: Any) -> float | None:
        """Convert float."""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            if math.isnan(float(value)):
                return None
            return float(value)
        text = str(value).strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None


__all__ = ["CVSAcquisitionStrategy", "StateEntry", "CityEntry"]