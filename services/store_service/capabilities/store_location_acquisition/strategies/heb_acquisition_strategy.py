# services/store_service/capabilities/store_location_acquisition/strategies/heb_acquisition_strategy.py

"""Acquisition strategy for H-E-B store locations."""

from __future__ import annotations

import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from threading import Lock
from typing import Any
from urllib.parse import quote

import pgeocode
import requests
from tqdm import tqdm


RETAILER = "H-E-B"
RETAILER_KEY = "heb"

HOMEPAGE_URL = "https://www.heb.com/store-locations"

# Deployment-specific fallback used when build ID discovery is unavailable.
KNOWN_BUILD_ID = "23ada7fe11e9545364140b2b171397fbfa0072fc"

NEXT_DATA_TEMPLATE = (
    "https://www.heb.com/_next/data/{build_id}/en/"
    "store-locations.json?address={address}&page={page}"
)

MIN_DELAY_SECONDS = 0.8
MAX_DELAY_SECONDS = 1.8
MAX_RETRIES = 4
BACKOFFS = (2.0, 4.0, 8.0, 16.0)
CHECKPOINT_EVERY = 100
MAX_WORKERS = 24


def get_all_texas_zip_codes() -> tuple[str, ...]:
    """Return all Texas ZIP codes available in pgeocode."""
    geo = pgeocode.Nominatim("us")
    data = geo._data.copy()

    texas = data[
        data["state_code"].eq("TX")
        & data["postal_code"].notna()
    ].copy()

    return tuple(
        texas["postal_code"]
        .astype(str)
        .str.replace(r"\.0$", "", regex=True)
        .str.zfill(5)
        .drop_duplicates()
        .sort_values()
        .tolist()
    )


REGIONAL_ZIP_SEEDS: tuple[str, ...] = get_all_texas_zip_codes()


@dataclass(slots=True, frozen=True)
class GeographicSeed:
    """Geographic seed used to query the retailer locator."""

    zip_code: str
    label: str


@dataclass(slots=True)
class SeedResult:
    """Result of acquiring one geographic seed."""

    seed: GeographicSeed
    records: list[dict[str, Any]]
    pages: int
    reported_total: int
    status: str
    attempts: int
    error: str | None = None


class StaleBuildIDError(RuntimeError):
    """Raised when the H-E-B Next.js build ID is no longer valid."""


class HEBAcquisitionStrategy:
    """
    Acquires H-E-B store locations from the official Next.js locator.

    Uses Texas ZIP-code seeds, paginated locator responses, and storeNumber
    as the retailer store identifier.
    """

    def __init__(
        self,
        *,
        seeds: tuple[str, ...] = REGIONAL_ZIP_SEEDS,
        min_delay: float = MIN_DELAY_SECONDS,
        max_delay: float = MAX_DELAY_SECONDS,
        session: requests.Session | None = None,
        workers: int = MAX_WORKERS,
        checkpoint_callback: Any | None = None,
        checkpoint_every: int = CHECKPOINT_EVERY,
    ) -> None:
        """
        Initialize the H-E-B acquisition strategy.

        :param seeds: ZIP-code seeds used to enumerate the H-E-B locator.
        :param min_delay: Minimum delay between requests.
        :param max_delay: Maximum delay between requests.
        :param session: Optional shared HTTP session.
        :param workers: Maximum number of concurrent seed workers.
        :param checkpoint_callback: Optional callback for periodic checkpoints.
        :param checkpoint_every: Number of completed seeds between checkpoints.
        """
        self.seeds = tuple(
            GeographicSeed(zip_code=z, label=f"ZIP {z}")
            for z in seeds
        )
        self.min_delay = max(0.0, min_delay)
        self.max_delay = max(self.min_delay, max_delay)
        self.workers = max(1, min(int(workers), 32))
        self.checkpoint_callback = checkpoint_callback
        self.checkpoint_every = max(1, int(checkpoint_every))

        self._session = session or requests.Session()
        self._configure_session(self._session)

        self.build_id: str | None = None
        self.failed_seeds: list[dict[str, Any]] = []
        self.empty_seeds: list[dict[str, Any]] = []

    @staticmethod
    def _configure_session(session: requests.Session) -> None:
        """Configure common headers for H-E-B locator requests."""
        session.headers.update(
            {
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": HOMEPAGE_URL,
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/151.0.0.0 Safari/537.36"
                ),
                "x-nextjs-data": "1",
            }
        )

    def _new_worker_session(self) -> requests.Session:
        """Create a configured session for a worker."""
        session = requests.Session()
        self._configure_session(session)
        return session

    def acquire(
        self,
        *,
        seed_limit: int | None = None,
    ) -> dict[str, Any]:
        """
        Run the H-E-B store-location acquisition workflow.

        :param seed_limit: Optional limit on the number of ZIP seeds to process.
        :return: Acquired records, validation results, and run metadata.
        """
        seeds = list(self.seeds)

        if seed_limit is not None:
            seeds = seeds[: max(0, seed_limit)]

        self.build_id = self._discover_build_id()

        print(f"H-E-B Next.js build ID: {self.build_id}")
        print(f"H-E-B regional ZIP seeds: {len(seeds)}")

        raw_records: list[dict[str, Any]] = []
        successful_seeds = 0
        stopped_early = False
        max_reported_total = 0

        results: dict[int, SeedResult] = {}
        rolling_unique_store_ids: set[str] = set()
        rolling_lock = Lock()

        # Each ZIP seed is processed independently and merged after acquisition.
        with ThreadPoolExecutor(
            max_workers=self.workers,
        ) as executor:
            future_map = {
                executor.submit(
                    self._acquire_seed,
                    seed,
                    self._new_worker_session(),
                ): index
                for index, seed in enumerate(seeds, start=1)
            }

            with tqdm(
                total=len(seeds),
                desc="H-E-B Texas ZIP acquisition",
                unit="zip",
            ) as progress:
                completed = 0

                for future in as_completed(future_map):
                    index = future_map[future]
                    seed = seeds[index - 1]

                    try:
                        result = future.result()
                    except StaleBuildIDError:
                        # A stale deployment-wide build ID invalidates the run.
                        for pending in future_map:
                            pending.cancel()
                        raise

                    except Exception as exc:
                        result = SeedResult(
                            seed=seed,
                            records=[],
                            pages=0,
                            reported_total=0,
                            status="failed",
                            attempts=1,
                            error=repr(exc),
                        )

                    results[index] = result
                    completed += 1

                    if result.status == "success":
                        successful_seeds += 1

                        if not result.records:
                            self.empty_seeds.append(
                                {
                                    "zip_code": seed.zip_code,
                                    "label": seed.label,
                                }
                            )
                    else:
                        self.failed_seeds.append(
                            {
                                "zip_code": seed.zip_code,
                                "label": seed.label,
                                "attempts": result.attempts,
                                "error": result.error,
                            }
                        )

                    max_reported_total = max(
                        max_reported_total,
                        result.reported_total,
                    )

                    with rolling_lock:
                        for record in result.records:
                            store_id = record.get("retailer_store_id")

                            if store_id:
                                rolling_unique_store_ids.add(
                                    str(store_id)
                                )

                    progress.set_postfix(
                        unique=len(rolling_unique_store_ids),
                        ok=successful_seeds,
                        empty=len(self.empty_seeds),
                        failed=len(self.failed_seeds),
                    )
                    progress.update(1)

                    if (
                        self.checkpoint_callback is not None
                        and completed % self.checkpoint_every == 0
                    ):
                        checkpoint_raw: list[dict[str, Any]] = []

                        for completed_index in sorted(results):
                            checkpoint_raw.extend(
                                results[completed_index].records
                            )

                        self.checkpoint_callback(
                            completed=completed,
                            total=len(seeds),
                            records=self._merge_records(checkpoint_raw),
                            failed_seeds=list(self.failed_seeds),
                            empty_seeds=list(self.empty_seeds),
                        )

        for index in sorted(results):
            raw_records.extend(results[index].records)

        records = self._merge_records(raw_records)

        validation = self._validate(
            seeds=seeds,
            successful_seeds=successful_seeds,
            raw_records=raw_records,
            records=records,
            max_reported_total=max_reported_total,
            stopped_early=stopped_early,
        )

        return {
            "retailer": RETAILER,
            "retailer_key": RETAILER_KEY,
            "source_type": "json",
            "build_id": self.build_id,
            "records": records,
            "validation": validation,
            "failed_seeds": self.failed_seeds,
            "empty_seeds": self.empty_seeds,
            "seed_definitions": [
                {
                    "zip_code": seed.zip_code,
                    "label": seed.label,
                }
                for seed in seeds
            ],
            "notes": self._notes(),
        }

    def _discover_build_id(self) -> str:
        """
        Resolve the active Next.js build ID for the locator endpoint.

        The homepage is preferred; the configured deployment-specific
        fallback is used only when discovery is unavailable.
        """
        configured = os.getenv("HEB_BUILD_ID")

        if configured:
            print("H-E-B build ID source: HEB_BUILD_ID")
            return configured.strip()

        try:
            response = self._session.get(
                HOMEPAGE_URL,
                timeout=30,
            )
            response.raise_for_status()

            html = response.text

            patterns = (
                r'/_next/data/([A-Za-z0-9_-]+)/en/store-locations\.json',
                r'"buildId"\s*:\s*"([^"]+)"',
            )

            for pattern in patterns:
                match = re.search(pattern, html)

                if match:
                    build_id = match.group(1)
                    print("H-E-B build ID source: homepage HTML")
                    return build_id

        except Exception as exc:
            print(
                "H-E-B homepage build-ID discovery warning: "
                f"{exc!r}"
            )

        print(
            "H-E-B build ID source: observed locator request "
            f"fallback ({KNOWN_BUILD_ID})"
        )
        return KNOWN_BUILD_ID

    def _acquire_seed(
        self,
        seed: GeographicSeed,
        session: requests.Session | None = None,
    ) -> SeedResult:
        """
        Acquire all paginated store records for one ZIP seed.

        :param seed: Geographic ZIP seed used for the locator query.
        :param session: HTTP session used by the worker.
        :return: Acquisition result for the seed.
        """
        records: list[dict[str, Any]] = []
        page = 1
        reported_total = 0

        # Continue until the locator returns an empty page or the
        # accumulated records reach the reported total.
        while page <= 200:
            result = self._request_page(
                seed=seed,
                page=page,
                session=session,
            )

            if result is None:
                return SeedResult(
                    seed=seed,
                    records=records,
                    pages=page - 1,
                    reported_total=reported_total,
                    status="failed",
                    attempts=MAX_RETRIES,
                    error=f"Failed to acquire page {page}.",
                )

            (
                page_records,
                total_stores,
                current_page,
                raw_page_count,
            ) = result

            reported_total = max(
                reported_total,
                total_stores,
            )

            if not page_records:
                break

            records.extend(page_records)

            # Defensively verify that the response matches the requested page.
            if current_page and current_page != page:
                return SeedResult(
                    seed=seed,
                    records=records,
                    pages=page,
                    reported_total=reported_total,
                    status="failed",
                    attempts=1,
                    error=(
                        f"Requested page={page} but response "
                        f"currentPage={current_page}."
                    ),
                )

            if (
                reported_total > 0
                and len(records) >= reported_total
            ):
                break

            page += 1

        if page > 200:
            return SeedResult(
                seed=seed,
                records=records,
                pages=200,
                reported_total=reported_total,
                status="failed",
                attempts=1,
                error="Pagination exceeded 200 pages.",
            )

        return SeedResult(
            seed=seed,
            records=records,
            pages=max(1, page),
            reported_total=reported_total,
            status="success",
            attempts=1,
        )

    def _request_page(
        self,
        *,
        seed: GeographicSeed,
        page: int,
        session: requests.Session | None = None,
    ) -> tuple[
        list[dict[str, Any]],
        int,
        int,
        int,
    ] | None:
        """
        Request and parse one H-E-B locator page.

        :param seed: Geographic ZIP seed used for the request.
        :param page: One-based locator page number.
        :param session: Optional HTTP session.
        :return: Page records and pagination metadata, or None on failure.
        :raises StaleBuildIDError: If the refreshed build ID still returns HTTP 404.
        """
        assert self.build_id is not None

        request_session = session or self._session

        url = NEXT_DATA_TEMPLATE.format(
            build_id=self.build_id,
            address=quote(seed.zip_code),
            page=page,
        )

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = request_session.get(
                    url,
                    timeout=30,
                )

                # Refresh the build ID once when a deployment may have changed it.
                if response.status_code == 404:
                    self.build_id = self._discover_build_id()
                    url = NEXT_DATA_TEMPLATE.format(
                        build_id=self.build_id,
                        address=quote(seed.zip_code),
                        page=page,
                    )
                    response = request_session.get(
                        url,
                        timeout=30,
                    )

                    if response.status_code == 404:
                        raise StaleBuildIDError(
                            "H-E-B Next.js data route returned HTTP 404 after build-ID refresh. "
                            "The fallback build ID is likely stale after an H-E-B website deployment. "
                            "Open https://www.heb.com/store-locations, open browser DevTools > Network, "
                            "search for a store ZIP code, and find a successful request matching "
                            "'/_next/data/<BUILD_ID>/en/store-locations.json?address=<ZIP>&page=<PAGE>'. "
                            "Copy <BUILD_ID> from the request URL, update KNOWN_BUILD_ID, and rerun "
                            "the acquisition."
                        )

                if response.status_code != 200:
                    raise RuntimeError(
                        f"HTTP {response.status_code}: "
                        f"{response.text[:300]!r}"
                    )

                payload = response.json()
                page_props = payload.get("pageProps", {})

                if page_props.get("searchError"):
                    raise RuntimeError(
                        "H-E-B locator returned searchError=true."
                    )

                current_page_stores = page_props.get(
                    "currentPageStores",
                    [],
                )

                if not isinstance(current_page_stores, list):
                    raise RuntimeError(
                        "Unexpected schema: "
                        "pageProps.currentPageStores is not a list."
                    )

                records: list[dict[str, Any]] = []

                for result in current_page_stores:
                    if not isinstance(result, dict):
                        continue

                    store = result.get("store")

                    if not isinstance(store, dict):
                        continue

                    normalized = self._normalize_store(
                        store=store,
                        distance_miles=result.get("distanceMiles"),
                        seed=seed,
                    )

                    if normalized is not None:
                        records.append(normalized)

                total_stores = self._int_or_zero(
                    page_props.get("totalStoresCount")
                )
                current_page = self._int_or_zero(
                    page_props.get("currentPage")
                )

                return (
                    records,
                    total_stores,
                    current_page,
                    len(current_page_stores),
                )

            except StaleBuildIDError:
                raise

            except Exception as exc:
                if attempt == MAX_RETRIES:
                    print(
                        "Request failed: "
                        f"seed={seed.zip_code} "
                        f"page={page} "
                        f"attempt={attempt}/{MAX_RETRIES} "
                        f"error={exc!r}"
                    )
                    return None

                time.sleep(
                    BACKOFFS[
                        min(
                            attempt - 1,
                            len(BACKOFFS) - 1,
                        )
                    ]
                )

        return None

    def _normalize_store(
        self,
        *,
        store: dict[str, Any],
        distance_miles: Any,
        seed: GeographicSeed,
    ) -> dict[str, Any] | None:
        """
        Convert an H-E-B API store object into an acquisition payload.

        :param store: Raw store object returned by the H-E-B locator.
        :param distance_miles: Locator-reported distance for the store.
        :param seed: ZIP seed used for the locator query.
        :return: Normalized store record, or None when no store number exists.
        """
        store_number = store.get("storeNumber")

        if store_number in (None, ""):
            return None

        address = store.get("address") or {}
        areas = store.get("areas") or []

        area_names = sorted(
            {
                str(area.get("name")).strip()
                for area in areas
                if isinstance(area, dict)
                and area.get("name")
            }
        )

        fulfillments = sorted(
            {
                str(item.get("name")).strip()
                for item in store.get("storeFulfillments") or []
                if isinstance(item, dict)
                and item.get("name")
            }
        )

        return {
            "retailer": RETAILER,
            "retailer_key": RETAILER_KEY,
            "store_name": self._clean(store.get("name")),
            "retailer_store_id": str(store_number),
            "store_number": str(store_number),
            "retail_format_code": self._clean(
                store.get("retailFormatCode")
            ),
            "address": self._clean(
                address.get("streetAddress")
            ),
            "city": self._clean(
                address.get("locality")
            ),
            "state": self._clean(
                address.get("region")
            ),
            "zip_code": self._clean(
                address.get("postalCode")
            ),
            "full_address": self._build_full_address(
                address=self._clean(
                    address.get("streetAddress")
                ),
                city=self._clean(
                    address.get("locality")
                ),
                state=self._clean(
                    address.get("region")
                ),
                zip_code=self._clean(
                    address.get("postalCode")
                ),
            ),
            "phone": self._clean(
                store.get("phoneNumber")
            ),
            "latitude": self._float_or_none(
                store.get("latitude")
            ),
            "longitude": self._float_or_none(
                store.get("longitude")
            ),
            "pharmacy_store": bool(
                store.get("pharmacyStore")
            ),
            "area_names": "|".join(area_names),
            "store_fulfillments": "|".join(fulfillments),
            "distance_miles": self._float_or_none(
                distance_miles
            ),
            "source": (
                "H-E-B official store locator "
                "Next.js JSON"
            ),
            "source_type": "json",
            "query_zip": seed.zip_code,
        }

    @staticmethod
    def _merge_records(
        raw_records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Merge overlapping ZIP results by retailer store ID.

        :param raw_records: Records collected across all ZIP seeds.
        :return: Deduplicated store records keyed by retailer store ID.
        """
        merged: dict[str, dict[str, Any]] = {}

        for record in raw_records:
            store_id = record.get("retailer_store_id")

            if not store_id:
                continue

            key = str(store_id)
            existing = merged.get(key)

            if existing is None:
                merged[key] = dict(record)
            else:
                merged[key] = HEBAcquisitionStrategy._merge_two(
                    existing,
                    record,
                )

        records = list(merged.values())

        records.sort(
            key=lambda row: (
                row.get("state") or "",
                row.get("city") or "",
                row.get("store_name") or "",
                row.get("retailer_store_id") or "",
            )
        )

        return records

    @staticmethod
    def _merge_two(
        first: dict[str, Any],
        second: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Merge non-empty values from overlapping store records.

        :param first: Existing normalized store record.
        :param second: Newly acquired overlapping record.
        :return: Combined store record.
        """
        merged = dict(first)

        for key, value in second.items():
            if (
                merged.get(key) in (None, "")
                and value not in (None, "")
            ):
                merged[key] = value

        first_query = first.get("query_zip")
        second_query = second.get("query_zip")

        if (
            first_query
            and second_query
            and first_query != second_query
        ):
            values = set()

            existing = merged.get("query_zip_all")

            if existing:
                values.update(
                    str(existing).split("|")
                )

            values.add(str(first_query))
            values.add(str(second_query))

            merged["query_zip_all"] = "|".join(
                sorted(values)
            )

        return merged

    def _validate(
        self,
        *,
        seeds: list[GeographicSeed],
        successful_seeds: int,
        raw_records: list[dict[str, Any]],
        records: list[dict[str, Any]],
        max_reported_total: int,
        stopped_early: bool,
    ) -> dict[str, Any]:
        """
        Validate acquisition coverage and store-record completeness.

        :param seeds: ZIP seeds included in the acquisition run.
        :param successful_seeds: Number of seeds completed successfully.
        :param raw_records: Records collected before deduplication.
        :param records: Final deduplicated records.
        :param max_reported_total: Largest locator-reported store count.
        :param stopped_early: Whether acquisition stopped before all seeds completed.
        :return: Validation metrics and coverage statistics.
        """
        state_counts: dict[str, int] = {}
        format_counts: dict[str, int] = {}

        for record in records:
            state = record.get("state") or "UNKNOWN"
            state_counts[state] = state_counts.get(state, 0) + 1

            format_code = (
                record.get("retail_format_code")
                or "UNKNOWN"
            )
            format_counts[format_code] = (
                format_counts.get(format_code, 0) + 1
            )

        return {
            "valid": (
                bool(records)
                and not stopped_early
                and not self.failed_seeds
            ),
            "total_records": len(records),
            "unique_store_ids": len(
                {
                    row.get("retailer_store_id")
                    for row in records
                    if row.get("retailer_store_id")
                }
            ),
            "raw_api_records": len(raw_records),
            "duplicate_records_merged": max(
                0,
                len(raw_records) - len(records),
            ),
            "missing_store_ids": sum(
                not row.get("retailer_store_id")
                for row in records
            ),
            "missing_addresses": sum(
                not row.get("full_address")
                for row in records
            ),
            "missing_phones": sum(
                not row.get("phone")
                for row in records
            ),
            "missing_coordinates": sum(
                row.get("latitude") is None
                or row.get("longitude") is None
                for row in records
            ),
            "regional_zip_seeds": len(seeds),
            "successful_seed_queries": successful_seeds,
            "empty_seed_queries": len(self.empty_seeds),
            "failed_seed_queries": len(self.failed_seeds),
            "max_reported_seed_total": max_reported_total,
            "stopped_early": stopped_early,
            "state_counts": dict(sorted(state_counts.items())),
            "retail_format_counts": dict(
                sorted(format_counts.items())
            ),
        }

    @staticmethod
    def _notes() -> list[str]:
        """Return notes describing the H-E-B acquisition approach."""
        return [
            "Official source: H-E-B store locator Next.js SSR JSON endpoint.",
            "The Next.js build ID is discovered dynamically from the official /store-locations page.",
            "Every Texas ZIP code available in pgeocode is queried to maximize official locator coverage.",
            (
                f"Independent ZIP seeds are acquired concurrently with "
                f"a bounded pool of {MAX_WORKERS} workers with minimal delay."
            ),
            "Each seed is paginated using the official page query parameter and currentPageStores response.",
            (
                "Pagination stops when accumulated store cards reach the "
                "reported totalStoresCount, with an empty-page fallback."
            ),
            "storeNumber is used as retailer_store_id/store_number.",
            "Official latitude/longitude and phone are preserved.",
            "pharmacyStore is retained as metadata and is not an exclusion rule.",
            "Overlapping geographic results are merged globally by storeNumber.",
        ]

    @staticmethod
    def _sleep_random(
        min_delay: float,
        max_delay: float,
    ) -> None:
        """
        Sleep for a random interval within the configured range.

        :param min_delay: Minimum delay in seconds.
        :param max_delay: Maximum delay in seconds.
        """
        time.sleep(
            random.uniform(
                min_delay,
                max_delay,
            )
        )

    @staticmethod
    def _clean(value: Any) -> str | None:
        """
        Convert a value to trimmed text.

        :param value: Value to normalize.
        :return: Trimmed text, or None for empty values.
        """
        if value is None:
            return None

        text = str(value).strip()
        return text or None

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        """
        Convert a value to float when possible.

        :param value: Value to convert.
        :return: Parsed float, or None when conversion fails.
        """
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _int_or_zero(value: Any) -> int:
        """
        Convert a value to int, returning zero on failure.

        :param value: Value to convert.
        :return: Parsed integer, or zero when conversion fails.
        """
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _build_full_address(
        *,
        address: str | None,
        city: str | None,
        state: str | None,
        zip_code: str | None,
    ) -> str | None:
        """
        Compose the available address components into one string.

        :param address: Street address.
        :param city: Locality or city name.
        :param state: State abbreviation.
        :param zip_code: Postal code.
        :return: Combined address string, or None when no components exist.
        """
        locality = None

        if city and state and zip_code:
            locality = f"{city}, {state} {zip_code}"
        elif city and state:
            locality = f"{city}, {state}"
        else:
            locality = city or state or zip_code

        parts = [
            item
            for item in (address, locality)
            if item
        ]

        return ", ".join(parts) if parts else None