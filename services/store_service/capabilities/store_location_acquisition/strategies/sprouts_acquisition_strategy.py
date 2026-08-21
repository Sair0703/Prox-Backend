# services/store_service/capabilities/store_location_acquisition/strategies/sprouts_acquisition_strategy.py

"""Acquisition strategy for Sprouts Farmers Market store locations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from services.store_service.capabilities.store_location_acquisition.protocols import (
        AcquisitionArtifact,
        AcquisitionSourceInfo,
        AcquisitionValidationResult,
        StoreLocationAcquisitionStrategy,
    )
except ImportError:  # pragma: no cover
    from services.store_service.capabilities.store_location_acquisition.protocals import (
        AcquisitionArtifact,
        AcquisitionSourceInfo,
        AcquisitionValidationResult,
        StoreLocationAcquisitionStrategy,
    )


STATE_CARD_SELECTOR = "div.store-states a[href]"
STORE_CARD_SELECTOR = "div.list-by-state"

PHONE_RE = re.compile(
    r"(?<!\d)(?:\+1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)"
)
CITY_STATE_ZIP_RE = re.compile(
    r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$"
)
STORE_NUMBER_RE = re.compile(r"(\d+)")
OPENING_DATE_RE = re.compile(r"Opening\s+(?P<date>.+)$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class StateEntry:
    """State directory entry discovered from the Sprouts store index."""

    state_code: str
    state_name: str
    state_url: str
    store_count: int | None = None


class SproutsAcquisitionStrategy(StoreLocationAcquisitionStrategy):
    """
    Acquires Sprouts store locations from the official store directory.

    Uses the public store index to discover state pages, then parses the
    official store cards rendered on each state page.
    """

    retailer_key = "sprouts"
    retailer_name = "Sprouts Farmers Market"

    official_website_url = "https://www.sprouts.com/"
    store_locator_url = "https://www.sprouts.com/stores/"
    store_index_url = "https://www.sprouts.com/stores/"

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        user_agent: str | None = None,
    ) -> None:
        """
        Initialize the Sprouts acquisition strategy.

        :param timeout_seconds: HTTP request timeout in seconds.
        :param user_agent: Optional user-agent used for store-page requests.
        """
        self._timeout_seconds = timeout_seconds
        self._session = requests.Session()
        self._session.headers.update(
            {
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
        )

    def discover_source(self) -> AcquisitionSourceInfo:
        """Describe the official Sprouts acquisition source."""
        return AcquisitionSourceInfo(
            retailer_key=self.retailer_key,
            retailer_name=self.retailer_name,
            official_website_url=self.official_website_url,
            store_locator_url=self.store_locator_url,
            endpoint_url=self.store_index_url,
            source_type="html",
            provider="www.sprouts.com",
            notes=(
                "Public HTML store index and state pages. "
                "The store index page lists state cards with state-page links and counts; "
                "each state page renders stores as grid cards with store number, name, "
                "address, phone, and opening date when applicable."
            ),
        )

    def fetch_raw_artifacts(self) -> list[AcquisitionArtifact]:
        """Fetch the store index and all discovered state pages."""
        artifacts: list[AcquisitionArtifact] = []

        index_html = self._fetch_html(self.store_index_url)
        index_states = self._extract_state_entries(index_html)

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
                    "http_status": 200,
                    "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                    "state_count": len(index_states),
                },
            )
        )

        for state in index_states:
            state_html = self._fetch_html(state.state_url)
            artifacts.append(
                AcquisitionArtifact(
                    artifact_type="raw_html",
                    source_url=state.state_url,
                    file_path=None,
                    content=state_html,
                    metadata={
                        "retailer_key": self.retailer_key,
                        "retailer_name": self.retailer_name,
                        "page_type": "state_page",
                        "state_code": state.state_code,
                        "state_name": state.state_name,
                        "expected_store_count": state.store_count,
                        "http_status": 200,
                        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                        "location_count": self._count_store_cards(state_html),
                    },
                )
            )

        return artifacts

    def extract_store_payloads(
        self,
        artifacts: Sequence[AcquisitionArtifact],
    ) -> list[dict[str, Any]]:
        """
        Extract and deduplicate stores from state-page artifacts.

        :param artifacts: Raw artifacts collected from the Sprouts store directory.
        :return: Store payloads deduplicated by store identity.
        """
        payloads: list[dict[str, Any]] = []
        seen_keys: set[str] = set()

        for artifact in artifacts:
            page_type = self._clean_text(artifact.metadata.get("page_type"))
            if page_type != "state_page":
                continue

            state_code = self._clean_text(artifact.metadata.get("state_code"))
            state_name = self._clean_text(artifact.metadata.get("state_name"))
            source_page_url = self._clean_text(artifact.source_url)
            if not source_page_url:
                continue

            page_payloads = self._extract_store_payloads_from_state_html(
                html=artifact.content,
                source_page_url=source_page_url,
                state_code=state_code,
                state_name=state_name,
            )

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
        """
        Validate store identifiers and required location fields.

        :param payloads: Store payloads produced by the extraction stage.
        :return: Validation metrics and detected data-quality issues.
        """
        seen_store_ids: set[str] = set()
        duplicate_store_ids: list[str] = []

        missing_store_ids = 0
        missing_store_names = 0
        missing_address_components = 0
        issue_counts: dict[str, int] = {}
        opening_soon_count = 0

        for payload in payloads:
            store_id = self._clean_text(payload.get("retailer_store_id"))
            store_name = self._clean_text(payload.get("store_name"))

            address = self._clean_text(payload.get("address")) or self._clean_text(
                payload.get("address_line1")
            )
            city = self._clean_text(payload.get("city"))
            state = self._clean_text(payload.get("state"))
            zip_code = self._clean_text(payload.get("zip_code"))

            status = self._clean_text(payload.get("status"))

            if store_id is None:
                missing_store_ids += 1
                issue_counts["missing_store_id"] = issue_counts.get("missing_store_id", 0) + 1
            elif store_id in seen_store_ids:
                duplicate_store_ids.append(store_id)
            else:
                seen_store_ids.add(store_id)

            if store_name is None:
                missing_store_names += 1
                issue_counts["missing_store_name"] = issue_counts.get("missing_store_name", 0) + 1

            if not (address and city and state and zip_code):
                missing_address_components += 1
                issue_counts["missing_address"] = issue_counts.get("missing_address", 0) + 1

            if status == "opening_soon":
                opening_soon_count += 1

        total_records = len(payloads)
        unique_store_ids = len(seen_store_ids)

        is_valid = (
            total_records > 0
            and missing_store_ids == 0
            and missing_store_names == 0
            and missing_address_components == 0
            and not duplicate_store_ids
        )

        notes: list[str] = []
        if total_records == 0:
            notes.append("No payloads were collected from the Sprouts source.")
        if duplicate_store_ids:
            notes.append(
                f"Duplicate retailer_store_id values detected: {sorted(set(duplicate_store_ids))[:10]}"
            )
        if opening_soon_count > 0:
            notes.append(f"Opening-soon stores included: {opening_soon_count}")
        if unique_store_ids != total_records:
            notes.append(
                f"Payload count ({total_records}) differs from unique store id count ({unique_store_ids}); deduplication was required."
            )

        notes.append(
            "Coordinates are not exposed by the observed Sprouts state-page HTML and are left null rather than inferred."
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
        """Return notes describing the Sprouts acquisition methodology."""
        return [
            "Discovery source: public Sprouts store index page and state pages.",
            "Acquisition mechanism: HTML parsing with BeautifulSoup.",
            "Normalization key: retailer store number with fallback dedupe on store name and store URL.",
            "Coverage plan: parse all state links from the store index page, then parse each state's grid cards.",
        ]

    def _fetch_html(self, url: str) -> str:
        """
        Fetch an HTML page from the official Sprouts website.

        :param url: Page URL to request.
        :return: Retrieved HTML content.
        :raises requests.HTTPError: If the request returns an unsuccessful status.
        """
        response = self._session.get(url, timeout=self._timeout_seconds)
        response.raise_for_status()
        return response.text

    def _extract_state_entries(self, html: str) -> list[StateEntry]:
        """
        Extract state directory entries from the store index.

        :param html: Sprouts store-index HTML.
        :return: Discovered state directory entries.
        """
        soup = BeautifulSoup(html, "html.parser")
        entries: list[StateEntry] = []

        for anchor in soup.select(STATE_CARD_SELECTOR):
            href = self._clean_text(anchor.get("href"))
            if not href:
                continue

            state_url = urljoin(self.store_index_url, href)

            state_name_tag = anchor.select_one("h3.state-name")
            state_name = (
                self._clean_text(state_name_tag.get_text(" ", strip=True))
                if state_name_tag
                else None
            )
            if not state_name:
                continue

            state_code = self._state_code_from_url(state_url)
            if not state_code:
                continue

            anchor_text = self._clean_text(anchor.get_text(" ", strip=True)) or ""
            store_count = self._parse_count_from_anchor_text(anchor_text)

            entries.append(
                StateEntry(
                    state_code=state_code,
                    state_name=state_name,
                    state_url=state_url,
                    store_count=store_count,
                )
            )

        return entries

    def _extract_store_payloads_from_state_html(
        self,
        *,
        html: str,
        source_page_url: str,
        state_code: str | None,
        state_name: str | None,
    ) -> list[dict[str, Any]]:
        """
        Extract store payloads from one state page.

        :param html: State-page HTML.
        :param source_page_url: URL of the state page.
        :param state_code: State code associated with the page.
        :param state_name: State name associated with the page.
        :return: Parsed store payloads from the page.
        """
        soup = BeautifulSoup(html, "html.parser")
        payloads: list[dict[str, Any]] = []

        for card in soup.select(STORE_CARD_SELECTOR):
            payload = self._parse_store_card(
                card,
                source_page_url=source_page_url,
                state_code=state_code,
                state_name=state_name,
            )
            if payload is not None:
                payloads.append(payload)

        return payloads

    def _parse_store_card(
        self,
        card: Any,
        *,
        source_page_url: str,
        state_code: str | None,
        state_name: str | None,
    ) -> dict[str, Any] | None:
        """
        Parse one official Sprouts store card.

        :param card: BeautifulSoup store-card element.
        :param source_page_url: State page containing the card.
        :param state_code: State code associated with the page.
        :param state_name: State name associated with the page.
        :return: Parsed store payload, or None when the card is unusable.
        """
        store_number_tag = card.select_one("p.store-num")
        store_number_text = (
            self._clean_text(store_number_tag.get_text(" ", strip=True))
            if store_number_tag
            else None
        )
        store_number = self._parse_store_number(store_number_text)

        name_anchor = card.select_one("h4 a[href]")
        if not name_anchor:
            return None

        store_name = self._clean_text(name_anchor.get_text(" ", strip=True))
        store_url = self._clean_text(name_anchor.get("href"))
        if store_url:
            store_url = urljoin(source_page_url, store_url)

        opening_date_tag = card.select_one("p.opening-date")
        opening_date_text = (
            self._clean_text(opening_date_tag.get_text(" ", strip=True))
            if opening_date_tag
            else None
        )
        opening_date = self._parse_opening_date(opening_date_text)

        lines = self._collect_card_lines(card)

        phone = None
        phone_idx = None
        for idx in range(len(lines) - 1, -1, -1):
            if PHONE_RE.search(lines[idx]):
                phone = self._normalize_phone(lines[idx])
                phone_idx = idx
                break

        if phone_idx is not None:
            lines.pop(phone_idx)

        city = None
        state = None
        zip_code = None
        city_idx = None
        for idx in range(len(lines) - 1, -1, -1):
            match = CITY_STATE_ZIP_RE.match(lines[idx])
            if match:
                city = self._clean_text(match.group("city"))
                state = self._clean_text(match.group("state"))
                zip_code = self._clean_text(match.group("zip"))
                city_idx = idx
                break

        address_line1 = None
        address_line2 = None
        note_lines: list[str] = []

        if city_idx is not None:
            before_city = lines[:city_idx]
            if before_city:
                address_line1 = before_city[-1]
                if len(before_city) > 1:
                    address_line2 = " | ".join(before_city[:-1])
                    note_lines.extend(before_city[:-1])
        else:
            if lines:
                address_line1 = lines[0]
                if len(lines) > 1:
                    address_line2 = " | ".join(lines[1:])
                    note_lines.extend(lines[1:])

        if opening_date_text:
            note_lines.append(opening_date_text)

        status = "opening_soon" if opening_date_text else "open"
        store_type = self._infer_store_type(note_lines, store_name)

        full_address_parts: list[str] = []
        if address_line1:
            full_address_parts.append(address_line1)
        if address_line2:
            full_address_parts.append(address_line2)
        if city and state and zip_code:
            full_address_parts.append(f"{city}, {state} {zip_code}")

        full_address = " | ".join(full_address_parts) if full_address_parts else None

        return {
            # Canonical acquisition schema.
            "retailer": self.retailer_name,
            "retailer_key": self.retailer_key,
            "store_name": store_name,
            "retailer_store_id": store_number,
            "store_number": store_number,
            "address": address_line1,
            "city": city,
            "state": state,
            "zip_code": zip_code,
            "full_address": full_address,
            "phone": phone,
            "latitude": None,
            "longitude": None,
            "store_url": store_url,
            "source": "Sprouts official store locator",
            "source_type": "html",
        }

    def _collect_card_lines(self, card: Any) -> list[str]:
        """
        Collect relevant text lines from a store card.

        :param card: BeautifulSoup store-card element.
        :return: Normalized text lines used for address parsing.
        """
        lines: list[str] = []

        for p_tag in card.find_all("p"):
            classes = set(p_tag.get("class", []))
            if "store-num" in classes or "opening-date" in classes:
                continue

            block_text = p_tag.get_text("\n", strip=False)
            if not block_text:
                continue

            for line in block_text.splitlines():
                cleaned = self._clean_text(line)
                if cleaned:
                    lines.append(cleaned)

        return lines

    def _dedupe_key(self, payload: Mapping[str, Any]) -> str:
        """
        Build the identity key used to deduplicate store payloads.

        :param payload: Parsed store payload.
        :return: Store-ID key or normalized fallback identity.
        """
        store_id = self._clean_text(payload.get("retailer_store_id"))
        if store_id:
            return f"store_id:{store_id}"

        store_name = self._normalize_key_piece(payload.get("store_name"))
        store_url = self._normalize_key_piece(payload.get("store_url"))
        city = self._normalize_key_piece(payload.get("city"))
        state = self._normalize_key_piece(payload.get("state"))
        zip_code = self._normalize_key_piece(payload.get("zip_code"))
        return f"fallback:{store_name}|{store_url}|{city}|{state}|{zip_code}"

    def _count_store_cards(self, html: str) -> int:
        """
        Count store cards rendered on a state page.

        :param html: State-page HTML.
        :return: Number of detected store cards.
        """
        soup = BeautifulSoup(html, "html.parser")
        return len(soup.select(STORE_CARD_SELECTOR))

    @staticmethod
    def _state_code_from_url(url: str) -> str | None:
        """
        Extract a two-letter state code from a Sprouts state URL.

        :param url: Sprouts state-page URL.
        :return: Lowercase state code, or None when the URL is invalid.
        """
        path = urlparse(url).path.strip("/")
        parts = [part for part in path.split("/") if part]
        if len(parts) < 2 or parts[0] != "stores":
            return None
        state_code = parts[1].lower()
        if re.fullmatch(r"[a-z]{2}", state_code):
            return state_code
        return None

    @staticmethod
    def _parse_count_from_anchor_text(text: str) -> int | None:
        """
        Parse the store count displayed on a state directory card.

        :param text: State-card text.
        :return: Parsed store count, or None when unavailable.
        """
        match = re.search(r"\((\d+)\)\s*$", text)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    @staticmethod
    def _parse_store_number(value: str | None) -> str | None:
        """
        Extract the numeric store identifier from store-number text.

        :param value: Raw store-number text.
        :return: Parsed store number, or None when unavailable.
        """
        if not value:
            return None
        match = STORE_NUMBER_RE.search(value)
        if not match:
            return None
        return match.group(1)

    @staticmethod
    def _parse_opening_date(value: str | None) -> str | None:
        """
        Parse an opening-date label into an ISO date when possible.

        :param value: Raw opening-date text.
        :return: ISO date or the original date text when parsing is unavailable.
        """
        if not value:
            return None
        match = OPENING_DATE_RE.search(value)
        if not match:
            return None

        raw_date = match.group("date").strip()
        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                parsed = datetime.strptime(raw_date, fmt)
                return parsed.date().isoformat()
            except ValueError:
                continue

        return raw_date

    @staticmethod
    def _normalize_phone(value: Any) -> str | None:
        """
        Normalize a phone value using the observed US phone pattern.

        :param value: Raw phone value.
        :return: Extracted phone number or normalized original text.
        """
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        match = PHONE_RE.search(text)
        if match:
            return match.group(0)
        return text

    @staticmethod
    def _clean_text(value: Any) -> str | None:
        """
        Normalize a value to compact trimmed text.

        :param value: Value to normalize.
        :return: Normalized text, or None for empty values.
        """
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
        """
        Normalize a value for use in a fallback identity key.

        :param value: Value to normalize.
        :return: Lowercase alphanumeric identity component.
        """
        text = SproutsAcquisitionStrategy._clean_text(value) or ""
        text = text.lower()
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"[^a-z0-9 ]", "", text)
        return text.strip()

    @staticmethod
    def _infer_store_type(
        note_lines: Sequence[str],
        store_name: str | None,
    ) -> str | None:
        """
        Infer the store type from store notes and name.

        :param note_lines: Supplemental text extracted from the store card.
        :param store_name: Store display name.
        :return: Inferred store-type label.
        """
        note_blob = " ".join(note_lines).lower()
        name_blob = (store_name or "").lower()

        if "independently operated" in note_blob or "independently operated" in name_blob:
            return "independently_operated"
        if "opening" in note_blob:
            return "opening_soon"
        return "standard"

    @staticmethod
    def _looks_like_note(value: str) -> bool:
        """
        Determine whether a text value resembles a store note.

        :param value: Text to inspect.
        :return: True when the value matches a known note pattern.
        """
        lowered = value.lower()
        return (
            lowered.startswith("*")
            or "redirected" in lowered
            or "opening" in lowered
        )

    @staticmethod
    def _coerce_float(value: Any) -> float | None:
        """
        Convert a value to a finite float when possible.

        :param value: Value to convert.
        :return: Parsed float, or None for missing, NaN, or invalid values.
        """
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


__all__ = [
    "SproutsAcquisitionStrategy",
    "StateEntry",
]