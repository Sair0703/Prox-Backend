# services/store_location_matcher.py
"""Store-location matching for current flyer_deals rows.

This module intentionally treats flyer_deals.retailer_key as authoritative.
It does not collapse distinct banners into a parent company, and it never
creates synthetic store_locations rows. Matching is based on the existing
store master, with exact-ZIP matching first and a nearest-store fallback.
"""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any

from config.supabase import get_supabase_client

logger = logging.getLogger(__name__)
supabase = get_supabase_client()

CACHE_TTL_SECONDS = int(os.getenv("STORE_LOCATION_CACHE_TTL_SECONDS", "900"))
MAX_NEARBY_MILES = float(os.getenv("STORE_LOCATION_MAX_NEARBY_MILES", "15"))
STORE_PAGE_SIZE = 1000

# Only aliases that preserve the actual retailer banner are allowed here.
# These normalize source/version naming differences into the retailer_key
# used by public.store_locations.
SOURCE_RETAILER_KEY_OVERRIDES: dict[str, str] = {
    "aldiv2": "aldi",
    "aldi_v_2": "aldi",
    "wholefoodsv2": "wholefoods",
    "whole_foods_v_2": "wholefoods",
    "whole_foods": "wholefoods",
    "trader_joes": "traderjoes",
    "sams_club": "samsclub",
    "stop_and_shop": "stopandshop",
    "food_lion": "foodlion",
    "harris_teeter": "harristeeter",
    "winn_dixie": "winndixie",
    "dollar_general": "dollargeneral",
    "family_dollar": "familydollar",
    "save_a_lot": "savealot",
}

DISPLAY_NAME_FALLBACKS: dict[str, str] = {
    "whole foods market": "wholefoods",
    "whole foods": "wholefoods",
    "aldi": "aldi",
    "smart & final": "smart_and_final",
    "smart and final": "smart_and_final",
    "walmart": "walmart",
    "target": "target",
    "costco": "costco",
    "sam's club": "samsclub",
    "sams club": "samsclub",
    "sprouts farmers market": "sprouts",
    "sprouts": "sprouts",
    "ralphs": "ralphs",
    "qfc": "qfc",
    "mariano's": "marianos",
    "marianos": "marianos",
    "foodsco": "foodsco",
    "metro market": "metromarket",
    "fred meyer": "fredmeyer",
    "king soopers": "kingsoopers",
    "smith's": "smiths",
    "fry's": "frys",
    "pick 'n save": "picknsave",
    "food 4 less": "food4less",
    "food4less": "food4less",
    "publix": "publix",
    "albertsons": "albertsons",
    "vons": "vons",
    "safeway": "safeway",
    "pavilions": "pavilions",
}

_store_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_zip_centroid_cache: dict[str, tuple[float, tuple[float, float] | None]] = {}


@dataclass(frozen=True)
class StoreMatchResult:
    store_id: int | None
    match_confidence: str
    candidate_store_count: int
    matched_by: str
    candidate_store_ids: list[int] = field(default_factory=list)
    distance_miles: float | None = None
    store_retailer_key: str | None = None


def canonical_store_retailer_key(
    retailer_key: str | None,
    retailer_raw: str | None = None,
) -> str | None:
    """Resolve the store_locations retailer key without collapsing banners."""
    if retailer_key:
        key = retailer_key.strip().lower()
        if key:
            return SOURCE_RETAILER_KEY_OVERRIDES.get(key, key)

    if retailer_raw:
        raw = retailer_raw.strip().lower()
        if raw:
            if raw in SOURCE_RETAILER_KEY_OVERRIDES:
                return SOURCE_RETAILER_KEY_OVERRIDES[raw]
            if raw in DISPLAY_NAME_FALLBACKS:
                return DISPLAY_NAME_FALLBACKS[raw]

    return None


def _load_retailer_stores(retailer_key: str) -> list[dict[str, Any]]:
    now = time.monotonic()
    cached = _store_cache.get(retailer_key)
    if cached and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    rows: list[dict[str, Any]] = []
    offset = 0

    while True:
        result = (
            supabase.table("store_locations")
            .select("id, retailer_key, zip_code, latitude, longitude")
            .eq("retailer_key", retailer_key)
            .order("id")
            .range(offset, offset + STORE_PAGE_SIZE - 1)
            .execute()
        )
        page = result.data or []
        rows.extend(page)
        if len(page) < STORE_PAGE_SIZE:
            break
        offset += STORE_PAGE_SIZE

    _store_cache[retailer_key] = (now, rows)
    logger.info("[STORE_MATCH] loaded %s stores for retailer_key=%s", len(rows), retailer_key)
    return rows


def _get_zip_centroid(zip_code: str) -> tuple[float, float] | None:
    now = time.monotonic()
    cached = _zip_centroid_cache.get(zip_code)
    if cached and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    result = (
        supabase.table("zip_centroids")
        .select("latitude, longitude")
        .eq("zip_code", zip_code)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    value: tuple[float, float] | None = None
    if rows:
        lat = rows[0].get("latitude")
        lng = rows[0].get("longitude")
        if lat is not None and lng is not None:
            value = (float(lat), float(lng))

    _zip_centroid_cache[zip_code] = (now, value)
    return value


def _coords(store: dict[str, Any]) -> tuple[float, float] | None:
    lat = store.get("latitude")
    lng = store.get("longitude")
    if lat is None or lng is None:
        return None
    try:
        return float(lat), float(lng)
    except (TypeError, ValueError):
        return None


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_miles = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (
        math.sin(dp / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    )
    return radius_miles * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _pick_nearest(
    stores: list[dict[str, Any]],
    ref_lat: float | None,
    ref_lng: float | None,
) -> tuple[int, float | None]:
    if not stores:
        raise ValueError("stores cannot be empty")

    if ref_lat is None or ref_lng is None:
        return int(stores[0]["id"]), None

    candidates: list[tuple[dict[str, Any], float]] = []
    for store in stores:
        point = _coords(store)
        if not point:
            continue
        distance = _haversine(ref_lat, ref_lng, point[0], point[1])
        candidates.append((store, distance))

    if not candidates:
        return int(stores[0]["id"]), None

    store, distance = min(candidates, key=lambda item: item[1])
    return int(store["id"]), distance


def find_store_for_deal(
    *,
    retailer_key: str | None,
    retailer_raw: str | None,
    zip_code: str | None,
    deal_lat: float | None = None,
    deal_lng: float | None = None,
    max_nearby_miles: float | None = None,
) -> StoreMatchResult:
    """Match one flyer row to an existing physical store.

    Matching order:
      1. Exact retailer_key + ZIP.
      2. If no exact-ZIP store exists, nearest store within the configured
         radius using the scrape ZIP centroid.

    No store rows are created by this function.
    """
    store_key = canonical_store_retailer_key(retailer_key, retailer_raw)
    if not store_key or not zip_code:
        return StoreMatchResult(None, "none", 0, "missing_retailer_or_zip")

    stores = _load_retailer_stores(store_key)
    if not stores:
        return StoreMatchResult(
            None, "none", 0, "retailer_not_loaded", store_retailer_key=store_key
        )

    zip_text = str(zip_code).strip()
    exact = [s for s in stores if str(s.get("zip_code") or "").strip() == zip_text]

    ref_lat, ref_lng = deal_lat, deal_lng
    if ref_lat is None or ref_lng is None:
        centroid = _get_zip_centroid(zip_text)
        if centroid:
            ref_lat, ref_lng = centroid

    if exact:
        candidate_ids = [int(s["id"]) for s in exact]
        best_id, distance = _pick_nearest(exact, ref_lat, ref_lng)
        if len(exact) == 1:
            return StoreMatchResult(
                best_id,
                "zip_single",
                1,
                "zip_code",
                [],
                distance,
                store_key,
            )
        return StoreMatchResult(
            best_id,
            "zip_multi",
            len(exact),
            "zip_code",
            candidate_ids,
            distance,
            store_key,
        )

    if ref_lat is None or ref_lng is None:
        return StoreMatchResult(
            None,
            "none",
            0,
            "zip_centroid_unavailable",
            store_retailer_key=store_key,
        )

    radius = MAX_NEARBY_MILES if max_nearby_miles is None else max_nearby_miles
    nearby: list[tuple[dict[str, Any], float]] = []
    for store in stores:
        point = _coords(store)
        if not point:
            continue
        distance = _haversine(ref_lat, ref_lng, point[0], point[1])
        if distance <= radius:
            nearby.append((store, distance))

    if not nearby:
        return StoreMatchResult(
            None,
            "none",
            0,
            "no_store_within_radius",
            store_retailer_key=store_key,
        )

    nearby.sort(key=lambda item: item[1])
    candidate_ids = [int(store["id"]) for store, _ in nearby]
    best_store, best_distance = nearby[0]
    confidence = "nearby_single" if len(nearby) == 1 else "nearby_multi"

    return StoreMatchResult(
        int(best_store["id"]),
        confidence,
        len(nearby),
        "nearest_within_radius",
        candidate_ids if len(nearby) > 1 else [],
        best_distance,
        store_key,
    )


def get_match_cache_stats() -> dict[str, int]:
    return {
        "retailer_store_caches": len(_store_cache),
        "zip_centroid_caches": len(_zip_centroid_cache),
    }
