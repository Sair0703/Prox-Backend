# services/store_service/capabilities/store_location_resolution/locators/internal/nearest_store_locator.py

from __future__ import annotations

import asyncio
import logging
from typing import Any

from config.supabase import get_supabase_client
from services.store_service.capabilities.store_info_normalization.store_info_normalization_service import (
    StoreInfoNormalizationService,
)
from services.store_service.geocoders.geocoder import Geocoder
from services.store_service.models.base import FlyerDeal, StoreCandidate

logger = logging.getLogger(__name__)


class NearestStoreLocator:
    """
    Nearest-store locator backed by the PostGIS nearest-store RPC.

    Responsibilities:
    - resolve the deal context coordinates;
    - call the nearest-store RPC;
    - map the RPC row into a StoreCandidate;
    - return the nearest canonical candidate.

    The locator only reads canonical store data and produces candidates.
    """

    LOCATOR_NAME = "nearest_store_locator"
    LOCATOR_TYPE = "internal"

    def __init__(
        self,
        geocoder: Geocoder,
        store_info_normalizer: StoreInfoNormalizationService | None = None,
        supabase=None,
        locator_radius_meters: float = 16093.34,
        show_on_map_only: bool = True,
    ) -> None:
        """
        Initialize the nearest-store locator.

        :param geocoder: Shared geocoder used to resolve ZIP coordinates
            when the deal has no coordinates.
        :param store_info_normalizer: Shared store-info normalization service.
        :param supabase: Optional Supabase client override.
        :param locator_radius_meters: Search radius passed to the nearest-store RPC.
        :param show_on_map_only: Whether the RPC should exclude stores that
            are not marked for map display.
        """
        self.geocoder = geocoder
        self.store_info_normalizer = (
            store_info_normalizer
            or StoreInfoNormalizationService()
        )
        self.supabase = supabase or get_supabase_client()
        self.locator_radius_meters = locator_radius_meters
        self.show_on_map_only = show_on_map_only
        self._zip_cache: dict[str, tuple[float, float]] = {}

    async def find_candidate_stores(
        self,
        deal: FlyerDeal,
    ) -> list[StoreCandidate]:
        """
        Return the nearest canonical store as a candidate list.

        :param deal: Flyer deal containing retailer and location context.
        :return: A one-element candidate list, or an empty list.
        """
        candidate = await self.find_nearest_store(deal)
        return [candidate] if candidate else []

    async def find_nearest_store(
        self,
        deal: FlyerDeal,
    ) -> StoreCandidate | None:
        """
        Return the nearest canonical store for a deal.

        :param deal: Flyer deal containing retailer and location context.
        :return: Nearest internal store candidate, or None when no match exists.
        """
        retailer_key = self._resolve_retailer_key(deal)
        if not retailer_key:
            return None

        zip_code = (deal.zip_code or "").strip()
        if not zip_code:
            return None

        try:
            context_lat, context_lng = await self._resolve_context_coords(
                deal
            )
        except Exception as exc:
            logger.warning(
                "[STORE_LOCATOR] context resolution failed "
                "retailer_key=%s zip=%s error=%s",
                retailer_key,
                zip_code,
                exc,
            )
            return None

        try:
            row = await self._fetch_nearest_row(
                retailer_key=retailer_key,
                lat=context_lat,
                lng=context_lng,
                radius_meters=float(
                    self.locator_radius_meters
                ),
                show_on_map_only=self.show_on_map_only,
            )
        except Exception as exc:
            logger.warning(
                "[STORE_LOCATOR] RPC lookup failed "
                "retailer_key=%s zip=%s error=%s",
                retailer_key,
                zip_code,
                exc,
            )
            return None

        if not row:
            logger.info(
                "[STORE_LOCATOR] no nearest store "
                "retailer_key=%s zip=%s",
                retailer_key,
                zip_code,
            )
            return None

        candidate = self._to_candidate(row)

        logger.info(
            "[STORE_LOCATOR] nearest "
            "retailer_key=%s zip=%s retailer_store_id=%s "
            "canonical_id=%s distance_meters=%.2f",
            retailer_key,
            zip_code,
            candidate.retailer_store_id,
            candidate.canonical_store_id,
            candidate.distance_meters,
        )
        return candidate

    def _zip_key(
        self,
        zip_code: str,
    ) -> str:
        """Normalize a ZIP code for caching."""
        return (zip_code or "").strip()

    def _resolve_retailer_key(
        self,
        deal: FlyerDeal,
    ) -> str | None:
        """
        Resolve the retailer key used by the nearest-store lookup.

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
        key = self._zip_key(zip_code)

        cached = self._zip_cache.get(key)
        if cached is not None:
            logger.debug(
                "[STORE_LOCATOR] zip cache hit zip=%s",
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

        lat = float(coords[0])
        lng = float(coords[1])
        self._zip_cache[key] = (lat, lng)

        logger.debug(
            "[STORE_LOCATOR] geocoded zip=%s lat=%.6f lng=%.6f",
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
        Resolve the coordinates used for nearest-store lookup.

        Deal coordinates are preferred; ZIP geocoding is used as a fallback.

        :param deal: Flyer deal containing optional coordinates and ZIP.
        :return: Latitude and longitude used as the RPC query origin.
        :raises RuntimeError: When no deal coordinates or usable ZIP exists.
        """
        if deal.store_lat is not None and deal.store_lng is not None:
            return float(deal.store_lat), float(deal.store_lng)

        zip_code = (deal.zip_code or "").strip()
        if not zip_code:
            raise RuntimeError(
                "ZIP code is required when deal coordinates are absent"
            )

        return await self._geocode_cached(zip_code)

    async def _fetch_nearest_row(
        self,
        retailer_key: str,
        lat: float,
        lng: float,
        radius_meters: float,
        show_on_map_only: bool = True,
    ) -> dict[str, Any] | None:
        """
        Call the nearest-store RPC and return its first result.

        :param retailer_key: Canonical retailer key used by the RPC.
        :param lat: Query origin latitude.
        :param lng: Query origin longitude.
        :param radius_meters: Search radius passed to the RPC.
        :param show_on_map_only: Whether to restrict results to map-visible stores.
        :return: The nearest RPC row, or None when no row is returned.
        :raises RuntimeError: When the RPC reports an error.
        """
        response = (
            self.supabase.rpc(
                "find_nearest_store",
                {
                    "p_retailer_key": retailer_key,
                    "p_lat": lat,
                    "p_lng": lng,
                    "p_radius_meters": radius_meters,
                    "p_show_on_map_only": show_on_map_only,
                },
            )
            .execute()
        )

        if hasattr(response, "error") and response.error:
            raise RuntimeError(
                f"find_nearest_store RPC failed: {response.error}"
            )

        rows = list(response.data or [])
        if not rows:
            return None

        return rows[0]

    def _to_candidate(
        self,
        row: dict[str, Any],
    ) -> StoreCandidate:
        """
        Convert a nearest-store RPC row into a StoreCandidate.

        :param row: Raw RPC result row.
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
            geocode_source=row.get("geocode_source"),
            geocode_confidence=row.get("geocode_confidence"),
            geocoded_at=row.get("geocoded_at"),
            osm_id=row.get("osm_id"),
            locator_name=self.LOCATOR_NAME,
            locator_type=self.LOCATOR_TYPE,
            show_on_map=row.get("show_on_map"),
            distance_meters=(
                self._as_float(
                    row.get("distance_meters")
                )
                or 0.0
            ),
        )

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


__all__ = ["NearestStoreLocator"]