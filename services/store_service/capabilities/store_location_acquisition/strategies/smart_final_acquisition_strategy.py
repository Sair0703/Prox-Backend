# services/store_service/capabilities/store_location_acquisition/strategies/smart_final_acquisition_strategy.py

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import re
from typing import Any, Mapping, Sequence

import requests

from services.store_service.capabilities.store_location_acquisition.protocals import \
    StoreLocationAcquisitionStrategy, AcquisitionSourceInfo, AcquisitionArtifact, AcquisitionValidationResult


@dataclass(frozen=True, slots=True)
class SmartFinalProbe:
    latitude: float
    longitude: float
    within_kilometers: float = 5000.0
    limit: int = 1000

    @property
    def url(self) -> str:
        """Handle url.

        :return: Result produced by url.
        """
        return (
            "https://storefrontgateway.smartandfinal.com/api/near/"
            f"{self.latitude}/{self.longitude}/{self.within_kilometers}/{self.limit}/stores"
            f"?latitude={self.latitude}&longitude={self.longitude}"
            f"&withinKilometers={self.within_kilometers}"
        )


class SmartFinalAcquisitionStrategy(StoreLocationAcquisitionStrategy):
    retailer_key = "smart_final"
    retailer_name = "Smart & Final"

    official_website_url = "https://www.smartandfinal.com/"
    store_locator_url = "https://www.smartandfinal.com/store"

    default_probes: tuple[SmartFinalProbe, ...] = (
        SmartFinalProbe(latitude=39.8283, longitude=-98.5795, within_kilometers=5000.0),
        SmartFinalProbe(latitude=34.0522, longitude=-118.2437, within_kilometers=5000.0),
        SmartFinalProbe(latitude=33.4484, longitude=-112.0740, within_kilometers=5000.0),
    )

    def __init__(
        self,
        *,
        probes: Sequence[SmartFinalProbe] | None = None,
        timeout_seconds: float = 30.0,
        user_agent: str | None = None,
    ) -> None:
        """Initialize acquisition configuration and run state.

        :param probes: Geographic probes used for acquisition.
        :param timeout_seconds: HTTP request timeout in seconds.
        :param user_agent: Optional user-agent header for retailer requests.
        :return: Result produced by init  .
        """
        self._probes = tuple(probes) if probes is not None else self.default_probes
        self._timeout_seconds = timeout_seconds
        self._session = requests.Session()
        self._session.headers.update(
            {
                "accept": "application/json",
                "accept-language": "en-US,en;q=0.9",
                "origin": self.official_website_url.rstrip("/"),
                "referer": self.store_locator_url,
                "user-agent": user_agent
                or (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/151.0.0.0 Safari/537.36"
                ),
            }
        )

    def discover_source(self) -> AcquisitionSourceInfo:
        """Return metadata describing the retailer's official acquisition source.

        :return: Metadata describing the acquisition source.
        """
        return AcquisitionSourceInfo(
            retailer_key=self.retailer_key,
            retailer_name=self.retailer_name,
            official_website_url=self.official_website_url,
            store_locator_url=self.store_locator_url,
            endpoint_url=self._probes[0].url if self._probes else None,
            source_type="rest_api",
            provider="storefrontgateway.smartandfinal.com",
            notes=(
                "Nearby-store REST endpoint discovered via browser network traffic; "
                "returns JSON items with store identity, address, coordinates, "
                "status, hours, phone, and shopping modes."
            ),
        )

    def fetch_raw_artifacts(self) -> list[AcquisitionArtifact]:
        """Fetch raw artifacts required for store location acquisition.

        :return: Raw acquisition artifacts.
        """
        artifacts: list[AcquisitionArtifact] = []

        for probe in self._probes:
            response = self._session.get(probe.url, timeout=self._timeout_seconds)
            response.raise_for_status()
            content: dict[str, Any] = response.json()

            items = content.get("items", [])
            artifacts.append(
                AcquisitionArtifact(
                    artifact_type="raw_response",
                    source_url=probe.url,
                    file_path=None,
                    content=content,
                    metadata={
                        "retailer_key": self.retailer_key,
                        "retailer_name": self.retailer_name,
                        "probe": {
                            "latitude": probe.latitude,
                            "longitude": probe.longitude,
                            "within_kilometers": probe.within_kilometers,
                            "limit": probe.limit,
                        },
                        "http_status": response.status_code,
                        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                        "item_count": len(items) if isinstance(items, list) else None,
                    },
                )
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
        payloads: list[dict[str, Any]] = []
        seen_keys: set[str] = set()

        for artifact in artifacts:
            raw_items = self._extract_items_from_artifact(artifact)
            for item in raw_items:
                payload = self._build_store_payload(item, source_url=artifact.source_url)
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
        """Validate acquired store payloads for completeness and uniqueness.

        :param payloads: Normalized store payloads to validate.
        :return: Validation result for the acquired payloads.
        """
        duplicate_store_ids: list[str] = []
        issue_counts: dict[str, int] = {}
        seen_store_ids: set[str] = set()
        seen_dedupe_keys: set[str] = set()

        missing_store_ids = 0
        missing_coordinates = 0
        non_us_records = 0

        for payload in payloads:
            store_id = self._clean_text(payload.get("retailer_store_id"))
            country = self._clean_text(payload.get("country"))
            latitude = payload.get("latitude")
            longitude = payload.get("longitude")

            if store_id is None:
                missing_store_ids += 1
                issue_counts["missing_store_id"] = issue_counts.get("missing_store_id", 0) + 1
            elif store_id in seen_store_ids:
                duplicate_store_ids.append(store_id)
            else:
                seen_store_ids.add(store_id)

            if latitude is None or longitude is None:
                missing_coordinates += 1
                issue_counts["missing_coordinates"] = issue_counts.get("missing_coordinates", 0) + 1

            if country is not None and country.lower() != "united states":
                non_us_records += 1
                issue_counts["non_us_coordinates"] = issue_counts.get("non_us_coordinates", 0) + 1

            dedupe_key = self._dedupe_key(payload)
            if dedupe_key in seen_dedupe_keys:
                issue_counts["duplicate_store"] = issue_counts.get("duplicate_store", 0) + 1
            else:
                seen_dedupe_keys.add(dedupe_key)

        total_records = len(payloads)
        unique_store_ids = len(seen_store_ids)

        is_valid = (
            missing_store_ids == 0
            and missing_coordinates == 0
            and non_us_records == 0
        )

        notes: list[str] = []
        if duplicate_store_ids:
            notes.append(
                f"Duplicate retailer_store_id values detected: {sorted(set(duplicate_store_ids))[:10]}"
            )
        if total_records == 0:
            notes.append("No payloads were collected from the Smart & Final source.")
        if unique_store_ids != total_records:
            notes.append(
                f"Payload count ({total_records}) differs from unique store id count ({unique_store_ids}); deduplication was required."
            )

        return AcquisitionValidationResult(
            is_valid=is_valid,
            total_records=total_records,
            unique_store_ids=unique_store_ids,
            missing_store_ids=missing_store_ids,
            missing_coordinates=missing_coordinates,
            non_us_records=non_us_records,
            duplicate_store_ids=sorted(set(duplicate_store_ids)),
            issue_counts=issue_counts,
            notes=notes,
        )

    def build_run_notes(self) -> list[str]:
        """Return acquisition source and execution details for the run summary.

        :return: Notes describing the acquisition run.
        """
        return [
            "Discovery source: Smart & Final official store locator.",
            "Acquisition mechanism: nearby-store REST API on storefrontgateway.smartandfinal.com.",
            "Normalization key: retailerStoreId with fallback dedupe on name/address/city/state/zip.",
            "Coverage plan: multiple large-radius geospatial probes with cross-probe deduplication.",
        ]

    def _extract_items_from_artifact(self, artifact: AcquisitionArtifact) -> list[dict[str, Any]]:
        """Extract items from artifact.

        :param artifact: Acquisition artifact to parse.
        :return: Result produced by extract items from artifact.
        """
        if isinstance(artifact.content, dict):
            items = artifact.content.get("items", [])
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
            return []

        if artifact.file_path is not None and artifact.file_path.exists():
            parsed = json.loads(artifact.file_path.read_text(encoding="utf-8"))
            items = parsed.get("items", []) if isinstance(parsed, dict) else []
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
            return []

        return []

    def _build_store_payload(
        self,
        item: Mapping[str, Any],
        *,
        source_url: str,
    ) -> dict[str, Any]:
        """Build store payload.

        :param item: Raw retailer store object.
        :param source_url: Source URL associated with the page or record.
        :return: Result produced by build store payload.
        """
        location = item.get("location")
        if not isinstance(location, Mapping):
            location = {}

        retailer_store_id = self._clean_text(item.get("retailerStoreId"))
        address_line1 = self._clean_text(item.get("addressLine1"))
        address_line2 = self._clean_text(item.get("addressLine2"))
        address_line3 = self._clean_text(item.get("addressLine3"))
        city = self._clean_text(item.get("city"))
        state = self._clean_text(item.get("countyProvinceState"))
        zip_code = self._clean_text(item.get("postCode"))
        country = self._clean_text(item.get("country"))
        name = self._clean_text(item.get("name"))

        full_address = self._join_address_parts(
            address_line1,
            address_line2,
            address_line3,
            city,
            state,
            zip_code,
        )

        return {
            "retailer": self.retailer_name,
            "retailer_key": self.retailer_key,
            "retailer_store_id": retailer_store_id,
            "source_url": source_url,
            "source_type": "rest_api",
            "provider": "storefrontgateway.smartandfinal.com",
            "status": self._clean_text(item.get("status")),
            "store_type": self._clean_text(item.get("type")),
            "store_name": name,
            "store_url": None,
            "phone": self._clean_text(item.get("phone")),
            "address": address_line1,
            "address_line1": address_line1,
            "address_line2": address_line2,
            "address_line3": address_line3,
            "city": city,
            "state": state,
            "zip_code": zip_code,
            "country": country,
            "full_address": full_address,
            "opening_hours": self._clean_text(item.get("openingHours")),
            "shopping_modes": item.get("shoppingModes") if isinstance(item.get("shoppingModes"), list) else [],
            "time_zone": self._clean_text(item.get("timeZone")),
            "currency": self._clean_text(item.get("currency")),
            "site_id": self._clean_text(item.get("siteId")),
            "raw_store_id": self._clean_text(item.get("id")),
            "latitude": self._coerce_float(location.get("latitude")),
            "longitude": self._coerce_float(location.get("longitude")),
            "raw": dict(item),
            "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
        }

    def _dedupe_key(self, payload: Mapping[str, Any]) -> str:
        """Deduplicate key.

        :param payload: Store payload to process.
        :return: Result produced by dedupe key.
        """
        store_id = self._clean_text(payload.get("retailer_store_id"))
        if store_id:
            return f"store_id:{store_id}"

        name = self._normalize_key_piece(payload.get("store_name"))
        address = self._normalize_key_piece(payload.get("address"))
        city = self._normalize_key_piece(payload.get("city"))
        state = self._normalize_key_piece(payload.get("state"))
        zip_code = self._normalize_key_piece(payload.get("zip_code"))
        return f"fallback:{name}|{address}|{city}|{state}|{zip_code}"

    @staticmethod
    def _clean_text(value: Any) -> str | None:
        """Normalize text.

        :param value: Value to normalize or convert.
        :return: Result produced by clean text.
        """
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip()
            return text or None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _coerce_float(value: Any) -> float | None:
        """Handle coerce float.

        :param value: Value to normalize or convert.
        :return: Result produced by coerce float.
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

    @staticmethod
    def _normalize_key_piece(value: Any) -> str:
        """Normalize key piece.

        :param value: Value to normalize or convert.
        :return: Result produced by normalize key piece.
        """
        text = SmartFinalAcquisitionStrategy._clean_text(value) or ""
        text = text.lower()
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"[^a-z0-9 ]", "", text)
        return text.strip()

    @staticmethod
    def _join_address_parts(*parts: str | None) -> str | None:
        """Handle join address parts.

        :return: Result produced by join address parts.
        """
        values = [part for part in parts if part]
        if not values:
            return None
        return ", ".join(values)


__all__ = ["SmartFinalAcquisitionStrategy", "SmartFinalProbe"]