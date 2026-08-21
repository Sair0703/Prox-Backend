from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Mapping, Sequence

import requests

from services.store_service.capabilities.store_location_acquisition.protocals import (
    AcquisitionArtifact,
    AcquisitionSourceInfo,
    AcquisitionValidationResult,
    StoreLocationAcquisitionStrategy,
)

LOCATOR_ID = "LETA2YVm6txbe0b9lS297XdxDX4qVQ"
API_URL = (
    f"https://locator.uberall.com/api/locators/{LOCATOR_ID}/locations/all"
)

FIELD_MASKS = (
    "id",
    "identifier",
    "googlePlaceId",
    "lat",
    "lng",
    "name",
    "country",
    "city",
    "province",
    "streetAndNumber",
    "zip",
    "businessId",
    "addressExtra",
)

US_STATE_TO_ABBR = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT",
    "Delaware": "DE", "District of Columbia": "DC", "Florida": "FL",
    "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL",
    "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY",
    "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT",
    "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH",
    "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH",
    "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
    "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT",
    "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY",
}

VALID_STATE_ABBRS = set(US_STATE_TO_ABBR.values())


class AldiAcquisitionStrategy(StoreLocationAcquisitionStrategy):
    """Represent AldiAcquisitionStrategy data used by the acquisition strategy."""
    retailer_key = "aldi"
    retailer_name = "Aldi"

    def __init__(
        self,
        *,
        request_timeout: int = 60,
        max_retries: int = 3,
    ) -> None:
        """Initialize acquisition configuration and run state."""
        self.request_timeout = request_timeout
        self.max_retries = max_retries

        self._raw_location_count = 0
        self._us_location_count = 0
        self._non_us_location_count = 0
        self._business_ids: set[str] = set()

    def discover_source(self) -> AcquisitionSourceInfo:
        """Return metadata describing the retailer's official acquisition source."""
        return AcquisitionSourceInfo(
            retailer_key=self.retailer_key,
            retailer_name=self.retailer_name,
            official_website_url="https://www.aldi.us/",
            store_locator_url="https://info.aldi.us/stores",
            endpoint_url=API_URL,
            source_type="api",
            provider="Uberall",
            notes=(
                "ALDI's official Store Finder loads its location dataset from "
                "Uberall's /locations/all endpoint. The retailer-specific store "
                "identifier is the API 'identifier' field (examples: FG14, FN36, "
                "L545, F002), while 'id' is Uberall's internal location ID."
            ),
        )

    def fetch_raw_artifacts(self) -> list[AcquisitionArtifact]:
        """Fetch raw artifacts required for store location acquisition."""
        self._reset_run_state()

        session = requests.Session()
        session.headers.update(
            {
                "accept": "*/*",
                "accept-language": "en-US,en;q=0.9",
                "origin": "https://info.aldi.us",
                "referer": "https://info.aldi.us/stores",
                "user-agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/151.0.0.0 Safari/537.36"
                ),
            }
        )

        params: list[tuple[str, str]] = [
            ("v", "20260101"),
            ("language", "en"),
        ]
        params.extend(("fieldMask", field) for field in FIELD_MASKS)

        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = session.get(
                    API_URL,
                    params=params,
                    timeout=self.request_timeout,
                )
                response.raise_for_status()

                payload = response.json()

                if payload.get("status") != "SUCCESS":
                    raise RuntimeError(
                        f"Uberall returned non-success status: "
                        f"{payload.get('status')!r}"
                    )

                locations = (
                    payload.get("response", {}).get("locations")
                    if isinstance(payload.get("response"), dict)
                    else None
                )

                if not isinstance(locations, list) or not locations:
                    raise RuntimeError(
                        "Uberall ALDI response contains no locations."
                    )

                self._raw_location_count = len(locations)

                return [
                    AcquisitionArtifact(
                        artifact_type="json",
                        source_url=response.url,
                        content=payload,
                        metadata={
                            "retrieved_at_utc": self._utc_now(),
                            "page_type": "locations_all",
                            "http_status": response.status_code,
                            "scrape_status": "success",
                            "location_count": len(locations),
                            "provider": "Uberall",
                        },
                    )
                ]

            except Exception as exc:
                last_error = exc
                if attempt == self.max_retries:
                    break

        raise RuntimeError(
            f"Failed to fetch ALDI Uberall locations API after "
            f"{self.max_retries} attempts: {last_error}"
        ) from last_error

    def extract_store_payloads(
        self,
        artifacts: Sequence[AcquisitionArtifact],
    ) -> list[dict[str, Any]]:
        """Extract normalized store payloads from acquired artifacts."""
        rows_by_identifier: dict[str, dict[str, Any]] = {}

        for artifact in artifacts:
            if artifact.metadata.get("scrape_status") != "success":
                continue
            if not isinstance(artifact.content, dict):
                continue

            response = artifact.content.get("response")
            if not isinstance(response, dict):
                continue

            locations = response.get("locations")
            if not isinstance(locations, list):
                continue

            for location in locations:
                if not isinstance(location, dict):
                    continue

                country = self._clean_text(location.get("country"))
                if country and country.upper() != "US":
                    self._non_us_location_count += 1
                    continue

                self._us_location_count += 1

                identifier = self._clean_text(location.get("identifier"))
                if not identifier:
                    # Keep malformed records so validation can report them.
                    identifier = f"__missing_identifier_{location.get('id')}"

                business_id = self._clean_text(location.get("businessId"))
                if business_id:
                    self._business_ids.add(business_id)

                city = self._clean_text(location.get("city"))
                state = self._normalize_state(location.get("province"))
                zip_code = self._clean_text(location.get("zip"))
                street = self._clean_text(location.get("streetAndNumber"))
                address_extra = self._clean_text(location.get("addressExtra"))

                # streetAndNumber frequently already includes suite/unit text.
                # Only append addressExtra if it is not already represented.
                street_address = self._merge_address_extra(
                    street,
                    address_extra,
                )

                full_address = self._build_full_address(
                    street_address=street_address,
                    city=city,
                    state=state,
                    zip_code=zip_code,
                )

                city_slug = self._slugify(city)

                row = {
                    "retailer": self.retailer_name,
                    "retailer_store_id": (
                        None
                        if identifier.startswith("__missing_identifier_")
                        else identifier
                    ),
                    "store_number": (
                        None
                        if identifier.startswith("__missing_identifier_")
                        else identifier
                    ),
                    "store_type": "Regular",
                    "store_name": self._clean_text(location.get("name")),
                    "address": street_address,
                    "street_address": street_address,
                    "city": city,
                    "state": state,
                    "address_city": city,
                    "address_state": state,
                    "zip_code": zip_code,
                    "full_address": full_address,
                    "phone": None,
                    "store_url": None,
                    "source_url": artifact.source_url,
                    "source_sitemap": None,
                    "city_slug": city_slug,
                    "latitude": self._as_float(location.get("lat")),
                    "longitude": self._as_float(location.get("lng")),
                    "google_place_id": self._clean_text(
                        location.get("googlePlaceId")
                    ),
                    "uberall_location_id": self._clean_text(
                        location.get("id")
                    ),
                    "uberall_business_id": business_id,
                    "address_extra": address_extra,
                    "country": country or "US",
                    "extraction_source": (
                        "ALDI official Store Finder / Uberall locations API"
                    ),
                    "scrape_status": "success",
                    "http_status": artifact.metadata.get("http_status"),
                    "error_message": None,
                    "scraped_at_utc": artifact.metadata.get(
                        "retrieved_at_utc"
                    ),
                }

                rows_by_identifier[identifier] = row

        return list(rows_by_identifier.values())

    def validate_store_payloads(
        self,
        payloads: Sequence[Mapping[str, Any]],
    ) -> AcquisitionValidationResult:
        """Validate acquired store payloads for completeness and uniqueness."""
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

        missing_coordinates = sum(
            1
            for row in payloads
            if row.get("latitude") is None
            or row.get("longitude") is None
        )

        missing_addresses = sum(
            1
            for row in payloads
            if not self._clean_text(row.get("street_address"))
            or not self._clean_text(row.get("city"))
            or not self._clean_text(row.get("state"))
            or not self._clean_text(row.get("zip_code"))
        )

        invalid_states = sum(
            1
            for row in payloads
            if (
                self._clean_text(row.get("state"))
                and self._clean_text(row.get("state"))
                not in VALID_STATE_ABBRS
            )
        )

        invalid_coordinates = sum(
            1
            for row in payloads
            if not self._valid_coordinates(
                row.get("latitude"),
                row.get("longitude"),
            )
        )

        missing_google_place_ids = sum(
            1
            for row in payloads
            if not self._clean_text(row.get("google_place_id"))
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
        if missing_coordinates:
            issue_counts["missing_coordinates"] = missing_coordinates
        if invalid_coordinates:
            issue_counts["invalid_coordinates"] = invalid_coordinates
        if invalid_states:
            issue_counts["invalid_states"] = invalid_states
        if missing_google_place_ids:
            issue_counts["missing_google_place_ids"] = (
                missing_google_place_ids
            )
        if self._non_us_location_count:
            issue_counts["non_us_locations_filtered"] = (
                self._non_us_location_count
            )

        if (
            self._raw_location_count
            and total_records + self._non_us_location_count
            != self._raw_location_count
        ):
            issue_counts["raw_count_mismatch"] = 1

        notes = [
            f"Uberall raw locations: {self._raw_location_count}",
            f"US locations retained: {self._us_location_count}",
            f"Non-US locations filtered: {self._non_us_location_count}",
            (
                "retailer_store_id uses Uberall 'identifier', which is ALDI's "
                "retailer-specific identifier (examples observed: FG14, FN36, "
                "L545, F002)."
            ),
            (
                "Uberall 'id' is retained internally as uberall_location_id "
                "but is not used as store_number."
            ),
            (
                f"Observed Uberall business IDs: "
                f"{', '.join(sorted(self._business_ids)) or 'none'}"
            ),
            (
                "State/province values are normalized to USPS abbreviations; "
                "the API mixes full names and abbreviations."
            ),
            (
                "Phone and canonical detail URL are not included in the selected "
                "Uberall /locations/all field masks and remain empty."
            ),
        ]

        is_valid = (
            total_records > 0
            and missing_store_ids == 0
            and not duplicate_store_ids
            and missing_addresses == 0
            and missing_coordinates == 0
            and invalid_coordinates == 0
            and invalid_states == 0
            and (
                not self._raw_location_count
                or total_records + self._non_us_location_count
                == self._raw_location_count
            )
        )

        return AcquisitionValidationResult(
            is_valid=is_valid,
            total_records=total_records,
            unique_store_ids=unique_store_ids,
            missing_store_ids=missing_store_ids,
            missing_coordinates=missing_coordinates,
            non_us_records=self._non_us_location_count,
            duplicate_store_ids=duplicate_store_ids,
            issue_counts=issue_counts,
            notes=notes,
        )

    def build_run_notes(self) -> list[str]:
        """Return acquisition source and execution details for the run summary."""
        return [
            "Source: ALDI official Store Finder",
            f"Provider endpoint: {API_URL}",
            "Method: single Uberall /locations/all JSON request",
            "No Playwright, state-page crawl, city-page crawl, or detail-page crawl required.",
            "Dedup key: API identifier field.",
            "Uberall internal id is not used as retailer store ID.",
            "Coordinates are provided directly by the official Store Finder dataset.",
        ]

    def _reset_run_state(self) -> None:
        """Reset run state."""
        self._raw_location_count = 0
        self._us_location_count = 0
        self._non_us_location_count = 0
        self._business_ids = set()

    @staticmethod
    def _normalize_state(value: Any) -> str | None:
        """Normalize state."""
        text = AldiAcquisitionStrategy._clean_text(value)
        if not text:
            return None

        upper = text.upper()
        if upper in VALID_STATE_ABBRS:
            return upper

        return US_STATE_TO_ABBR.get(text)

    @staticmethod
    def _merge_address_extra(
        street: str | None,
        address_extra: str | None,
    ) -> str | None:
        """Merge address extra."""
        if not street:
            return address_extra
        if not address_extra:
            return street

        normalized_street = re.sub(r"[^a-z0-9]", "", street.lower())
        normalized_extra = re.sub(
            r"[^a-z0-9]",
            "",
            address_extra.lower(),
        )

        if normalized_extra and normalized_extra in normalized_street:
            return street

        return f"{street}, {address_extra}"

    @staticmethod
    def _build_full_address(
        *,
        street_address: str | None,
        city: str | None,
        state: str | None,
        zip_code: str | None,
    ) -> str | None:
        """Build full address."""
        locality = None

        if city and state:
            locality = f"{city}, {state}"
        elif city:
            locality = city
        elif state:
            locality = state

        if locality and zip_code:
            locality = f"{locality} {zip_code}"
        elif zip_code:
            locality = zip_code

        parts = [
            part
            for part in (street_address, locality)
            if part
        ]
        return ", ".join(parts) if parts else None

    @staticmethod
    def _slugify(value: str | None) -> str | None:
        """Handle slugify."""
        if not value:
            return None

        slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
        return slug or None

    @staticmethod
    def _as_float(value: Any) -> float | None:
        """Handle as float."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _valid_coordinates(
        latitude: Any,
        longitude: Any,
    ) -> bool:
        """Handle valid coordinates."""
        try:
            lat = float(latitude)
            lng = float(longitude)
        except (TypeError, ValueError):
            return False

        return -90 <= lat <= 90 and -180 <= lng <= 180

    @staticmethod
    def _clean_text(value: Any) -> str | None:
        """Normalize text."""
        if value is None:
            return None

        text = str(value).strip()
        return text or None

    @staticmethod
    def _utc_now() -> str:
        """Return the current UTC timestamp in ISO 8601 format."""
        return datetime.now(timezone.utc).isoformat()


__all__ = ["AldiAcquisitionStrategy"]