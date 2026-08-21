from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
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
    StorePayload,
)

BASE_URL = "https://locations.familydollar.com"
INDEX_URL = f"{BASE_URL}/index.html"


@dataclass(slots=True)
class _StateLink:
    """Represent StateLink used by the acquisition workflow."""
    state_code: str
    href: str


@dataclass(slots=True)
class _CityLink:
    """Represent CityLink used by the acquisition workflow."""
    state_code: str
    city_slug: str
    href: str


class FamilyDollarAcquisitionStrategy(StoreLocationAcquisitionStrategy):
    """Represent FamilyDollarAcquisitionStrategy used by the acquisition workflow."""
    retailer_key = "family_dollar"
    retailer_name = "Family Dollar"

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
            official_website_url="https://www.familydollar.com/",
            store_locator_url=INDEX_URL,
            endpoint_url=INDEX_URL,
            source_type="html",
            provider="BeautifulSoup",
            notes=(
                "Family Dollar store locator is hierarchical HTML: state index -> "
                "state city list -> city store cards. City cards expose address, "
                "phone, and View Store Page links. Store pages are used as a "
                "best-effort fallback to resolve retailer_store_id when needed."
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
                    "page_type": "state_index",
                    "http_status": 200,
                },
            )
        )

        state_links = self._parse_state_links(index_html)
        state_pbar = tqdm(
            total=len(state_links),
            desc="Family Dollar states",
            unit="state",
            leave=True,
        )
        city_pbar = tqdm(desc="Family Dollar cities", unit="city", leave=True)

        try:
            state_html_map: dict[str, tuple[str, str]] = {}

            with ThreadPoolExecutor(max_workers=self.state_workers) as pool:
                futures = {
                    pool.submit(self._fetch_text, session, urljoin(BASE_URL + "/", state_link.href)): state_link
                    for state_link in state_links
                }

                for future in as_completed(futures):
                    state_link = futures[future]
                    state_html = future.result()
                    state_html_map[state_link.state_code] = (state_link.href, state_html)

                    artifacts.append(
                        AcquisitionArtifact(
                            artifact_type="html",
                            source_url=urljoin(BASE_URL + "/", state_link.href),
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

            with ThreadPoolExecutor(max_workers=self.city_workers) as pool:
                futures = {
                    pool.submit(self._fetch_text, session, urljoin(BASE_URL + "/", city_link.href)): city_link
                    for city_link in city_jobs
                }

                for future in as_completed(futures):
                    city_link = futures[future]
                    city_html = future.result()

                    artifacts.append(
                        AcquisitionArtifact(
                            artifact_type="html",
                            source_url=urljoin(BASE_URL + "/", city_link.href),
                            content=city_html,
                            metadata={
                                "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                                "page_type": "city",
                                "state_code": city_link.state_code,
                                "city_slug": city_link.city_slug,
                                "http_status": 200,
                            },
                        )
                    )
                    city_pbar.update(1)

        finally:
            state_pbar.close()
            city_pbar.close()
            session.close()

        return artifacts

    def extract_store_payloads(
        self,
        artifacts: Sequence[AcquisitionArtifact],
    ) -> list[StorePayload]:
        """Extract normalized store payloads from acquired artifacts."""
        city_artifacts = [
            artifact for artifact in artifacts if artifact.metadata.get("page_type") == "city"
        ]

        preliminary_rows: list[dict[str, Any]] = []
        pbar = tqdm(
            total=len(city_artifacts),
            desc="Parsing Family Dollar stores",
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
                    preliminary_rows.extend(rows)
                    pbar.update(1)
        finally:
            pbar.close()

        # Best-effort resolve retailer_store_id from store pages in parallel.
        unresolved_indices = [
            i for i, row in enumerate(preliminary_rows)
            if not self._clean_text(row.get("retailer_store_id"))
        ]

        if unresolved_indices:
            with ThreadPoolExecutor(max_workers=self.store_workers) as pool:
                futures = {
                    pool.submit(self._resolve_store_id_from_page, preliminary_rows[i]["store_url"]): i
                    for i in unresolved_indices
                    if self._clean_text(preliminary_rows[i].get("store_url"))
                }

                for future in as_completed(futures):
                    idx = futures[future]
                    store_id = future.result()
                    if store_id:
                        preliminary_rows[idx]["retailer_store_id"] = store_id
                        preliminary_rows[idx]["store_number"] = store_id

        # Deduplicate by retailer_store_id when available, otherwise by store_url.
        deduped: dict[str, dict[str, Any]] = {}
        for row in preliminary_rows:
            key = self._clean_text(row.get("retailer_store_id")) or self._clean_text(row.get("store_url"))
            if not key:
                continue
            deduped[key] = row

        return list(deduped.values())

    def validate_store_payloads(
        self,
        payloads: Sequence[StorePayload],
    ) -> AcquisitionValidationResult:
        """Validate acquired store payloads for completeness and uniqueness."""
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
            "City pages are parsed directly from HTML.",
            "City cards provide address and phone; store pages are used as a best-effort fallback to resolve retailer_store_id.",
            "Canonical dedup key is retailer_store_id.",
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
        """Return acquisition source and execution details for the run summary."""
        return [
            "Source: https://locations.familydollar.com/index.html",
            "Method: HTML / BeautifulSoup",
            "Hierarchy: state index -> state city list -> city store cards",
            "Canonical dedup key: retailer_store_id",
            f"Workers: state={self.state_workers}, city={self.city_workers}, store={self.store_workers}",
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

    def _parse_state_links(self, html: str) -> list[_StateLink]:
        """Parse state links."""
        soup = BeautifulSoup(html, "html.parser")
        links: list[_StateLink] = []

        for a in soup.select('a[href]'):
            href = (a.get("href") or "").strip().lstrip("/")
            if re.fullmatch(r"[a-z]{2}", href):
                links.append(_StateLink(state_code=href.upper(), href=href))

        deduped: list[_StateLink] = []
        seen: set[str] = set()
        for link in links:
            if link.state_code in seen:
                continue
            seen.add(link.state_code)
            deduped.append(link)

        return deduped

    def _parse_city_links(self, html: str, state_code: str) -> list[_CityLink]:
        """Parse city links."""
        soup = BeautifulSoup(html, "html.parser")
        links: list[_CityLink] = []

        state_code = state_code.lower()

        for a in soup.select('a[href]'):
            href = (a.get("href") or "").strip().lstrip("/")
            if not href:
                continue

            parts = href.split("/")
            if len(parts) != 2:
                continue

            if parts[0].lower() != state_code:
                continue

            city_slug = parts[1].strip()
            if not city_slug:
                continue

            links.append(
                _CityLink(
                    state_code=state_code.upper(),
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
        """Parse city artifact."""
        html = artifact.content or ""
        soup = BeautifulSoup(html, "html.parser")

        state_code = self._clean_text(artifact.metadata.get("state_code"))
        city_slug = self._clean_text(artifact.metadata.get("city_slug"))
        source_url = self._clean_text(artifact.source_url)

        rows: list[StorePayload] = []

        for card in soup.select(".TeaserCard"):
            link = self._find_store_link(card, state_code, city_slug)
            if not link:
                continue

            store_url = urljoin(BASE_URL + "/", link.get("href") or "")
            store_name = self._clean_text(self._text_or_none(card.select_one("h2")))

            address = self._extract_address_from_card(card)
            phone = self._extract_phone(card)
            amenities = self._extract_amenities(card)

            rows.append(
                {
                    "retailer": self.retailer_name,
                    "retailer_store_id": None,
                    "store_number": None,
                    "store_type": "Regular",
                    "store_name": store_name,
                    "address": address.get("street_address"),
                    "street_address": address.get("street_address"),
                    "city": address.get("city"),
                    "state": address.get("state") or state_code,
                    "zip_code": address.get("zip_code"),
                    "full_address": address.get("full_address"),
                    "phone": phone,
                    "store_url": store_url,
                    "source_url": source_url,
                    "source_sitemap": None,
                    "city_slug": city_slug,
                    "amenities": amenities,
                    "extraction_source": "Family Dollar city card HTML",
                    "scrape_status": "success",
                    "http_status": artifact.metadata.get("http_status"),
                    "scraped_at_utc": artifact.metadata.get("retrieved_at_utc"),
                }
            )

        return rows

    def _resolve_store_id_from_page(self, store_url: str) -> str | None:
        """
        Best-effort fallback for Family Dollar store page lookup.
        """
        if not store_url:
            return None

        try:
            response = requests.get(
                store_url,
                timeout=self.request_timeout,
                headers={
                    "user-agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/151.0.0.0 Safari/537.36"
                    )
                },
            )
            response.raise_for_status()
            html = response.text
        except Exception:
            return None

        patterns = [
            r'["\']retailer_store_id["\']\s*:\s*["\']?(\d+)["\']?',
            r'["\']storeId["\']\s*:\s*["\']?(\d+)["\']?',
            r'["\']id["\']\s*:\s*["\']?(\d+)["\']?',
            r'data-store-id=["\'](\d+)["\']',
            r'id=(\d+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, html, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip()

        soup = BeautifulSoup(html, "html.parser")

        for node in soup.select("[data-store-id], [data-storeid]"):
            value = node.get("data-store-id") or node.get("data-storeid")
            if value and str(value).strip().isdigit():
                return str(value).strip()

        text = soup.get_text(" ", strip=True)
        match = re.search(r"store\s*id[:\s]+(\d+)", text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()

        return None

    def _find_store_link(
        self,
        card: BeautifulSoup,
        state_code: str | None,
        city_slug: str | None,
    ) -> Any | None:
        """Find store link."""
        links = card.select("a[href]")
        if not links:
            return None

        preferred: list[Any] = []
        fallback: list[Any] = []

        for link in links:
            href = (link.get("href") or "").strip()
            if not href:
                continue

            text = link.get_text(" ", strip=True).lower()
            href_norm = href.lstrip("/")

            if "view store page" in text:
                preferred.append(link)
                continue

            if state_code and city_slug and href_norm.startswith(f"{state_code.lower()}/{city_slug}"):
                preferred.append(link)
                continue

            fallback.append(link)

        if preferred:
            return preferred[0]
        if fallback:
            return fallback[0]
        return None

    def _extract_address_from_card(
        self,
        card: BeautifulSoup,
    ) -> dict[str, str | None]:
        """Extract address from card."""
        street_address = None
        city = None
        state = None
        zip_code = None

        address_lines = [
            node.get_text(" ", strip=True)
            for node in card.select(".address-line")
            if node.get_text(" ", strip=True)
        ]

        if address_lines:
            street_address = address_lines[0]
            if len(address_lines) > 1:
                city_state_zip = address_lines[1]
                city, state, zip_code = self._parse_city_state_zip(city_state_zip)

        if not street_address:
            strong = card.select_one("strong")
            if strong:
                street_address = self._clean_text(strong.get_text(" ", strip=True))

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
    def _parse_city_state_zip(text: str | None) -> tuple[str | None, str | None, str | None]:
        """Parse city state zip."""
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
    def _extract_phone(card: BeautifulSoup) -> str | None:
        """Extract phone."""
        phone = card.select_one('a[href^="tel:"]')
        if not phone:
            return None
        return phone.get_text(" ", strip=True) or None

    @staticmethod
    def _extract_amenities(card: BeautifulSoup) -> list[str] | None:
        """Extract amenities."""
        amenities: list[str] = []

        for node in card.select(".flex.flex-row.items-center .text-gray-900, .text-gray-900"):
            text = node.get_text(" ", strip=True)
            if text and text not in amenities:
                amenities.append(text)

        for img in card.select("img[alt]"):
            alt = (img.get("alt") or "").strip()
            if alt and alt not in amenities:
                amenities.append(alt)

        return amenities or None

    @staticmethod
    def _compose_full_address(
        *,
        street_address: str | None,
        city: str | None,
        state: str | None,
        zip_code: str | None,
    ) -> str | None:
        """Handle compose full address."""
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
        if isinstance(value, str):
            text = value.strip()
            return text or None
        text = str(value).strip()
        return text or None