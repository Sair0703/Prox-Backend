# services/store_service/capabilities/store_location_resolution/locators/external/osm_store_locator.py

from __future__ import annotations

import hashlib
import logging
import math
import re
from typing import Any

from geopy.extra.rate_limiter import RateLimiter
from geopy.geocoders import Nominatim

from services.store_service.capabilities.store_info_normalization.store_info_normalization_service import (
    StoreInfoNormalizationService,
)
from services.store_service.geocoders.geocoder import Geocoder
from services.store_service.models.base import FlyerDeal, StoreCandidate

logger = logging.getLogger(__name__)


class OSMStoreLocator:
    """
    OpenStreetMap / Nominatim-backed store locator.

    Responsibilities:
    - resolve a deal's context coordinates
    - query Nominatim using several retailer+ZIP variants
    - parse results into StoreCandidate objects
    - deduplicate and sort by proximity to the deal context

    The locator does not write to store_locations or flyer_deals.
    """

    LOCATOR_NAME = "osm_store_locator"
    LOCATOR_TYPE = "external"

    def __init__(
        self,
        geocoder: Geocoder,
        store_info_normalizer: StoreInfoNormalizationService | None = None,
        user_agent: str = "prox-shopping-osm-locator",
        timeout: int = 5,
        min_delay_seconds: float = 1.0,
        search_limit: int = 10,
        max_results: int = 10,
        max_search_distance_meters: float = 25000.0,
    ) -> None:
        """
        Initialize the OSM-backed store locator.

        :param geocoder: Shared geocoder used to resolve deal context coordinates.
        :param store_info_normalizer: Shared store-info normalization service.
        :param user_agent: User agent used for Nominatim requests.
        :param timeout: Nominatim request timeout in seconds.
        :param min_delay_seconds: Minimum delay between Nominatim search requests.
        :param search_limit: Maximum results requested for each search query.
        :param max_results: Maximum store candidates returned by the locator.
        :param max_search_distance_meters: Maximum candidate distance from the deal context.
        """
        self.geocoder = geocoder
        self.store_info_normalizer = (
            store_info_normalizer
            or StoreInfoNormalizationService()
        )
        self.search_limit = search_limit
        self.max_results = max_results
        self.max_search_distance_meters = max_search_distance_meters
        self._geolocator = Nominatim(user_agent=user_agent, timeout=timeout)
        self._search = RateLimiter(
            self._geolocator.geocode,
            min_delay_seconds=min_delay_seconds,
        )
        self._zip_cache: dict[str, tuple[float, float]] = {}

    async def find_candidate_stores(
            self,
            deal: FlyerDeal,
    ) -> list[StoreCandidate]:
        """
        Find external OSM store candidates for a flyer deal.

        :param deal: Flyer deal containing retailer and location context.
        :return: Deduplicated store candidates ordered by distance.
        """
        retailer_key = self._resolve_retailer_key(deal)
        zip_code = (deal.zip_code or "").strip()

        if not retailer_key or not zip_code:
            return []

        try:
            context_lat, context_lng = await self._resolve_context_coords(deal)
        except Exception as e:
            logger.warning(
                "[OSM_LOCATOR] context resolution failed retailer_key=%s zip=%s error=%s",
                retailer_key,
                zip_code,
                e,
            )
            return []

        query_plan = self._build_query_plan(retailer_key=retailer_key, zip_code=zip_code)
        viewbox = self._build_viewbox(context_lat, context_lng, self.max_search_distance_meters)

        raw_results: list[dict[str, Any]] = []
        seen_search_keys: set[str] = set()

        for query in query_plan:
            search_key = self._normalize_text(query)
            if search_key in seen_search_keys:
                continue
            seen_search_keys.add(search_key)

            try:
                logger.debug(
                    "[OSM_LOCATOR] search query=%s retailer_key=%s zip=%s",
                    query,
                    retailer_key,
                    zip_code,
                )
                results = self._search(
                    query,
                    exactly_one=False,
                    limit=self.search_limit,
                    addressdetails=True,
                    country_codes="us",
                    viewbox=viewbox,
                    bounded=True,
                )
                if not results:
                    continue
                if not isinstance(results, list):
                    results = [results]

                for item in results:
                    if hasattr(item, "raw"):
                        raw = dict(item.raw or {})
                    elif isinstance(item, dict):
                        raw = item
                    else:
                        continue
                    raw_results.append(raw)
            except Exception as e:
                logger.warning(
                    "[OSM_LOCATOR] search failed query=%s retailer_key=%s zip=%s error=%s",
                    query,
                    retailer_key,
                    zip_code,
                    e,
                )

        candidates: list[StoreCandidate] = []
        for raw in raw_results:
            if not self._retailer_match(retailer_key, raw):
                continue

            candidate = self._to_candidate(
                retailer_key=retailer_key,
                raw=raw,
                context_lat=context_lat,
                context_lng=context_lng,
            )
            if candidate is None:
                continue
            candidates.append(candidate)

        candidates = self._dedupe_candidates(candidates)
        candidates.sort(key=lambda c: (c.distance_meters, c.canonical_store_id))

        if self.max_results > 0:
            candidates = candidates[: self.max_results]

        logger.info(
            "[OSM_LOCATOR] found candidates retailer_key=%s zip=%s count=%d",
            retailer_key,
            zip_code,
            len(candidates),
        )
        return candidates
    async def find_nearest_store(
            self,
            deal: FlyerDeal,
    ) -> StoreCandidate | None:
        """
        Find the nearest external OSM store candidate.

        :param deal: Flyer deal containing retailer and location context.
        :return: Nearest candidate, or None when no candidate is found.
        """
        candidates = await self.find_candidate_stores(deal)
        return candidates[0] if candidates else None

    def _zip_key(self, zip_code: str) -> str:
        """Normalize a ZIP code for locator caching."""
        return (zip_code or "").strip()

    async def _geocode_cached(self, zip_code: str) -> tuple[float, float]:
        """Resolve and cache the coordinates for a ZIP code."""
        key = self._zip_key(zip_code)
        if key in self._zip_cache:
            logger.debug("[OSM_LOCATOR] zip cache hit zip=%s", zip_code)
            return self._zip_cache[key]

        coords = self.geocoder.geocode(f"{zip_code}, US")
        if not coords:
            raise RuntimeError(f"Geocoding failed for ZIP {zip_code}")

        lat = float(coords[0])
        lng = float(coords[1])
        self._zip_cache[key] = (lat, lng)

        logger.debug(
            "[OSM_LOCATOR] geocoded zip=%s lat=%.6f lng=%.6f",
            zip_code,
            lat,
            lng,
        )
        return lat, lng

    async def _resolve_context_coords(self, deal: FlyerDeal) -> tuple[float, float]:
        """
        Resolve the coordinate used for OSM search ranking.

        Prefer deal coordinates when present; otherwise geocode ZIP.
        """
        if deal.store_lat is not None and deal.store_lng is not None:
            return float(deal.store_lat), float(deal.store_lng)

        zip_code = (deal.zip_code or "").strip()
        if not zip_code:
            raise RuntimeError("ZIP code is required when deal coordinates are absent")

        return await self._geocode_cached(zip_code)

    def _build_viewbox(
        self,
        lat: float,
        lng: float,
        radius_meters: float,
    ) -> list[list[float]]:
        """
        Build a Nominatim viewbox around the context coordinate.
        """
        radius_miles = max(1.0, radius_meters / 1609.344)
        lat_delta = radius_miles / 69.0
        cos_lat = max(0.2, math.cos(math.radians(lat)))
        lng_delta = radius_miles / (69.0 * cos_lat)
        south = max(-90.0, lat - lat_delta)
        north = min(90.0, lat + lat_delta)
        west = max(-180.0, lng - lng_delta)
        east = min(180.0, lng + lng_delta)
        return [[south, west], [north, east]]

    def _pretty_retailer_name(self, raw: str) -> str:
        """Convert a retailer key into a readable retailer name."""
        text = (raw or "").strip().replace("_", " ")
        if not text:
            return ""
        return " ".join(part.capitalize() for part in text.split())

    def _normalize_text(self, value: str | None) -> str:
        """Normalize free text for retailer matching and deduplication."""
        if not value:
            return ""
        value = value.lower().replace("&", " and ")
        value = re.sub(r"[^a-z0-9\s]+", " ", value)
        return " ".join(value.split())

    def _retailer_tokens(self, retailer_key: str) -> list[str]:
        """Build normalized retailer tokens used to filter OSM results."""
        text = self._normalize_text(self._pretty_retailer_name(retailer_key))
        tokens = [t for t in text.split() if len(t) > 1]
        if not tokens and text:
            tokens = [text]
        return tokens

    def _build_query_plan(self, retailer_key: str, zip_code: str) -> list[str]:
        """Build and deduplicate the Nominatim query variants for a deal."""
        retailer_name = self._pretty_retailer_name(retailer_key)
        query_plan: list[str] = []

        if retailer_name and zip_code:
            query_plan.append(f"{retailer_name}, {zip_code}, US")
            query_plan.append(f"{retailer_name} {zip_code}")

        if retailer_name:
            query_plan.append(f"{retailer_name}, US")
            query_plan.append(retailer_name)

        if zip_code:
            query_plan.append(f"{zip_code}, US")

        deduped: list[str] = []
        seen: set[str] = set()
        for item in query_plan:
            norm = self._normalize_text(item)
            if norm and norm not in seen:
                seen.add(norm)
                deduped.append(item)
        return deduped

    def _candidate_key(self, raw: dict[str, Any], fallback_label: str) -> str:
        """Build a stable identity key for an external OSM result."""
        osm_id = raw.get("osm_id")
        place_id = raw.get("place_id")
        display_name = raw.get("display_name") or fallback_label
        if osm_id is not None:
            return f"osm:{osm_id}"
        if place_id is not None:
            return f"place:{place_id}"
        normalized = self._normalize_text(str(display_name))
        return f"name:{normalized}"

    def _stable_negative_id(self, key: str) -> int:
        """Build a deterministic negative ID for a non-canonical candidate."""
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        value = int(digest[:12], 16)
        return -max(1, value)

    def _extract_address(self, raw: dict[str, Any]) -> dict[str, str | None]:
        """Extract StoreCandidate address fields from a raw OSM result."""
        address = raw.get("address")
        if not isinstance(address, dict):
            address = {}

        city = (
            address.get("city")
            or address.get("town")
            or address.get("village")
            or address.get("suburb")
            or address.get("hamlet")
        )
        state = address.get("state") or address.get("state_code")
        zip_code = address.get("postcode")
        street_parts = [
            address.get("house_number"),
            address.get("road"),
            address.get("neighbourhood"),
        ]
        street = " ".join(part for part in street_parts if part)
        full_address = raw.get("display_name") or None

        return {
            "address": street or None,
            "full_address": full_address,
            "city": city,
            "state": state,
            "zip_code": zip_code,
        }

    def _retailer_match(self, retailer_key: str, raw: dict[str, Any]) -> bool:
        """Return whether an OSM result contains the expected retailer identity."""
        retailer_tokens = self._retailer_tokens(retailer_key)
        if not retailer_tokens:
            return True

        display_name = self._normalize_text(str(raw.get("display_name") or ""))
        name = self._normalize_text(str(raw.get("name") or ""))
        raw_text = f"{display_name} {name}".strip()
        if not raw_text:
            return False

        return any(token in raw_text for token in retailer_tokens)

    def _distance_meters(
        self,
        lat1: float | None,
        lng1: float | None,
        lat2: float | None,
        lng2: float | None,
    ) -> float | None:
        """Calculate the Haversine distance between two coordinates."""
        if lat1 is None or lng1 is None or lat2 is None or lng2 is None:
            return None
        try:
            r = 6371000.0
            p1 = math.radians(lat1)
            p2 = math.radians(lat2)
            dp = math.radians(lat2 - lat1)
            dl = math.radians(lng2 - lng1)
            a = (
                math.sin(dp / 2.0) ** 2
                + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
            )
            c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
            return r * c
        except Exception:
            return None

    def _as_float(self, value: Any) -> float | None:
        """Convert a value to float when possible."""
        if value is None:
            return None
        try:
            return float(value)
        except Exception:
            return None

    def _to_candidate(
        self,
        retailer_key: str,
        raw: dict[str, Any],
        context_lat: float,
        context_lng: float,
    ) -> StoreCandidate | None:
        """Convert a raw OSM result into an external StoreCandidate."""
        lat = self._as_float(raw.get("lat"))
        lng = self._as_float(raw.get("lon"))
        if lat is None or lng is None:
            return None

        distance_meters = self._distance_meters(context_lat, context_lng, lat, lng)
        if distance_meters is not None and distance_meters > self.max_search_distance_meters:
            return None

        address_fields = self._extract_address(raw)
        key = self._candidate_key(raw, address_fields["full_address"] or "")

        return StoreCandidate(
            canonical_store_id=self._stable_negative_id(key),
            retailer=self._pretty_retailer_name(retailer_key),
            retailer_store_id=str(raw.get("osm_id") or raw.get("place_id") or ""),
            retailer_key=retailer_key,
            store_name=raw.get("name") or raw.get("display_name") or None,
            address=address_fields["address"],
            full_address=address_fields["full_address"],
            city=address_fields["city"],
            state=address_fields["state"],
            zip_code=address_fields["zip_code"],
            latitude=lat,
            longitude=lng,
            geocode_source="osm_search",
            geocode_confidence="osm_search",
            geocoded_at=None,
            osm_id=str(raw.get("osm_id") or raw.get("place_id") or "") or None,
            locator_name=self.LOCATOR_NAME,
            locator_type=self.LOCATOR_TYPE,
            show_on_map=True,
            distance_meters=float(distance_meters or 0.0),
        )

    def _dedupe_candidates(self, candidates: list[StoreCandidate]) -> list[StoreCandidate]:
        """Remove duplicate OSM candidates while preserving result order."""
        deduped: list[StoreCandidate] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = "|".join(
                [
                    str(candidate.osm_id or candidate.retailer_store_id or candidate.canonical_store_id),
                    (candidate.store_name or "").strip().lower(),
                    (candidate.full_address or candidate.address or "").strip().lower(),
                ]
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(candidate)
        return deduped

    def _resolve_retailer_key(self, deal: FlyerDeal) -> str | None:
        """
        Resolve the retailer key for a deal.

        Prefer the persisted retailer_key when available.
        Fall back to the retailer name otherwise.
        """
        retailer_raw = (
                (deal.retailer_key or "").strip()
                or (deal.retailer or "").strip()
        )

        if not retailer_raw:
            return None

        key = self.store_info_normalizer.normalize_retailer_key(
            retailer_raw
        )
        if key:
            return key

        return self.store_info_normalizer.make_retailer_key(
            retailer_raw
        )