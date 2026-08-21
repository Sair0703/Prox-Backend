from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse, parse_qs
import re
import time

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

from services.store_service.capabilities.store_location_acquisition import (
    AcquisitionArtifact,
    AcquisitionSourceInfo,
    AcquisitionValidationResult,
    StoreLocationAcquisitionStrategy,
)

BASE_URL = "https://locations.dollartree.com"
INDEX_URL = f"{BASE_URL}/index.html"


@dataclass(slots=True)
class _StoreCard:
    """Represent StoreCard used by the acquisition workflow."""
    state_code: str
    city_slug: str
    store_page_url: str
    store_name: str | None = None


class DollarTreeAcquisitionStrategy(StoreLocationAcquisitionStrategy):
    """Represent DollarTreeAcquisitionStrategy used by the acquisition workflow."""
    retailer_key = "dollar_tree"
    retailer_name = "Dollar Tree"

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

    def discover_source(self) -> AcquisitionSourceInfo:
        """Return metadata describing the retailer's official acquisition source."""
        return AcquisitionSourceInfo(
            retailer_key=self.retailer_key,
            retailer_name=self.retailer_name,
            official_website_url="https://www.dollartree.com/",
            store_locator_url=INDEX_URL,
            endpoint_url=INDEX_URL,
            source_type="html",
            provider="BeautifulSoup",
            notes=(
                "Dollar Tree store locator is hierarchical HTML: index -> state "
                "-> city -> store page. Store page is the source of truth for "
                "address, phone, hours, amenities, and store number."
            ),
        )

    def fetch_raw_artifacts(self) -> list[AcquisitionArtifact]:
        """Fetch raw artifacts required for store location acquisition."""
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

        index_html = self._fetch_text(session, INDEX_URL)
        artifacts.append(
            AcquisitionArtifact(
                artifact_type="html",
                source_url=INDEX_URL,
                content=index_html,
                metadata={
                    "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                    "page_type": "index",
                    "http_status": 200,
                },
            )
        )

        state_links = self._parse_state_links(index_html)
        state_pbar = tqdm(
            total=len(state_links),
            desc="Dollar Tree states",
            unit="state",
            leave=True,
        )
        city_pbar = tqdm(desc="Dollar Tree cities", unit="city", leave=True)
        store_pbar = tqdm(desc="Dollar Tree stores", unit="store", leave=True)

        try:
            with ThreadPoolExecutor(max_workers=self.state_workers) as state_pool:
                state_futures = {
                    state_pool.submit(self._fetch_page, session, state_url): (
                        state_code,
                        state_url,
                    )
                    for state_code, state_url in state_links.items()
                }

                for state_future in as_completed(state_futures):
                    state_code, state_url = state_futures[state_future]
                    state_html = state_future.result()

                    artifacts.append(
                        AcquisitionArtifact(
                            artifact_type="html",
                            source_url=state_url,
                            content=state_html,
                            metadata={
                                "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                                "page_type": "state",
                                "state_code": state_code,
                                "http_status": 200,
                            },
                        )
                    )
                    state_pbar.update(1)

                    city_links = self._parse_city_links(state_html, state_code)
                    city_pbar.total = (city_pbar.total or 0) + len(city_links)
                    city_pbar.refresh()

                    with ThreadPoolExecutor(max_workers=self.city_workers) as city_pool:
                        city_futures = {
                            city_pool.submit(self._fetch_page, session, city_url): (
                                state_code,
                                city_slug,
                                city_url,
                            )
                            for city_slug, city_url in city_links.items()
                        }

                        for city_future in as_completed(city_futures):
                            state_code, city_slug, city_url = city_futures[city_future]
                            city_html = city_future.result()

                            artifacts.append(
                                AcquisitionArtifact(
                                    artifact_type="html",
                                    source_url=city_url,
                                    content=city_html,
                                    metadata={
                                        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                                        "page_type": "city",
                                        "state_code": state_code,
                                        "city_slug": city_slug,
                                        "http_status": 200,
                                    },
                                )
                            )
                            city_pbar.update(1)

                            store_cards = self._parse_store_cards(
                                city_html=city_html,
                                state_code=state_code,
                                city_slug=city_slug,
                            )
                            store_pbar.total = (store_pbar.total or 0) + len(store_cards)
                            store_pbar.refresh()

                            with ThreadPoolExecutor(max_workers=self.store_workers) as store_pool:
                                store_futures = {
                                    store_pool.submit(
                                        self._fetch_store_page,
                                        session,
                                        card.store_page_url,
                                        card,
                                    ): card
                                    for card in store_cards
                                }

                                for store_future in as_completed(store_futures):
                                    card = store_future.result()
                                    artifacts.append(
                                        AcquisitionArtifact(
                                            artifact_type="html",
                                            source_url=card["url"],
                                            content=card["html"],
                                            metadata={
                                                "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                                                "page_type": "store",
                                                "state_code": card["state_code"],
                                                "city_slug": card["city_slug"],
                                                "http_status": 200,
                                            },
                                        )
                                    )
                                    store_pbar.update(1)
        finally:
            state_pbar.close()
            city_pbar.close()
            store_pbar.close()
            session.close()

        return artifacts

    def extract_store_payloads(
        self,
        artifacts: list[AcquisitionArtifact],
    ) -> list[dict[str, Any]]:
        """Extract normalized store payloads from acquired artifacts."""
        payloads: dict[str, dict[str, Any]] = {}

        for artifact in artifacts:
            if artifact.metadata.get("page_type") != "store":
                continue

            html = artifact.content or ""
            soup = BeautifulSoup(html, "html.parser")

            store_number = self._extract_store_number_from_shop_now(soup)
            if not store_number:
                continue

            store_name = self._text_or_none(soup.select_one('h1'))
            address = self._extract_address(soup)
            phone = self._extract_phone(soup)
            hours = self._extract_hours(soup)
            amenities = self._extract_amenities(soup)

            row = {
                "retailer": self.retailer_name,
                "store_number": store_number,
                "store_name": store_name,
                "street_address": address.get("street_address"),
                "address_city": address.get("city"),
                "address_state": address.get("state"),
                "zip_code": address.get("zip_code"),
                "full_address": address.get("full_address"),
                "phone": phone,
                "store_url": artifact.source_url,
                "extraction_source": "HTML / BeautifulSoup",
                "scrape_status": "success",
                "http_status": artifact.metadata.get("http_status"),
                "hours": hours,
                "amenities": amenities,
                "state_code": artifact.metadata.get("state_code"),
                "city_slug": artifact.metadata.get("city_slug"),
                "scraped_at_utc": artifact.metadata.get("retrieved_at_utc"),
            }

            payloads[str(store_number)] = row

        return list(payloads.values())

    def validate_store_payloads(
        self,
        payloads: list[dict[str, Any]],
    ) -> AcquisitionValidationResult:
        """Validate acquired store payloads for completeness and uniqueness."""
        total_records = len(payloads)
        store_ids = [self._clean_text(row.get("store_number")) for row in payloads]
        unique_store_ids = len({sid for sid in store_ids if sid})
        missing_store_ids = sum(1 for sid in store_ids if not sid)
        missing_coordinates = 0
        non_us_records = sum(
            1
            for row in payloads
            if self._clean_text(row.get("address_state")) is None
            or self._clean_text(row.get("zip_code")) is None
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
        if non_us_records:
            issue_counts["missing_us_address_fields"] = non_us_records

        notes = [
            "State -> city -> store-page hierarchy is used for discovery only.",
            "Store page is the source of truth for address, phone, hours, and amenities.",
            "Store number is extracted from the Shop Now URL parameter storeId.",
            "Deduplication is done by store_number.",
        ]

        return AcquisitionValidationResult(
            is_valid=missing_store_ids == 0 and non_us_records == 0,
            total_records=total_records,
            unique_store_ids=unique_store_ids,
            missing_store_ids=missing_store_ids,
            missing_coordinates=missing_coordinates,
            non_us_records=non_us_records,
            duplicate_store_ids=duplicate_store_ids,
            issue_counts=issue_counts,
            notes=notes,
        )

    def build_run_notes(self) -> list[str]:
        """Return acquisition source and execution details for the run summary."""
        return [
            "Source: https://locations.dollartree.com/index.html",
            "Method: HTML / BeautifulSoup",
            "Hierarchy: index -> state -> city -> store page",
            "Canonical source: store page",
            "Dedup key: storeId from Shop Now URL",
            "Concurrency: ThreadPoolExecutor",
            f"Workers: state={self.state_workers}, city={self.city_workers}, store={self.store_workers}",
        ]

    def _fetch_page(self, session: requests.Session, url: str) -> str:
        """Fetch page."""
        return self._fetch_text(session, urljoin(BASE_URL + "/", url.lstrip("/")))

    def _fetch_store_page(
        self,
        session: requests.Session,
        store_url: str,
        card: _StoreCard,
    ) -> dict[str, Any]:
        """Fetch store page."""
        html = self._fetch_text(session, urljoin(BASE_URL + "/", store_url.lstrip("/")))
        return {
            "html": html,
            "url": urljoin(BASE_URL + "/", store_url.lstrip("/")),
            "state_code": card.state_code,
            "city_slug": card.city_slug,
        }

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

    def _parse_state_links(self, html: str) -> dict[str, str]:
        """Parse state links."""
        soup = BeautifulSoup(html, "html.parser")
        links: dict[str, str] = {}

        for a in soup.select('a[href]'):
            href = (a.get("href") or "").strip()
            if re.fullmatch(r"[a-z]{2}", href.strip("/")):
                state_code = href.strip("/").lower()
                links[state_code] = href

        return links

    def _parse_city_links(self, html: str, state_code: str) -> dict[str, str]:
        """Parse city links."""
        soup = BeautifulSoup(html, "html.parser")
        links: dict[str, str] = {}

        for a in soup.select('a[href]'):
            href = (a.get("href") or "").strip()
            if href.startswith(f"{state_code}/") and href.count("/") == 1:
                city_slug = href.split("/", 1)[1]
                links[city_slug] = href

        return links

    def _parse_store_cards(
        self,
        *,
        city_html: str,
        state_code: str,
        city_slug: str,
    ) -> list[_StoreCard]:
        """Parse store cards."""
        soup = BeautifulSoup(city_html, "html.parser")
        cards: list[_StoreCard] = []

        for a in soup.select('a[href]'):
            href = (a.get("href") or "").strip()
            if not href.startswith(f"/{state_code}/{city_slug}/"):
                continue

            parts = href.strip("/").split("/")
            if len(parts) != 3:
                continue

            cards.append(
                _StoreCard(
                    state_code=state_code,
                    city_slug=city_slug,
                    store_page_url=href,
                    store_name=self._text_or_none(a),
                )
            )

        if not cards:
            for card in soup.select(".TeaserCard a[href]"):
                href = (card.get("href") or "").strip()
                if not href.startswith(f"/{state_code}/{city_slug}/"):
                    continue
                cards.append(
                    _StoreCard(
                        state_code=state_code,
                        city_slug=city_slug,
                        store_page_url=href,
                        store_name=self._text_or_none(card),
                    )
                )

        return cards

    @staticmethod
    def _extract_store_number_from_shop_now(soup: BeautifulSoup) -> str | None:
        """Extract store number from shop now."""
        for a in soup.select('a[href*="storeId="]'):
            href = a.get("href") or ""
            query = parse_qs(urlparse(href).query)
            store_id = query.get("storeId", [None])[0]
            if store_id:
                return store_id.strip()
        return None

    @staticmethod
    def _extract_address(soup: BeautifulSoup) -> dict[str, str | None]:
        """Extract address."""
        address_root = soup.select_one('div[itemprop="address"]')
        if not address_root:
            return {
                "street_address": None,
                "city": None,
                "state": None,
                "zip_code": None,
                "full_address": None,
            }

        lines: list[str] = []
        for node in address_root.select(".address-line"):
            text = node.get_text(" ", strip=True)
            if text:
                lines.append(text)

        street_address = lines[0] if len(lines) > 0 else None
        city = state = zip_code = None

        if len(lines) > 1:
            parts = [part.strip() for part in lines[1].split(",")]
            if len(parts) >= 3:
                city = parts[0] or None
                state = parts[1] or None
                zip_code = parts[2] or None

        full_address = ", ".join([part for part in [street_address, lines[1] if len(lines) > 1 else None] if part])

        return {
            "street_address": street_address,
            "city": city,
            "state": state,
            "zip_code": zip_code,
            "full_address": full_address or None,
        }

    @staticmethod
    def _extract_phone(soup: BeautifulSoup) -> str | None:
        """Extract phone."""
        phone = soup.select_one('a[href^="tel:"]')
        if not phone:
            return None
        text = phone.get_text(" ", strip=True)
        return text or None

    @staticmethod
    def _extract_hours(soup: BeautifulSoup) -> dict[str, str] | None:
        """Extract hours."""
        tbody = soup.select_one("#dropdown-table-closed")
        if not tbody:
            return None

        hours: dict[str, str] = {}
        for tr in tbody.select("tr"):
            cells = tr.find_all("td")
            if len(cells) != 2:
                continue
            day = cells[0].get_text(" ", strip=True).lower()
            time_text = cells[1].get_text(" ", strip=True)
            if day and time_text:
                hours[day] = time_text

        return hours or None

    @staticmethod
    def _extract_amenities(soup: BeautifulSoup) -> list[str] | None:
        """Extract amenities."""
        section_title = soup.find(lambda tag: tag.name in {"div", "h2"} and tag.get_text(" ", strip=True) == "Local Amenities")
        if section_title:
            container = section_title.parent if section_title.name == "div" else section_title.find_parent()
            if container:
                labels: list[str] = []
                for node in container.select(".text-gray-900"):
                    text = node.get_text(" ", strip=True)
                    if text and text != "Local Amenities" and text not in labels:
                        labels.append(text)
                if labels:
                    return labels

        labels = []
        for img in soup.select('img[alt]'):
            alt = (img.get("alt") or "").strip()
            if alt and alt not in labels:
                labels.append(alt)

        return labels or None

    @staticmethod
    def _text_or_none(node: Any) -> str | None:
        """Extract non-empty text from an HTML node."""
        if node is None:
            return None
        text = node.get_text(" ", strip=True)
        return text or None

    @staticmethod
    def _clean_text(value: Any) -> str | None:
        """Normalize text."""
        if value is None:
            return None
        text = str(value).strip()
        return text or None