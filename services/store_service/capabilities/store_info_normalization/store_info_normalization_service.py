# services/store_service/capabilities/store_info_normalization/store_info_normalization_service.py

"""Normalizes retailer and store metadata into a canonical store representation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Any, Iterable, Mapping

from services.store_service.capabilities.store_info_normalization.constants import DEFAULT_RETAILER_NORMALIZATION_MAP, \
    RETAILER_KEY_MAP
from services.store_service.models.base import DetectedIssue, StoreLocationRecord
from services.store_service.models.store_location_issues import ISSUE_TYPES


def _clean(value: Any) -> str | None:
    """Convert a value to trimmed text, returning None when empty."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value if value else None
    text = str(value).strip()
    return text if text else None


def _collapse_whitespace(value: str) -> str:
    """Collapse repeated whitespace into single spaces."""
    return re.sub(r"\s+", " ", value).strip()


def _normalize_title_case(value: str) -> str:
    """Normalize text casing while preserving directional abbreviations."""
    value = _collapse_whitespace(value)
    value = value.replace("-", " ")
    value = value.lower().title()

    directional_replacements = {
        r"\bNw\b": "NW",
        r"\bNe\b": "NE",
        r"\bSw\b": "SW",
        r"\bSe\b": "SE",
        r"\bN\b": "N",
        r"\bS\b": "S",
        r"\bE\b": "E",
        r"\bW\b": "W",
    }
    for pattern, replacement in directional_replacements.items():
        value = re.sub(pattern, replacement, value)

    return _collapse_whitespace(value)


def _normalize_city(value: Any) -> str | None:
    """Normalize a city name."""
    text = _clean(value)
    if text is None:
        return None
    return _normalize_title_case(text)


def _normalize_city_slug(value: Any) -> str | None:
    """Convert a city slug into a normalized city name."""
    text = _clean(value)
    if text is None:
        return None
    return _normalize_title_case(text.replace("-", " "))


def _normalize_state(value: Any) -> str | None:
    """Normalize a state value to uppercase."""
    text = _clean(value)
    if text is None:
        return None
    return text.upper()


def _normalize_zip(value: Any) -> str | None:
    """Normalize a ZIP code to its five-digit form when available."""
    text = _clean(value)
    if text is None:
        return None

    digits = re.sub(r"\D", "", text)
    if len(digits) >= 5:
        return digits[:5]

    return digits or text


def _normalize_store_type(value: Any) -> str | None:
    """Normalize a retailer store type."""
    text = _clean(value)
    if text is None:
        return None
    return _normalize_title_case(text)


def _normalize_retailer_token(value: Any) -> str | None:
    """Build a normalized token used for retailer canonicalization."""
    text = _clean(value)
    if text is None:
        return None

    text = text.lower()
    text = text.replace("&", " and ")
    text = text.replace("'", "")
    text = text.replace("-", " ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return _collapse_whitespace(text)


def _build_trigger_rkey(value: Any) -> str | None:
    """Build the retailer key format used by the existing normalization output."""
    text = _clean(value)
    if text is None:
        return None
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _build_fallback_retailer_key(raw: Any) -> str | None:
    """Build the legacy fallback retailer key from an unmapped retailer name."""
    text = _clean(raw)
    if text is None:
        return None

    text = re.sub(r"#\s*\d+", "", text.lower())

    for suffix in (
        " store",
        " supermarket",
        " super market",
        " market",
        " grocery",
        " foods",
        " food",
        " pharmacy",
        " drug",
    ):
        text = text.replace(suffix, "")

    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text or None


def _normalize_address_line(value: Any) -> str | None:
    """Normalize an address line."""
    text = _clean(value)
    if text is None:
        return None

    text = _collapse_whitespace(text)
    text = _normalize_title_case(text)
    return text


def _parse_full_address(
    full_address: str | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """
    Parse common full-address components conservatively.

    :param full_address: Full address string to parse.
    :return: Street, city, state, and ZIP components when identifiable.
    """
    text = _clean(full_address)
    if text is None:
        return None, None, None, None

    parts = [part.strip() for part in text.split(",") if part.strip()]

    street: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None

    if len(parts) >= 1:
        street = _normalize_address_line(parts[0])

    if len(parts) >= 3:
        city = _normalize_city(parts[-2])

    state_zip_part = parts[-1] if len(parts) >= 2 else None
    if state_zip_part:
        state_zip_part = _collapse_whitespace(state_zip_part)

        match = re.search(
            r"\b([A-Za-z]{2})\s+(\d{5})(?:-\d{4})?\b",
            state_zip_part,
        )
        if match:
            state = match.group(1).upper()
            zip_code = match.group(2)
        else:
            state_match = re.search(r"\b([A-Za-z]{2})\b", state_zip_part)
            if state_match:
                state = state_match.group(1).upper()

            zip_match = re.search(r"\b(\d{5})(?:-\d{4})?\b", state_zip_part)
            if zip_match:
                zip_code = zip_match.group(1)

    return street, city, state, zip_code


def _compose_full_address(
    street_address: str | None,
    city: str | None,
    state: str | None,
    zip_code: str | None,
) -> str | None:
    """Compose normalized address components into a full address."""
    if street_address is None and city is None and state is None and zip_code is None:
        return None

    parts: list[str] = []

    if street_address:
        parts.append(street_address)

    location_bits: list[str] = []
    if city:
        location_bits.append(city)
    if state:
        location_bits.append(state)

    location = ", ".join(location_bits)
    if zip_code:
        location = f"{location} {zip_code}".strip()

    if location:
        parts.append(location)

    return ", ".join(parts) if parts else None


def _build_store_name(
    retailer: str | None,
    store_type: str | None,
    store_number: str | None,
) -> str | None:
    """Build a canonical store name from retailer metadata."""
    if retailer is None or store_number is None:
        return None

    if store_type:
        return f"{retailer} {store_type.title()} #{store_number}"

    return f"{retailer} #{store_number}"


def _build_issue(name: str, description: str) -> DetectedIssue:
    """Build a detected issue using the registered issue definition when available."""
    issue_type = ISSUE_TYPES.get(name)
    if issue_type is not None:
        return DetectedIssue(name=issue_type.name, description=issue_type.description)
    return DetectedIssue(name=name, description=description)


def _parse_http_status(value: Any) -> int | None:
    """Normalize an HTTP status value to an integer."""
    if value is None:
        return None

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        return int(value)

    text = _clean(value)
    if text is None:
        return None

    return int(text)


@dataclass(slots=True)
class StoreInfoNormalizationResult:
    """Normalized store metadata with detected issues and normalization notes."""

    raw_retailer: str | None
    raw_store_type: str | None

    retailer: str | None
    retailer_key: str | None
    store_type: str | None
    store_number: str | None

    store_name: str | None

    street_address: str | None
    address_city: str | None
    address_state: str | None
    city_slug: str | None

    city: str | None
    state: str | None
    zip_code: str | None
    address: str | None
    full_address: str | None

    store_url: str | None
    source_sitemap: str | None
    phone: str | None
    extraction_source: str | None
    scrape_status: str | None
    http_status: int | None
    error_message: str | None
    scraped_at_utc: str | None

    issues: list[DetectedIssue] = field(default_factory=list)
    normalization_notes: list[str] = field(default_factory=list)

    @property
    def reason_codes(self) -> list[str]:
        """Return the issue names associated with the normalized result."""
        return [issue.name for issue in self.issues]

    def to_store_locations_payload(
        self,
        *,
        source: str = "retailer",
        latitude: float | None = None,
        longitude: float | None = None,
        geocode_source: str | None = None,
        geocode_confidence: str | None = None,
        geocoded_at: datetime | None = None,
        osm_id: str | None = None,
        show_on_map: bool | None = True,
    ) -> dict[str, Any]:
        """
        Convert the normalized result into a store_locations payload.

        :param source: Source associated with the store record.
        :param latitude: Store latitude when available.
        :param longitude: Store longitude when available.
        :param geocode_source: Source used to obtain coordinates.
        :param geocode_confidence: Confidence assigned to the geocoding result.
        :param geocoded_at: Time at which the store was geocoded.
        :param osm_id: OpenStreetMap identifier when available.
        :param show_on_map: Whether the store should be displayed on the map.
        :return: Payload compatible with the store_locations representation.
        """
        geocoded_at_value = None
        if geocoded_at is not None:
            geocoded_at_value = geocoded_at.astimezone(timezone.utc).isoformat()

        return {
            "retailer": self.retailer,
            "store_id": self.store_number,
            "latitude": latitude,
            "longitude": longitude,
            "address": self.address,
            "zip_code": self.zip_code,
            "full_address": self.full_address,
            "retailer_key": self.retailer_key,
            "geocode_source": geocode_source,
            "geocode_confidence": geocode_confidence,
            "geocoded_at": geocoded_at_value,
            "osm_id": osm_id,
            "source": source,
            "store_name": self.store_name,
            "city": self.city,
            "state": self.state,
            "show_on_map": show_on_map,
        }


class StoreInfoNormalizationService:
    """Canonicalizes retailer and store metadata before downstream processing."""

    def __init__(
        self,
        retailer_normalization_map: Mapping[str, tuple[str, str | None]] | None = None,
    ) -> None:
        """
        Initialize the normalization service.

        :param retailer_normalization_map: Optional retailer canonicalization overrides.
        """
        self._retailer_normalization_map = dict(
            retailer_normalization_map or DEFAULT_RETAILER_NORMALIZATION_MAP
        )

    def normalize_retailer_key(
        self,
        raw_retailer: Any,
    ) -> str | None:
        """
        Map a raw retailer name to the legacy canonical retailer key.

        The lookup intentionally preserves the historical substring-matching
        behavior used by Store Service locators.

        :param raw_retailer: Raw retailer name to map.
        :return: Canonical retailer key, or None when no mapping exists.
        """
        text = _clean(raw_retailer)
        if text is None:
            return None

        normalized = text.lower()
        for pattern, retailer_key in RETAILER_KEY_MAP.items():
            if pattern in normalized:
                return retailer_key

        return None

    def make_retailer_key(
        self,
        raw_retailer: Any,
    ) -> str | None:
        """
        Build the historical fallback retailer key.

        :param raw_retailer: Raw retailer name to convert.
        :return: Fallback retailer key, or None when the input is empty.
        """
        return _build_fallback_retailer_key(raw_retailer)

    @staticmethod
    def normalize_address(
        raw_address: str | None,
    ) -> str | None:
        """
        Normalize an address for deduplication and comparisons.

        :param raw_address: Raw address string.
        :return: Lowercase address with repeated whitespace and suite/unit
            noise removed, or None when the address is empty.
        """
        if not raw_address:
            return None

        value = raw_address.lower().strip()
        value = re.sub(r"\s+", " ", value)
        value = re.sub(r"\bste\.?\s*#?\d+\b", "", value)
        value = re.sub(r"\bunit\s*#?\d+\b", "", value)
        value = re.sub(r"\bsuite\s*#?\d+\b", "", value)
        return value.strip(", ").strip()

    def normalize_retailer(
        self,
        raw_retailer: Any,
        raw_store_type: Any | None = None,
    ) -> tuple[str | None, str | None, str | None, list[str]]:
        """
        Normalize retailer identity and store type.

        :param raw_retailer: Raw retailer value to canonicalize.
        :param raw_store_type: Raw store type associated with the retailer.
        :return: Canonical retailer, retailer key, store type, and normalization notes.
        """
        notes: list[str] = []

        raw_text = _clean(raw_retailer)
        normalized_raw_store_type = _normalize_store_type(raw_store_type)

        if raw_text is None:
            return None, None, normalized_raw_store_type, notes

        retailer_token = _normalize_retailer_token(raw_text)
        canonical_retailer: str | None = None
        inferred_store_type: str | None = None

        if retailer_token is not None:
            canonical = self._retailer_normalization_map.get(retailer_token)
            if canonical is not None:
                canonical_retailer, inferred_store_type = canonical
                notes.append(
                    f"retailer canonicalized: {raw_text} -> {canonical_retailer}"
                )

            elif retailer_token.startswith("walmart "):
                suffix = retailer_token[len("walmart ") :]
                walmart_suffix_map = {
                    "supercenter": "Supercenter",
                    "super center": "Supercenter",
                    "neighborhood market": "Neighborhood Market",
                    "neighborhood": "Neighborhood Market",
                    "business center": "Business Center",
                    "fuel center": "Fuel Center",
                    "discount": "Discount",
                }
                inferred_store_type = walmart_suffix_map.get(suffix)
                if inferred_store_type is not None:
                    canonical_retailer = "Walmart"
                    notes.append(
                        f"retailer canonicalized: {raw_text} -> {canonical_retailer}"
                    )
                    notes.append(
                        f"store_type inferred from retailer variant: {inferred_store_type}"
                    )

        if canonical_retailer is None:
            canonical_retailer = _normalize_title_case(raw_text)
            if canonical_retailer != raw_text:
                notes.append(
                    f"retailer normalized: {raw_text} -> {canonical_retailer}"
                )

        # Keep the existing normalization output unchanged for promotion and other
        # workflows that consume StoreInfoNormalizationResult.retailer_key.
        retailer_key = _build_trigger_rkey(canonical_retailer)

        if inferred_store_type is not None:
            if (
                normalized_raw_store_type is not None
                and normalized_raw_store_type != inferred_store_type
            ):
                notes.append(
                    "store_type overridden by retailer variant: "
                    f"{normalized_raw_store_type} -> {inferred_store_type}"
                )
            store_type = inferred_store_type
        else:
            store_type = normalized_raw_store_type

        return canonical_retailer, retailer_key, store_type, notes

    def normalize(
            self,
            row: Mapping[str, Any] | StoreLocationRecord,
    ) -> StoreInfoNormalizationResult:
        """
        Normalize a raw store-information record.

        :param row: Raw store metadata to normalize.
        :return: Canonicalized store metadata with detected issues and notes.
        """
        if isinstance(row, StoreLocationRecord):
            row = self._store_location_to_mapping(
                row
            )

        raw_retailer = _clean(row.get("retailer"))
        raw_store_type = _clean(row.get("store_type"))

        retailer, retailer_key, store_type, notes = self.normalize_retailer(
            raw_retailer,
            raw_store_type,
        )

        store_number = _clean(row.get("store_number"))
        city_slug = _normalize_city_slug(row.get("city_slug"))

        state_from_row = _normalize_state(row.get("state"))
        street_address = _normalize_address_line(row.get("street_address"))
        address_city = _normalize_city(row.get("address_city"))
        address_state = _normalize_state(row.get("address_state"))
        zip_code = _normalize_zip(row.get("zip_code"))
        full_address = _normalize_address_line(row.get("full_address"))

        if address_city is None and city_slug is not None:
            address_city = city_slug
            notes.append("address_city fallback from city_slug")

        if address_state is None and state_from_row is not None:
            address_state = state_from_row
            notes.append("address_state fallback from state")

        if full_address is not None:
            parsed_street, parsed_city, parsed_state, parsed_zip = _parse_full_address(
                full_address
            )

            if street_address is None and parsed_street is not None:
                street_address = parsed_street
                notes.append("street_address parsed from full_address")

            if address_city is None and parsed_city is not None:
                address_city = parsed_city
                notes.append("city parsed from full_address")

            if address_state is None and parsed_state is not None:
                address_state = parsed_state
                notes.append("state parsed from full_address")

            if zip_code is None and parsed_zip is not None:
                zip_code = parsed_zip
                notes.append("zip_code parsed from full_address")

        canonical_full_address = _compose_full_address(
            street_address=street_address,
            city=address_city,
            state=address_state,
            zip_code=zip_code,
        )

        if canonical_full_address is not None:
            if full_address != canonical_full_address:
                notes.append(
                    f"full_address reconstructed: {canonical_full_address}"
                )
            full_address = canonical_full_address

        address = street_address or full_address
        city = address_city
        state = address_state

        store_name = _build_store_name(retailer, store_type, store_number)

        issues: list[DetectedIssue] = []

        if retailer is None:
            issues.append(
                _build_issue(
                    "missing_retailer",
                    "The retailer is missing.",
                )
            )

        if store_number is None:
            issues.append(
                _build_issue(
                    "missing_store_id",
                    "The retailer-specific store ID is missing.",
                )
            )

        if address is None:
            issues.append(
                _build_issue(
                    "missing_address",
                    "The address is missing.",
                )
            )

        if full_address is None:
            issues.append(
                _build_issue(
                    "missing_full_address",
                    "The full address is missing.",
                )
            )

        if city is None:
            issues.append(
                _build_issue(
                    "missing_city",
                    "The city is missing.",
                )
            )

        if state is None:
            issues.append(
                _build_issue(
                    "missing_state",
                    "The state is missing.",
                )
            )

        if zip_code is None:
            issues.append(
                _build_issue(
                    "missing_zip_code",
                    "The ZIP code is missing.",
                )
            )

        return StoreInfoNormalizationResult(
            raw_retailer=raw_retailer,
            raw_store_type=raw_store_type,
            retailer=retailer,
            retailer_key=retailer_key,
            store_type=store_type,
            store_number=store_number,
            store_name=store_name,
            street_address=street_address,
            address_city=address_city,
            address_state=address_state,
            city_slug=city_slug,
            city=city,
            state=state,
            zip_code=zip_code,
            address=address,
            full_address=full_address,
            store_url=_clean(row.get("store_url")),
            source_sitemap=_clean(row.get("source_sitemap")),
            phone=_clean(row.get("phone")),
            extraction_source=_clean(row.get("extraction_source")),
            scrape_status=_clean(row.get("scrape_status")),
            http_status=_parse_http_status(row.get("http_status")),
            error_message=_clean(row.get("error_message")),
            scraped_at_utc=_clean(row.get("scraped_at_utc")),
            issues=issues,
            normalization_notes=notes,
        )

    def normalize_many(
        self,
        rows: Iterable[Mapping[str, Any]],
    ) -> list[StoreInfoNormalizationResult]:
        """
        Normalize multiple store-information records.

        :param rows: Raw store metadata records to normalize.
        :return: Normalization results in input order.
        """
        return [self.normalize(row) for row in rows]

    @staticmethod
    def _store_location_to_mapping(
        store_location: StoreLocationRecord,
    ) -> dict[str, Any]:
        """
        Convert a StoreLocationRecord into the normalization input schema.

        :param store_location: Store-location record to normalize.
        :return: Mapping compatible with the normalization workflow.
        """
        return {
            "retailer": store_location.retailer,
            "store_number": store_location.store_id,
            "street_address": store_location.address,
            "address_city": store_location.city,
            "address_state": store_location.state,
            "state": store_location.state,
            "zip_code": store_location.zip_code,
            "full_address": store_location.full_address,
        }


__all__ = [
    "DEFAULT_RETAILER_NORMALIZATION_MAP",
    "RETAILER_KEY_MAP",
    "StoreInfoNormalizationResult",
    "StoreInfoNormalizer",
    "StoreInfoNormalizationService",
]


StoreInfoNormalizer = StoreInfoNormalizationService