from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from geopy.extra.rate_limiter import RateLimiter
from geopy.geocoders import Nominatim

from config.settings import GEOAPIFY_API_KEY

logger = logging.getLogger(__name__)


class Geocoder:
    def __init__(
        self,
        cache_path: Path,
        throttle_seconds: float = 1.0,
        max_retries: int = 1,
        geoapify_min_delay_seconds: float = 0.25,
    ) -> None:
        self.cache_path = cache_path
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

        self.throttle_seconds = throttle_seconds
        self.max_retries = max_retries
        self.geoapify_min_delay_seconds = geoapify_min_delay_seconds
        self._last_geoapify_request_start = 0.0

        self.cache: dict[str, object] = self._load_cache()
        self.geoapify_api_key = GEOAPIFY_API_KEY

        self._geolocator = Nominatim(
            user_agent="prox-shopping-geocoder",
            timeout=5,
        )
        self._geocode = RateLimiter(
            self._geolocator.geocode,
            min_delay_seconds=throttle_seconds,
        )

    def geocode(self, address: str, force: bool = False) -> tuple[float, float] | None:
        result = self.geocode_with_provider(address, force=force)
        if result is None:
            return None
        return result[0], result[1]

    def geocode_with_provider(
        self,
        address: str,
        force: bool = False,
        diagnostics: list[str] | None = None,
    ) -> tuple[float, float, str] | None:
        original_key = self._normalize(address)
        candidates = self._build_candidates(address)

        if not force:
            for query, _, cache_keys in candidates:
                for cache_key in cache_keys:
                    cached = self._cache_lookup(cache_key)
                    if cached is not None:
                        lat, lng, provider = cached
                        if cache_key != original_key:
                            self._cache_store(original_key, lat, lng, provider)
                        return lat, lng, provider

        for query, _, cache_keys in candidates:
            result = self._try_nominatim(query, cache_keys)
            if result is not None:
                return result

        for query, _, cache_keys in candidates:
            coords = self._geoapify_geocode(query)
            if coords is not None:
                lat, lng = coords
                provider = "geoapify"
                self._store_many(cache_keys, lat, lng, provider)
                return lat, lng, provider

        if diagnostics is not None:
            diagnostics.append(f"All geocoding attempts failed for {address!r}")

        logger.warning("[GEOCODER] all geocoding attempts failed address=%s", address)
        return None

    def _build_candidates(
        self,
        address: str,
    ) -> list[tuple[str, str, list[str]]]:
        """
        Build geocoding candidates.

        We keep:
          1. original full address
          2. cleaned full address (suite/unit/blg/# stripped)
        We do NOT use zip-code fallback or ZIP centroid fallback.
        """
        original_key = self._normalize(address)
        candidates: list[tuple[str, str, list[str]]] = [
            (address, original_key, [original_key]),
        ]

        cleaned = self._clean_address(address)
        if cleaned != address:
            cleaned_key = self._normalize(cleaned)
            candidates.append((cleaned, cleaned_key, [original_key, cleaned_key]))

        return candidates

    def _try_nominatim(
        self,
        query: str,
        cache_keys: list[str],
        diagnostics: list[str] | None = None,
    ) -> tuple[float, float, str] | None:
        started = time.perf_counter()

        for attempt in range(1, self.max_retries + 1):
            try:
                location = self._geocode(query)
                elapsed = time.perf_counter() - started

                if not location:
                    msg = f"Nominatim returned no result for {query!r} after {elapsed:.2f}s"
                    if diagnostics is not None:
                        diagnostics.append(msg)
                    logger.warning("[GEOCODER] %s", msg)
                    time.sleep(attempt)
                    continue

                lat = float(location.latitude)
                lng = float(location.longitude)
                provider = "nominatim"

                self._store_many(cache_keys, lat, lng, provider)

                logger.info(
                    "[GEOCODER] Nominatim success address=%s lat=%.6f lng=%.6f elapsed=%.2fs",
                    query,
                    lat,
                    lng,
                    elapsed,
                )
                return lat, lng, provider

            except Exception as e:
                elapsed = time.perf_counter() - started
                msg = (
                    f"Nominatim attempt {attempt} failed for {query!r} "
                    f"after {elapsed:.2f}s: {e.__class__.__name__}: {e}"
                )
                if diagnostics is not None:
                    diagnostics.append(msg)
                logger.warning("[GEOCODER] %s", msg)
                time.sleep(attempt)

        if diagnostics is not None:
            diagnostics.append(f"Nominatim exhausted retries for {query!r}")
        return None

    def _clean_address(self, address: str) -> str:
        cleaned = re.sub(
            r",?\s*(STE|SUITE|UNIT|BLDG|#)\s*[A-Z0-9\-#]+",
            "",
            address,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"(\d+)-[A-Z]\b", r"\1", cleaned)
        cleaned = cleaned.replace(".", "").strip()
        return cleaned

    def _normalize(self, address: str) -> str:
        address = address.replace(".", "")
        return " ".join(address.strip().lower().split())

    def _cache_lookup(self, key: str) -> tuple[float, float, str] | None:
        cached = self.cache.get(key)
        if cached is None:
            return None

        if isinstance(cached, dict):
            lat = cached.get("lat")
            lng = cached.get("lng")
            provider = cached.get("provider") or "cache"
            try:
                return float(lat), float(lng), str(provider)
            except Exception:
                return None

        if isinstance(cached, list) and len(cached) >= 2:
            try:
                return float(cached[0]), float(cached[1]), "cache"
            except Exception:
                return None

        if isinstance(cached, tuple) and len(cached) >= 2:
            try:
                return float(cached[0]), float(cached[1]), "cache"
            except Exception:
                return None

        return None

    def _cache_store(self, key: str, lat: float, lng: float, provider: str) -> None:
        self.cache[key] = {
            "lat": float(lat),
            "lng": float(lng),
            "provider": provider,
        }
        self._save_cache()

    def _store_many(
        self,
        keys: list[str],
        lat: float,
        lng: float,
        provider: str,
    ) -> None:
        unique_keys = list(dict.fromkeys(keys))
        for key in unique_keys:
            self.cache[key] = {
                "lat": float(lat),
                "lng": float(lng),
                "provider": provider,
            }
        self._save_cache()

    def _load_cache(self) -> dict[str, object]:
        if not self.cache_path.exists():
            return {}

        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(
                "[GEOCODER] failed to load geocode cache; starting fresh error=%s",
                e,
            )
            return {}

    def _save_cache(self) -> None:
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, indent=2, ensure_ascii=False)
        except Exception:
            logger.exception("[GEOCODER] failed to persist geocode cache")

    def _throttle_geoapify(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_geoapify_request_start
        wait = self.geoapify_min_delay_seconds - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_geoapify_request_start = time.monotonic()

    def _geoapify_geocode(
        self,
        address: str,
        diagnostics: list[str] | None = None,
    ) -> tuple[float, float] | None:
        started = time.perf_counter()

        if not self.geoapify_api_key:
            msg = "Geoapify skipped: API key missing"
            if diagnostics is not None:
                diagnostics.append(msg)
            logger.warning("[GEOCODER] %s", msg)
            return None

        try:
            self._throttle_geoapify()

            params = {
                "text": address,
                "lang": "en",
                "limit": 1,
                "format": "json",
                "filter": "countrycode:us",
                "apiKey": self.geoapify_api_key,
                "bias": "countrycode:us",
            }

            url = "https://api.geoapify.com/v1/geocode/search?" + urlencode(params)

            with urlopen(url, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))

            elapsed = time.perf_counter() - started

            results = payload.get("results", [])
            if not results:
                msg = f"Geoapify returned 0 results for {address!r} after {elapsed:.2f}s"
                if diagnostics is not None:
                    diagnostics.append(msg)
                logger.warning("[GEOCODER] %s", msg)
                return None

            result = results[0]
            latitude = float(result["lat"])
            longitude = float(result["lon"])

            logger.info(
                "[GEOCODER] Geoapify success address=%s lat=%.6f lng=%.6f elapsed=%.2fs",
                address,
                latitude,
                longitude,
                elapsed,
            )

            return latitude, longitude

        except HTTPError as e:
            elapsed = time.perf_counter() - started
            msg = (
                f"Geoapify HTTPError for {address!r} after {elapsed:.2f}s: "
                f"status={getattr(e, 'code', None)} reason={getattr(e, 'reason', None)}"
            )
            if diagnostics is not None:
                diagnostics.append(msg)
            logger.warning("[GEOCODER] %s", msg)
            return None

        except URLError as e:
            elapsed = time.perf_counter() - started
            msg = (
                f"Geoapify URLError for {address!r} after {elapsed:.2f}s: "
                f"{e.__class__.__name__}: {e}"
            )
            if diagnostics is not None:
                diagnostics.append(msg)
            logger.warning("[GEOCODER] %s", msg)
            return None

        except Exception as e:
            elapsed = time.perf_counter() - started
            msg = (
                f"Geoapify failed for {address!r} after {elapsed:.2f}s: "
                f"{e.__class__.__name__}: {e}"
            )
            if diagnostics is not None:
                diagnostics.append(msg)
            logger.warning("[GEOCODER] %s", msg)
            return None


def geocode_store(
    retailer: str,
    zip_code: str,
    address: str | None = None,
    geocoder: Geocoder | None = None,
):
    """
    Returns:
      (latitude, longitude, geocode_confidence, provider, failure_reason, failure_details)

    On success:
      failure_reason/failure_details are None.

    On total failure:
      failure_reason = "GEOCODE_FAILED"
      failure_details = provider-specific diagnostics, if available.
    """
    if geocoder is None:
        logger.warning(
            "[GEOCODER] shared geocoder is required for geocode_store retailer=%s zip=%s",
            retailer,
            zip_code,
        )
        return None, None, "failed", None, "GEOCODER_MISSING", "Shared geocoder is not configured."

    diagnostics: list[str] = []

    if not address:
        msg = "No address available for geocoding."
        diagnostics.append(msg)
        return None, None, "failed", None, "MISSING_ADDRESS", msg

    candidates: list[str] = [address]

    for query in candidates:
        try:
            result = geocoder.geocode_with_provider(query, diagnostics=diagnostics)
            if result and result[0] is not None and result[1] is not None:
                lat, lng, provider = result
                return float(lat), float(lng), "high", provider, None, None
        except Exception as e:
            diagnostics.append(
                f"Unhandled geocoder exception for {query!r}: {e.__class__.__name__}: {e}"
            )
            logger.warning("[GEOCODER] geocoder error query=%s error=%s", query, e)

    failure_details = (
        "; ".join(diagnostics)
        if diagnostics
        else "Nominatim and Geoapify returned no coordinates."
    )

    logger.warning(
        "[GEOCODER] all geocoding attempts failed retailer=%s address=%s details=%s",
        retailer,
        address,
        failure_details,
    )
    return None, None, "failed", None, "GEOCODE_FAILED", failure_details


def geocode_address(
    address: str,
    zip_code: str | None = None,
    city: str | None = None,
    state: str | None = None,
    geocoder: Geocoder | None = None,
) -> dict[str, Any] | None:
    """Wrap geocode_store and return {"lat": ..., "lng": ..., "confidence": ..., "provider": ...} or None."""
    if geocoder is None:
        return None

    location_str = ", ".join(p for p in [address, city, state, zip_code] if p)
    lat, lng, confidence, provider, failure_reason, failure_details = geocode_store(
        retailer="",
        zip_code=zip_code or "",
        address=location_str,
        geocoder=geocoder,
    )
    if lat is not None and lng is not None:
        return {
            "lat": lat,
            "lng": lng,
            "confidence": confidence,
            "provider": provider,
        }
    return None


def geocode_retailer(
    retailer_key: str,
    zip_code: str | None = None,
    city: str | None = None,
    state: str | None = None,
    geocoder: Geocoder | None = None,
) -> dict[str, Any] | None:
    """Wrap geocode_store using retailer name + location as the query."""
    if geocoder is None:
        return None

    name = retailer_key.replace("_", " ").title()
    location_str = ", ".join(p for p in [name, city, state, zip_code] if p)
    lat, lng, confidence, provider, failure_reason, failure_details = geocode_store(
        retailer=name,
        zip_code=zip_code or "",
        address=location_str,
        geocoder=geocoder,
    )
    if lat is not None and lng is not None:
        return {
            "lat": lat,
            "lng": lng,
            "confidence": confidence,
            "provider": provider,
        }
    return None