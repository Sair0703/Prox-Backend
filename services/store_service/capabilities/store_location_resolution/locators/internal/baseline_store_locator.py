# services/store_service/capabilities/store_location_resolution/locators/internal/baseline_store_locator.py

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from config.supabase import get_supabase_client
from services.store_service.capabilities.store_info_normalization.store_info_normalization_service import (
    StoreInfoNormalizationService,
)
from services.store_service.geocoders.geocoder import Geocoder
from services.store_service.models.base import FlyerDeal, StoreCandidate

logger = logging.getLogger(__name__)


class BaselineStoreLocator:
    """
    Legacy baseline store locator.

    Responsibilities:
    - try exact retailer_key + ZIP match;
    - fall back to retailer_key + city/state match;
    - sort candidates by distance to the deal context.

    Notes:
    - store_locations.id is treated as canonical_store_id;
    - store_locations.store_id is treated as retailer_store_id.
    """

    LOCATOR_NAME = "baseline_store_locator"
    LOCATOR_TYPE = "internal"

    def __init__(
        self,
        geocoder: Geocoder,
        store_info_normalizer: StoreInfoNormalizationService | None = None,
        supabase=None,
        show_on_map_only: bool = True,
    ) -> None:
        """
        Initialize the baseline store locator.

        :param geocoder: Shared geocoder used to resolve ZIP coordinates
            when a deal does not already contain coordinates.
        :param store_info_normalizer: Shared store-info normalization service.
        :param supabase: Optional Supabase client override.
        :param show_on_map_only: Whether to exclude canonical stores that are
            not marked for map display.
        """
        self.geocoder = geocoder
        self.store_info_normalizer = (
            store_info_normalizer
            or StoreInfoNormalizationService()
        )
        self.supabase = supabase or get_supabase_client()
        self.show_on_map_only = show_on_map_only
        self._zip_cache: dict[str, tuple[float, float]] = {}

    async def find_candidate_stores(
        self,
        deal: FlyerDeal,
    ) -> list[StoreCandidate]:
        """
        Return baseline canonical store candidates ordered by distance.

        Matching order:
        1. exact retailer_key + ZIP;
        2. retailer_key + city/state.

        :param deal: Flyer deal containing retailer and location context.
        :return: Matching internal store candidates ordered by distance.
        """
        retailer_key = self._resolve_retailer_key(deal)
        zip_code = self._norm(deal.zip_code)

        if not retailer_key or not zip_code:
            return []

        try:
            ref_lat, ref_lng = await self._resolve_context_coords(deal)
        except Exception as exc:
            logger.warning(
                "[BASELINE_LOCATOR] context resolution failed "
                "retailer_key=%s zip=%s error=%s",
                retailer_key,
                zip_code,
                exc,
            )
            return []

        try:
            rows = await self._fetch_zip_rows(
                retailer_key=retailer_key,
                zip_code=zip_code,
            )
        except Exception as exc:
            logger.warning(
                "[BASELINE_LOCATOR] ZIP lookup failed "
                "retailer_key=%s zip=%s error=%s",
                retailer_key,
                zip_code,
                exc,
            )
            return []

        if not rows:
            try:
                rows = await self._fetch_city_state_rows(
                    retailer_key=retailer_key,
                    city=deal.city,
                    state=deal.state,
                )
            except Exception as exc:
                logger.warning(
                    "[BASELINE_LOCATOR] city/state lookup failed "
                    "retailer_key=%s city=%s state=%s error=%s",
                    retailer_key,
                    deal.city,
                    deal.state,
                    exc,
                )
                return []

        if not rows:
            logger.info(
                "[BASELINE_LOCATOR] no rows "
                "retailer_key=%s zip=%s city=%s state=%s",
                retailer_key,
                zip_code,
                deal.city,
                deal.state,
            )
            return []

        candidates = self._rows_to_candidates(
            rows=rows,
            ref_lat=ref_lat,
            ref_lng=ref_lng,
        )

        if not candidates:
            logger.info(
                "[BASELINE_LOCATOR] no candidates after filtering "
                "retailer_key=%s zip=%s city=%s state=%s",
                retailer_key,
                zip_code,
                deal.city,
                deal.state,
            )
            return []

        logger.info(
            "[BASELINE_LOCATOR] candidates "
            "retailer_key=%s zip=%s city=%s state=%s count=%d",
            retailer_key,
            zip_code,
            deal.city,
            deal.state,
            len(candidates),
        )
        return candidates

    def _norm(self, value: str | None) -> str:
        """Normalize an optional string for lookup."""
        return (value or "").strip()

    def _resolve_retailer_key(
        self,
        deal: FlyerDeal,
    ) -> str | None:
        """
        Resolve the retailer key used by the canonical store lookup.

        Prefer the persisted retailer key when available; otherwise derive it
        from the retailer name.

        :param deal: Flyer deal containing retailer identity.
        :return: Canonical retailer key, or None when retailer identity is missing.
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

    async def _geocode_cached(
        self,
        zip_code: str,
    ) -> tuple[float, float]:
        """
        Resolve and cache ZIP coordinates.

        :param zip_code: ZIP code used as the geocoding query.
        :return: Latitude and longitude for the ZIP code.
        :raises RuntimeError: When ZIP geocoding fails.
        """
        key = self._norm(zip_code)

        cached = self._zip_cache.get(key)
        if cached is not None:
            logger.debug(
                "[BASELINE_LOCATOR] zip cache hit zip=%s",
                zip_code,
            )
            return cached

        coords = await asyncio.to_thread(
            self.geocoder.geocode,
            f"{zip_code}, US",
        )
        if not coords:
            raise RuntimeError(
                f"Geocoding failed for ZIP {zip_code}"
            )

        lat, lng = float(coords[0]), float(coords[1])
        self._zip_cache[key] = (lat, lng)

        logger.debug(
            "[BASELINE_LOCATOR] geocoded zip=%s lat=%.6f lng=%.6f",
            zip_code,
            lat,
            lng,
        )
        return lat, lng

    async def _resolve_context_coords(
        self,
        deal: FlyerDeal,
    ) -> tuple[float, float]:
        """
        Resolve the coordinate used for candidate distance ordering.

        Deal coordinates are preferred; ZIP geocoding is used as a fallback.

        :param deal: Flyer deal containing optional coordinates and ZIP.
        :return: Latitude and longitude used as the ranking reference point.
        :raises RuntimeError: When no deal coordinates or usable ZIP exists.
        """
        if deal.store_lat is not None and deal.store_lng is not None:
            return float(deal.store_lat), float(deal.store_lng)

        zip_code = self._norm(deal.zip_code)
        if not zip_code:
            raise RuntimeError(
                "ZIP code is required when deal coordinates are absent"
            )

        return await self._geocode_cached(zip_code)

    async def _fetch_zip_rows(
        self,
        retailer_key: str,
        zip_code: str,
    ) -> list[dict[str, Any]]:
        """
        Fetch canonical store rows matching retailer and ZIP.

        :param retailer_key: Canonical retailer key.
        :param zip_code: Five-digit ZIP code used for lookup.
        :return: Raw canonical store rows returned by Supabase.
        """
        response = (
            self.supabase.table("store_locations")
            .select(
                "id, retailer, store_id, retailer_key, store_name, address, "
                "full_address, city, state, zip_code, latitude, longitude, "
                "geocode_confidence, show_on_map"
            )
            .eq("retailer_key", retailer_key)
            .eq("zip_code", zip_code)
            .execute()
        )

        if hasattr(response, "error") and response.error:
            raise RuntimeError(
                f"store_locations ZIP lookup failed: {response.error}"
            )

        return list(response.data or [])

    async def _fetch_city_state_rows(
        self,
        retailer_key: str,
        city: str | None,
        state: str | None,
    ) -> list[dict[str, Any]]:
        """
        Fetch canonical stores using retailer, city, and state.

        :param retailer_key: Canonical retailer key.
        :param city: Deal city used for fallback matching.
        :param state: Deal state used for fallback matching.
        :return: Raw canonical store rows returned by Supabase.
        """
        normalized_city = self._norm(city)
        normalized_state = self._norm(state)

        if not normalized_city or not normalized_state:
            return []

        response = (
            self.supabase.table("store_locations")
            .select(
                "id, retailer, store_id, retailer_key, store_name, address, "
                "full_address, city, state, zip_code, latitude, longitude, "
                "geocode_confidence, show_on_map"
            )
            .eq("retailer_key", retailer_key)
            .ilike("city", normalized_city)
            .ilike("state", normalized_state)
            .execute()
        )

        if hasattr(response, "error") and response.error:
            raise RuntimeError(
                f"store_locations city/state lookup failed: {response.error}"
            )

        return list(response.data or [])

    @staticmethod
    def _as_float(
        value: Any,
    ) -> float | None:
        """Convert a value to float when possible."""
        if value is None:
            return None

        try:
            return float(value)
        except Exception:
            return None

    def _to_candidate(
        self,
        row: dict[str, Any],
        distance_meters: float,
    ) -> StoreCandidate:
        """
        Convert a canonical store row into a StoreCandidate.

        :param row: Raw canonical store row returned by Supabase.
        :param distance_meters: Distance from the deal context.
        :return: Internal store candidate.
        """
        return StoreCandidate(
            canonical_store_id=int(row["id"]),
            retailer=row.get("retailer"),
            retailer_store_id=row.get("store_id"),
            retailer_key=row.get("retailer_key"),
            store_name=row.get("store_name"),
            address=row.get("address"),
            full_address=row.get("full_address"),
            city=row.get("city"),
            state=row.get("state"),
            zip_code=row.get("zip_code"),
            latitude=self._as_float(row.get("latitude")),
            longitude=self._as_float(row.get("longitude")),
            geocode_confidence=row.get("geocode_confidence"),
            show_on_map=row.get("show_on_map"),
            distance_meters=distance_meters,
            locator_name=self.LOCATOR_NAME,
            locator_type=self.LOCATOR_TYPE,
        )

    @staticmethod
    def _haversine_meters(
        lat1: float,
        lng1: float,
        lat2: float,
        lng2: float,
    ) -> float:
        """
        Calculate the Haversine distance between two coordinates.

        :param lat1: First latitude.
        :param lng1: First longitude.
        :param lat2: Second latitude.
        :param lng2: Second longitude.
        :return: Distance between the points in meters.
        """
        radius = 6371000.0

        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        delta_phi = math.radians(lat2 - lat1)
        delta_lambda = math.radians(lng2 - lng1)

        a = (
            math.sin(delta_phi / 2.0) ** 2
            + math.cos(phi1)
            * math.cos(phi2)
            * math.sin(delta_lambda / 2.0) ** 2
        )
        c = 2.0 * math.atan2(
            math.sqrt(a),
            math.sqrt(1.0 - a),
        )
        return radius * c

    def _rows_to_candidates(
        self,
        rows: list[dict[str, Any]],
        ref_lat: float,
        ref_lng: float,
    ) -> list[StoreCandidate]:
        """
        Convert canonical store rows into sorted candidates.

        :param rows: Raw canonical store rows.
        :param ref_lat: Reference latitude used for distance calculation.
        :param ref_lng: Reference longitude used for distance calculation.
        :return: Filtered and distance-sorted internal candidates.
        """
        candidates: list[StoreCandidate] = []

        for row in rows:
            if (
                self.show_on_map_only
                and not bool(row.get("show_on_map", True))
            ):
                continue

            lat = self._as_float(row.get("latitude"))
            lng = self._as_float(row.get("longitude"))

            if lat is None or lng is None:
                distance_meters = float("inf")
            else:
                distance_meters = self._haversine_meters(
                    ref_lat,
                    ref_lng,
                    lat,
                    lng,
                )

            candidates.append(
                self._to_candidate(
                    row=row,
                    distance_meters=distance_meters,
                )
            )

        candidates.sort(
            key=lambda candidate: (
                candidate.distance_meters,
                candidate.canonical_store_id,
            )
        )
        return candidates


__all__ = ["BaselineStoreLocator"]