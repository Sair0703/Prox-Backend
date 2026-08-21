from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)


RETAILER = "The GIANT Company"
RETAILER_KEY = "giant"

RADIUS_MILES = 30
FETCH_CAP = 100

MIN_DELAY_SECONDS = 2.0
MAX_DELAY_SECONDS = 5.0

MAX_RETRIES = 3
BACKOFFS = (5.0, 10.0, 20.0)
MAX_CONSECUTIVE_HARD_FAILURES = 3


API_CONFIGS: dict[str, dict[str, Any]] = {
    "GNTC": {
        "homepage_url": "https://giantfoodstores.com/",
        "api_base_url": (
            "https://giantfoodstores.com/"
            "api/v6.0/serviceLocations"
        ),
    },
    "MRTN": {
        "homepage_url": "https://martinsfoods.com/",
        "api_base_url": (
            "https://martinsfoods.com/"
            "api/v5.0/serviceLocations"
        ),
    },
}


REGIONAL_ZIP_SEEDS: tuple[str, ...] = (
    # Pennsylvania
    "19104", "19103", "19128", "19460", "17601", "17101",
    "17013", "18101", "18301", "18503", "18701", "16801",
    "15501", "17201", "17401",

    # Maryland / MARTIN'S
    "21740", "21701", "21502", "21074", "20850",

    # Virginia / diagnostic coverage
    "22601", "20110", "24401", "24112",

    # West Virginia
    "25401", "26726", "26003", "26505", "26301",
)


@dataclass(slots=True, frozen=True)
class GeographicSeed:
    """Represent GeographicSeed used by the acquisition workflow."""
    zip_code: str
    label: str


@dataclass(slots=True)
class SeedResult:
    """Represent SeedResult used by the acquisition workflow."""
    opco: str
    seed: GeographicSeed
    raw_records: list[dict[str, Any]]
    eligible_records: list[dict[str, Any]]
    attempts: int
    status: str
    saturated: bool = False
    error: str | None = None


class GiantCompanyAcquisitionStrategy:
    """
    The GIANT Company dual-banner acquisition.

    GNTC:
      giantfoodstores.com/api/v6.0/serviceLocations

    MRTN:
      martinsfoods.com/api/v5.0/serviceLocations

    Every geographic seed is queried against BOTH opcos.

    Each opco uses a page navigated to its own homepage so that the
    API fetch is same-origin and can reuse that site's browser session.
    """

    def __init__(
        self,
        *,
        seeds: tuple[str, ...] = REGIONAL_ZIP_SEEDS,
        min_delay: float = MIN_DELAY_SECONDS,
        max_delay: float = MAX_DELAY_SECONDS,
        cdp_url: str | None = None,
        persistent_profile_dir: str = ".giant_company_chrome_profile",
    ) -> None:
        """Initialize acquisition configuration and run state."""
        self.seeds = tuple(
            GeographicSeed(
                zip_code=zip_code,
                label=f"ZIP {zip_code}",
            )
            for zip_code in seeds
        )

        self.min_delay = max(0.0, min_delay)
        self.max_delay = max(
            self.min_delay,
            max_delay,
        )

        self.cdp_url = cdp_url
        self.persistent_profile_dir = (
            persistent_profile_dir
        )

        self.failed_seeds: list[dict[str, Any]] = []
        self.empty_seeds: list[dict[str, Any]] = []
        self.saturated_seeds: list[dict[str, Any]] = []
        self.excluded_records: list[dict[str, Any]] = []

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._pages: dict[str, Page] = {}
        self._owns_browser = False
        self._hard_failure_streak = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def acquire(
        self,
        *,
        seed_limit: int | None = None,
    ) -> dict[str, Any]:
        """Run the complete store location acquisition workflow."""
        seeds = list(self.seeds)

        if seed_limit is not None:
            seeds = seeds[: max(0, seed_limit)]

        await self._start_browser()

        raw_api_records: list[dict[str, Any]] = []
        eligible_raw_records: list[dict[str, Any]] = []

        successful_queries = 0
        empty_queries = 0
        saturated_queries = 0
        stopped_early = False

        total_queries = len(seeds) * len(API_CONFIGS)
        query_index = 0

        try:
            for seed in seeds:
                for opco in API_CONFIGS:
                    query_index += 1

                    if query_index > 1:
                        await self._sleep_random()

                    result = await self._acquire_seed(
                        opco,
                        seed,
                    )

                    raw_api_records.extend(
                        result.raw_records
                    )
                    eligible_raw_records.extend(
                        result.eligible_records
                    )

                    if result.status == "success":
                        self._hard_failure_streak = 0
                        successful_queries += 1

                        if not result.eligible_records:
                            empty_queries += 1
                            self.empty_seeds.append(
                                {
                                    "opco": opco,
                                    "zip_code": seed.zip_code,
                                    "label": seed.label,
                                }
                            )

                        if result.saturated:
                            saturated_queries += 1
                            self.saturated_seeds.append(
                                {
                                    "opco": opco,
                                    "zip_code": seed.zip_code,
                                    "label": seed.label,
                                    "raw_record_count": len(
                                        result.raw_records
                                    ),
                                    "fetch_cap": FETCH_CAP,
                                }
                            )
                    else:
                        self._hard_failure_streak += 1

                        self.failed_seeds.append(
                            {
                                "opco": opco,
                                "zip_code": seed.zip_code,
                                "label": seed.label,
                                "attempts": result.attempts,
                                "error": result.error,
                            }
                        )

                        if (
                            self._hard_failure_streak
                            >= MAX_CONSECUTIVE_HARD_FAILURES
                        ):
                            stopped_early = True
                            print(
                                "Stopping early: "
                                f"{self._hard_failure_streak} consecutive "
                                "hard failures."
                            )
                            break

                    print(
                        f"[{query_index}/{total_queries}] "
                        f"opco={opco} "
                        f"seed={seed.zip_code} "
                        f"raw={len(result.raw_records)} "
                        f"eligible={len(result.eligible_records)} "
                        f"total_eligible={len(eligible_raw_records)} "
                        f"failed={len(self.failed_seeds)} "
                        f"saturated={len(self.saturated_seeds)}"
                    )

                if stopped_early:
                    break

            records = self._merge_records(
                eligible_raw_records
            )

            validation = self._validate(
                seeds=seeds,
                successful_queries=successful_queries,
                empty_queries=empty_queries,
                saturated_queries=saturated_queries,
                raw_api_records=raw_api_records,
                eligible_raw_records=eligible_raw_records,
                records=records,
                stopped_early=stopped_early,
            )

            return {
                "retailer": RETAILER,
                "retailer_key": RETAILER_KEY,
                "source_type": "json",
                "records": records,
                "validation": validation,
                "failed_seeds": self.failed_seeds,
                "empty_seeds": self.empty_seeds,
                "saturated_seeds": self.saturated_seeds,
                "excluded_records": self.excluded_records,
                "seed_definitions": [
                    {
                        "zip_code": seed.zip_code,
                        "label": seed.label,
                    }
                    for seed in seeds
                ],
                "notes": self._notes(),
            }
        finally:
            await self.close()

    async def close(self) -> None:
        """Close browser resources owned by the acquisition strategy."""
        for page in list(
            self._pages.values()
        ):
            try:
                await page.close()
            except Exception:
                pass

        self._pages.clear()

        if (
            self._context is not None
            and self._owns_browser
        ):
            try:
                await self._context.close()
            except Exception:
                pass

        if (
            self._browser is not None
            and self._owns_browser
        ):
            try:
                await self._browser.close()
            except Exception:
                pass

        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass

        self._context = None
        self._browser = None
        self._playwright = None

    # ------------------------------------------------------------------
    # Browser
    # ------------------------------------------------------------------

    async def _start_browser(self) -> None:
        """Start browser."""
        self._playwright = await async_playwright().start()

        if self.cdp_url:
            try:
                self._browser = (
                    await self._playwright.chromium.connect_over_cdp(
                        self.cdp_url
                    )
                )

                contexts = self._browser.contexts
                if not contexts:
                    raise RuntimeError(
                        "CDP Chrome has no browser contexts."
                    )

                self._context = contexts[0]
                self._owns_browser = False

                existing_pages = list(
                    self._context.pages
                )

                for opco, config in API_CONFIGS.items():
                    target_host = (
                        "giantfoodstores.com"
                        if opco == "GNTC"
                        else "martinsfoods.com"
                    )

                    page = next(
                        (
                            item
                            for item in existing_pages
                            if target_host in item.url
                        ),
                        None,
                    )

                    if page is None:
                        page = await self._context.new_page()

                    self._pages[opco] = page

                    await self._navigate_homepage(
                        opco,
                        page,
                    )

                return

            except Exception as exc:
                print(
                    "CDP attach failed; falling back to dedicated "
                    f"Chrome profile: {exc!r}"
                )

        self._context = (
            await self._playwright.chromium
            .launch_persistent_context(
                self.persistent_profile_dir,
                channel="chrome",
                headless=False,
                viewport={
                    "width": 1365,
                    "height": 900,
                },
                locale="en-US",
                timezone_id="America/Los_Angeles",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/151.0.0.0 Safari/537.36"
                ),
                args=[
                    "--disable-blink-features=AutomationControlled",
                ],
            )
        )

        self._owns_browser = True

        for opco, config in API_CONFIGS.items():
            page = await self._context.new_page()
            self._pages[opco] = page

            await self._navigate_homepage(
                opco,
                page,
            )

    async def _navigate_homepage(
        self,
        opco: str,
        page: Page,
    ) -> None:
        """Navigate to homepage."""
        homepage_url = API_CONFIGS[
            opco
        ]["homepage_url"]

        try:
            response = await page.goto(
                homepage_url,
                wait_until="domcontentloaded",
                timeout=30_000,
            )

            print(
                f"{opco} homepage status: "
                f"{response.status if response else 'none'}"
            )

        except Exception as exc:
            print(
                f"{opco} homepage navigation warning: "
                f"{exc!r}"
            )

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    async def _acquire_seed(
        self,
        opco: str,
        seed: GeographicSeed,
    ) -> SeedResult:
        """Handle acquire seed."""
        page = self._pages.get(opco)

        if page is None:
            return SeedResult(
                opco=opco,
                seed=seed,
                raw_records=[],
                eligible_records=[],
                attempts=0,
                status="failed",
                error=(
                    f"No browser page initialized for opco={opco}"
                ),
            )

        config = API_CONFIGS[opco]

        params = {
            "customerType": "C",
            "opco": opco,
            "radius": str(RADIUS_MILES),
            "serviceType": "B",
            "zip": seed.zip_code,
        }

        query_string = "&".join(
            f"{key}={value}"
            for key, value in params.items()
        )

        url = (
            f"{config['api_base_url']}"
            f"?{query_string}"
        )

        last_error: str | None = None

        for attempt in range(
            1,
            MAX_RETRIES + 1,
        ):
            try:
                response_payload = await page.evaluate(
                    """
                    async ({ url }) => {
                        const response = await fetch(url, {
                            method: "GET",
                            credentials: "include",
                            headers: {
                                "Accept": "application/json, text/plain, */*"
                            }
                        });

                        const text = await response.text();

                        return {
                            status: response.status,
                            contentType:
                                response.headers.get("content-type") || "",
                            text
                        };
                    }
                    """,
                    {
                        "url": url,
                    },
                )

                status = int(
                    response_payload[
                        "status"
                    ]
                )
                content_type = str(
                    response_payload[
                        "contentType"
                    ]
                )
                text = str(
                    response_payload["text"]
                )

                if status != 200:
                    raise RuntimeError(
                        (
                            f"HTTP {status}; "
                            f"content_type={content_type!r}; "
                            f"body={text[:300]!r}"
                        )
                    )

                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        (
                            "Non-JSON response; "
                            f"content_type={content_type!r}; "
                            f"body={text[:300]!r}"
                        )
                    ) from exc

                response_block = payload.get(
                    "response",
                    {},
                )

                locations = response_block.get(
                    "locations",
                    [],
                )

                if not isinstance(
                    locations,
                    list,
                ):
                    raise RuntimeError(
                        "Unexpected API schema: "
                        "response.locations is not a list."
                    )

                raw_records = [
                    item
                    for item in locations
                    if isinstance(
                        item,
                        dict,
                    )
                ]

                eligible_records: list[
                    dict[str, Any]
                ] = []

                for item in raw_records:
                    location = item.get(
                        "location"
                    )

                    if not isinstance(
                        location,
                        dict,
                    ):
                        continue

                    normalized = (
                        self._normalize_location(
                            opco,
                            location,
                            seed,
                            distance=item.get(
                                "distance"
                            ),
                        )
                    )

                    if normalized is not None:
                        eligible_records.append(
                            normalized
                        )

                query_block = response_block.get(
                    "query",
                    {},
                )

                try:
                    fetch_size = int(
                        query_block.get(
                            "fetchSize",
                            FETCH_CAP,
                        )
                    )
                except (
                    TypeError,
                    ValueError,
                ):
                    fetch_size = FETCH_CAP

                saturated = (
                    len(raw_records)
                    >= fetch_size
                )

                return SeedResult(
                    opco=opco,
                    seed=seed,
                    raw_records=raw_records,
                    eligible_records=eligible_records,
                    attempts=attempt,
                    status="success",
                    saturated=saturated,
                )

            except Exception as exc:
                last_error = repr(exc)

                if attempt == MAX_RETRIES:
                    break

                await asyncio.sleep(
                    BACKOFFS[
                        min(
                            attempt - 1,
                            len(BACKOFFS) - 1,
                        )
                    ]
                )

        return SeedResult(
            opco=opco,
            seed=seed,
            raw_records=[],
            eligible_records=[],
            attempts=MAX_RETRIES,
            status="failed",
            error=last_error,
        )

    # ------------------------------------------------------------------
    # Normalize
    # ------------------------------------------------------------------

    def _normalize_location(
        self,
        opco: str,
        location: dict[str, Any],
        seed: GeographicSeed,
        *,
        distance: Any,
    ) -> dict[str, Any] | None:
        """Normalize location."""
        if location.get("opco") != opco:
            self.excluded_records.append(
                {
                    "reason": "wrong_opco",
                    "opco": opco,
                    "seed_zip": seed.zip_code,
                    "location_id": location.get(
                        "id"
                    ),
                    "location_number": location.get(
                        "locationNumber"
                    ),
                }
            )
            return None

        if not bool(
            location.get("active")
        ):
            self.excluded_records.append(
                {
                    "reason": "inactive",
                    "opco": opco,
                    "seed_zip": seed.zip_code,
                    "location_id": location.get(
                        "id"
                    ),
                    "location_number": location.get(
                        "locationNumber"
                    ),
                }
            )
            return None

        if not bool(
            location.get("webActive")
        ):
            self.excluded_records.append(
                {
                    "reason": "web_inactive",
                    "opco": opco,
                    "seed_zip": seed.zip_code,
                    "location_id": location.get(
                        "id"
                    ),
                    "location_number": location.get(
                        "locationNumber"
                    ),
                }
            )
            return None

        if not bool(
            location.get("physicalGroceryStore")
        ):
            self.excluded_records.append(
                {
                    "reason": "non_physical_grocery_store",
                    "opco": opco,
                    "seed_zip": seed.zip_code,
                    "location_id": location.get(
                        "id"
                    ),
                    "location_number": location.get(
                        "locationNumber"
                    ),
                }
            )
            return None

        if (
            location.get(
                "storeStatusCode"
            )
            != "ACTIVE"
        ):
            self.excluded_records.append(
                {
                    "reason": "store_not_active",
                    "opco": opco,
                    "seed_zip": seed.zip_code,
                    "location_id": location.get(
                        "id"
                    ),
                    "location_number": location.get(
                        "locationNumber"
                    ),
                }
            )
            return None

        location_number = self._clean(
            location.get(
                "locationNumber"
            )
        )

        if not location_number:
            self.excluded_records.append(
                {
                    "reason": "missing_location_number",
                    "opco": opco,
                    "seed_zip": seed.zip_code,
                    "location_id": location.get(
                        "id"
                    ),
                }
            )
            return None

        coordinates = location.get(
            "location"
        )

        longitude = None
        latitude = None

        if (
            isinstance(
                coordinates,
                (list, tuple),
            )
            and len(coordinates) >= 2
        ):
            longitude = self._float_or_none(
                coordinates[0]
            )
            latitude = self._float_or_none(
                coordinates[1]
            )

        address = self._clean(
            location.get(
                "address"
            )
        )
        address2 = self._clean(
            location.get(
                "address2"
            )
        )
        city = self._clean(
            location.get(
                "city"
            )
        )
        state = self._clean(
            location.get(
                "state"
            )
        )
        zip_code = self._clean(
            location.get(
                "zip"
            )
        )

        return {
            "retailer": RETAILER,
            "retailer_key": RETAILER_KEY,
            "store_name": self._clean(
                location.get(
                    "name"
                )
            ),
            "opco": opco,
            "retailer_store_id": location_number,
            "store_number": location_number,
            "backend_location_id": self._clean(
                location.get(
                    "id"
                )
            ),
            "address": address,
            "address2": address2,
            "city": city,
            "state": state,
            "zip_code": zip_code,
            "full_address": self._build_full_address(
                address=address,
                address2=address2,
                city=city,
                state=state,
                zip_code=zip_code,
            ),
            "phone": self._clean(
                location.get(
                    "phoneNumber"
                )
            ),
            "longitude": longitude,
            "latitude": latitude,
            "ecomm_store_id": location.get(
                "ecommStoreId"
            ),
            "pickup_location_id": self._clean(
                location.get(
                    "pickupLocationId"
                )
            ),
            "service_type": self._clean(
                location.get(
                    "serviceType"
                )
            ),
            "pickup_point_type": self._clean(
                location.get(
                    "pickupPointType"
                )
            ),
            "store_id": self._clean(
                location.get(
                    "storeId"
                )
            ),
            "site": self._clean(
                location.get(
                    "site"
                )
            ),
            "source": (
                f"{opco} official "
                "serviceLocations API"
            ),
            "source_type": "json",
            "query_zip": seed.zip_code,
            "query_distance_miles": RADIUS_MILES,
            "distance_from_query": (
                self._float_or_none(
                    distance
                )
            ),
        }

    # ------------------------------------------------------------------
    # Merge
    # ------------------------------------------------------------------

    def _merge_records(
        self,
        records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Merge records."""
        merged: dict[
            tuple[str, str],
            dict[str, Any],
        ] = {}

        for record in records:
            opco = record.get(
                "opco"
            )
            store_id = record.get(
                "retailer_store_id"
            )

            if not opco or not store_id:
                continue

            key = (
                str(opco),
                str(store_id),
            )

            existing = merged.get(
                key
            )

            if existing is None:
                merged[key] = dict(record)
            else:
                merged[key] = self._merge_two(
                    existing,
                    record,
                )

        result = list(
            merged.values()
        )

        result.sort(
            key=lambda row: (
                row.get("opco") or "",
                row.get("state") or "",
                row.get("city") or "",
                row.get("store_name") or "",
                row.get(
                    "retailer_store_id"
                ) or "",
            )
        )

        return result

    @staticmethod
    def _merge_two(
        first: dict[str, Any],
        second: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge two."""
        merged = dict(first)

        for key, value in second.items():
            if (
                merged.get(key)
                in (None, "")
                and value
                not in (None, "")
            ):
                merged[key] = value

        first_backend_id = first.get(
            "backend_location_id"
        )
        second_backend_id = second.get(
            "backend_location_id"
        )

        if (
            first_backend_id
            and second_backend_id
            and first_backend_id
            != second_backend_id
        ):
            ids = {
                str(first_backend_id),
                str(second_backend_id),
            }

            merged[
                "backend_location_ids_all"
            ] = "|".join(
                sorted(ids)
            )

        first_query = first.get(
            "query_zip"
        )
        second_query = second.get(
            "query_zip"
        )

        if (
            first_query
            and second_query
            and first_query != second_query
        ):
            values = set()

            existing = merged.get(
                "query_zip_all"
            )

            if existing:
                values.update(
                    str(existing).split("|")
                )

            values.add(
                str(first_query)
            )
            values.add(
                str(second_query)
            )

            merged[
                "query_zip_all"
            ] = "|".join(
                sorted(values)
            )

        return merged

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate(
        self,
        *,
        seeds: list[GeographicSeed],
        successful_queries: int,
        empty_queries: int,
        saturated_queries: int,
        raw_api_records: list[dict[str, Any]],
        eligible_raw_records: list[dict[str, Any]],
        records: list[dict[str, Any]],
        stopped_early: bool,
    ) -> dict[str, Any]:
        """Handle validate."""
        state_counts: dict[str, int] = {}
        opco_counts: dict[str, int] = {}
        store_name_counts: dict[str, int] = {}

        for record in records:
            state = (
                record.get("state")
                or "UNKNOWN"
            )
            state_counts[state] = (
                state_counts.get(
                    state,
                    0,
                )
                + 1
            )

            opco = (
                record.get("opco")
                or "UNKNOWN"
            )
            opco_counts[opco] = (
                opco_counts.get(
                    opco,
                    0,
                )
                + 1
            )

            store_name = (
                record.get(
                    "store_name"
                )
                or "UNKNOWN"
            )
            store_name_counts[
                store_name
            ] = (
                store_name_counts.get(
                    store_name,
                    0,
                )
                + 1
            )

        missing_ids = sum(
            not record.get(
                "retailer_store_id"
            )
            for record in records
        )

        missing_addresses = sum(
            not record.get(
                "full_address"
            )
            for record in records
        )

        missing_phones = sum(
            not record.get(
                "phone"
            )
            for record in records
        )

        missing_coordinates = sum(
            record.get("latitude") is None
            or record.get("longitude") is None
            for record in records
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
                    (
                        record.get("opco"),
                        record.get(
                            "retailer_store_id"
                        ),
                    )
                    for record in records
                    if record.get(
                        "opco"
                    )
                    and record.get(
                        "retailer_store_id"
                    )
                }
            ),
            "raw_api_records": len(
                raw_api_records
            ),
            "eligible_raw_records": len(
                eligible_raw_records
            ),
            "duplicate_records_merged": max(
                0,
                len(eligible_raw_records)
                - len(records),
            ),
            "missing_store_ids": missing_ids,
            "missing_addresses": missing_addresses,
            "missing_phones": missing_phones,
            "missing_coordinates": missing_coordinates,
            "regional_zip_seeds": len(
                seeds
            ),
            "total_queries": (
                len(seeds)
                * len(API_CONFIGS)
            ),
            "successful_seed_queries": (
                successful_queries
            ),
            "empty_seed_queries": (
                empty_queries
            ),
            "failed_seed_queries": len(
                self.failed_seeds
            ),
            "queries_hitting_fetch_cap": (
                saturated_queries
            ),
            "excluded_records": len(
                self.excluded_records
            ),
            "stopped_early": stopped_early,
            "state_counts": dict(
                sorted(
                    state_counts.items()
                )
            ),
            "opco_counts": dict(
                sorted(
                    opco_counts.items()
                )
            ),
            "store_name_counts": dict(
                sorted(
                    store_name_counts.items()
                )
            ),
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _sleep_random(self) -> None:
        """Pause for random."""
        await asyncio.sleep(
            random.uniform(
                self.min_delay,
                self.max_delay,
            )
        )

    @staticmethod
    def _clean(
        value: Any,
    ) -> str | None:
        """Handle clean."""
        if value is None:
            return None

        text = str(value).strip()
        return text or None

    @staticmethod
    def _float_or_none(
        value: Any,
    ) -> float | None:
        """Convert a value to float when possible."""
        try:
            return float(value)
        except (
            TypeError,
            ValueError,
        ):
            return None

    @staticmethod
    def _build_full_address(
        *,
        address: str | None,
        address2: str | None,
        city: str | None,
        state: str | None,
        zip_code: str | None,
    ) -> str | None:
        """Build full address."""
        street_parts = [
            value
            for value in (
                address,
                address2,
            )
            if value
        ]

        street = (
            ", ".join(street_parts)
            if street_parts
            else None
        )

        if city and state and zip_code:
            locality = (
                f"{city}, {state} {zip_code}"
            )
        elif city and state:
            locality = (
                f"{city}, {state}"
            )
        else:
            locality = (
                city
                or state
                or zip_code
            )

        parts = [
            value
            for value in (
                street,
                locality,
            )
            if value
        ]

        return (
            ", ".join(parts)
            if parts
            else None
        )

    @staticmethod
    def _notes() -> list[str]:
        """Return acquisition notes."""
        return [
            (
                "Official sources: The GIANT Company GNTC "
                "serviceLocations API and MARTIN'S MRTN "
                "serviceLocations API."
            ),
            (
                "Every geographic seed is queried against both "
                "opcos so regional overlap is not assumed."
            ),
            (
                "GNTC uses giantfoodstores.com/api/v6.0; MRTN uses "
                "martinsfoods.com/api/v5.0."
            ),
            (
                "Each opco query uses radius=30 and the API fetch cap "
                "is monitored at 100 records."
            ),
            (
                "locationNumber is the retailer store identifier; "
                "backend id is preserved separately."
            ),
            (
                "Cross-banner merge identity is (opco, locationNumber), "
                "not locationNumber alone."
            ),
            (
                "Only active, web-active, physical grocery stores with "
                "storeStatusCode=ACTIVE are retained."
            ),
            (
                "Coordinates are normalized from API "
                "location=[longitude, latitude]."
            ),
            (
                "Each opco uses a page on its own homepage origin for "
                "same-origin browser fetches and session reuse."
            ),
        ]