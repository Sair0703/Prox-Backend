# services/store_service/capabilities/store_location_acquisition/strategies/giant_eagle_acquisition_strategy.py

"""Acquisition strategy for Giant Eagle store locations."""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from typing import Any

import requests


RETAILER = "Giant Eagle"
RETAILER_KEY = "giant_eagle"

API_URL = "https://core.shop.gianteagle.com/api/v2"
ORIGIN = "https://www.gianteagle.com"
REFERER = "https://www.gianteagle.com/stores"

OPERATION_NAME = "GetStores"

# Match the current official locator request.
PAGE_SIZE = 50
STORE_BROWSING_MODES = (
    "pickup",
    "delivery",
)

MIN_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 2.5

MAX_RETRIES = 4
BACKOFFS = (2.0, 4.0, 8.0, 16.0)
MAX_CONSECUTIVE_HARD_FAILURES = 3


REGIONAL_ZIP_SEEDS: tuple[str, ...] = (
    # Ohio - dense northern / central footprint
    "44101", "44130", "44256", "44308", "44702",
    "44406", "44512", "43215", "43004", "43016",
    "43054",
    # Deliberately avoid northwest Ohio seeds such as Toledo.

    # Pennsylvania - denser western PA coverage
    "15222", "15237", "15146", "15001", "16066",
    "16101", "16501", "16509", "16301", "16601",
    "16801",

    # West Virginia
    "26003", "26505", "26501", "26301",

    # Maryland
    "21502", "21740", "21701", "21550",

    # Indiana
    "46013", "46032", "47302", "47401",
)

EXCLUDED_NAME_PATTERNS = (
    "pharmacy",
    "locker pickup",
    "free delivery",
    "order pickup",
)


@dataclass(slots=True, frozen=True)
class GeographicSeed:
    """Geographic ZIP seed used to query the Giant Eagle locator."""

    zip_code: str
    label: str


@dataclass(slots=True)
class SeedResult:
    """Result of acquiring all pages for one geographic seed."""

    seed: GeographicSeed
    records: list[dict[str, Any]]
    raw_count: int
    excluded_count: int
    pages: int
    status: str
    attempts: int
    error: str | None = None


class GiantEagleAcquisitionStrategy:
    """
    Acquires Giant Eagle stores from the official GetStores API.

    Uses regional ZIP seeds, cursor-based pagination, and Store.code
    as the retailer store identifier. Obvious non-supermarket service
    locations are excluded and retained separately for audit.
    """

    def __init__(
        self,
        *,
        seeds: tuple[str, ...] = REGIONAL_ZIP_SEEDS,
        page_size: int = PAGE_SIZE,
        min_delay: float = MIN_DELAY_SECONDS,
        max_delay: float = MAX_DELAY_SECONDS,
        session: requests.Session | None = None,
    ) -> None:
        """
        Initialize the Giant Eagle acquisition strategy.

        :param seeds: Regional ZIP seeds used to query the locator.
        :param page_size: Number of store nodes requested per page.
        :param min_delay: Minimum delay between requests.
        :param max_delay: Maximum delay between requests.
        :param session: Optional HTTP session used for API requests.
        """
        self.seeds = tuple(
            GeographicSeed(
                zip_code=zip_code,
                label=f"ZIP {zip_code}",
            )
            for zip_code in seeds
        )
        self.page_size = max(1, page_size)
        self.min_delay = max(0.0, min_delay)
        self.max_delay = max(self.min_delay, max_delay)

        self._session = (
            session
            if session is not None
            else requests.Session()
        )

        self._session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Content-Type": "application/json;charset=utf-8",
                "Origin": ORIGIN,
                "Referer": REFERER,
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/151.0.0.0 Safari/537.36"
                ),
                "x-hl-app": "grocery",
                "x-hl-client": "web",
                "x-hl-referrer": REFERER,
            }
        )

        self.failed_seeds: list[dict[str, Any]] = []
        self.empty_seeds: list[dict[str, Any]] = []
        self.excluded_records: list[dict[str, Any]] = []
        self._hard_failure_streak = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(
        self,
        *,
        seed_limit: int | None = None,
    ) -> dict[str, Any]:
        """
        Run the Giant Eagle store-location acquisition workflow.

        :param seed_limit: Optional limit on the number of ZIP seeds to process.
        :return: Acquired records, validation results, and run metadata.
        """
        seeds = list(self.seeds)

        if seed_limit is not None:
            seeds = seeds[: max(0, seed_limit)]

        raw_records: list[dict[str, Any]] = []
        successful_seeds = 0
        stopped_early = False

        print(
            f"Giant Eagle regional ZIP seeds: {len(seeds)}"
        )

        for index, seed in enumerate(
            seeds,
            start=1,
        ):
            if index > 1:
                self._sleep_random()

            result = self._acquire_seed(seed)

            raw_records.extend(result.records)

            if result.status == "success":
                self._hard_failure_streak = 0
                successful_seeds += 1

                if not result.records:
                    self.empty_seeds.append(
                        {
                            "zip_code": seed.zip_code,
                            "label": seed.label,
                        }
                    )
            else:
                self._hard_failure_streak += 1

                self.failed_seeds.append(
                    {
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
                        f"{self._hard_failure_streak} "
                        "consecutive hard failures."
                    )
                    break

            print(
                f"[{index}/{len(seeds)}] "
                f"seed={seed.zip_code} "
                f"raw={result.raw_count} "
                f"eligible={len(result.records)} "
                f"excluded={result.excluded_count} "
                f"pages={result.pages} "
                f"total_raw={len(raw_records)} "
                f"failed={len(self.failed_seeds)}"
            )

            if stopped_early:
                break

        records = self._merge_records(raw_records)

        validation = self._validate(
            seeds=seeds,
            successful_seeds=successful_seeds,
            raw_records=raw_records,
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

    # ------------------------------------------------------------------
    # Seed acquisition
    # ------------------------------------------------------------------

    def _acquire_seed(
        self,
        seed: GeographicSeed,
    ) -> SeedResult:
        """
        Acquire all cursor-paginated results for one ZIP seed.

        :param seed: Geographic ZIP seed used for the locator query.
        :return: Acquisition result containing eligible and excluded counts.
        """
        all_records: list[dict[str, Any]] = []
        cursor: str | None = None
        pages = 0
        raw_count = 0
        excluded_count = 0

        for page_number in range(
            1,
            10_001,
        ):
            result = self._request_page(
                seed=seed,
                cursor=cursor,
            )

            if result is None:
                return SeedResult(
                    seed=seed,
                    records=all_records,
                    raw_count=raw_count,
                    excluded_count=excluded_count,
                    pages=pages,
                    status="failed",
                    attempts=MAX_RETRIES,
                    error=(
                        "Failed to acquire page "
                        f"{page_number}."
                    ),
                )

            page_records, page_info, page_raw_count, page_excluded = result

            pages += 1
            raw_count += page_raw_count
            excluded_count += page_excluded
            all_records.extend(page_records)

            has_next_page = bool(
                page_info.get("hasNextPage")
            )
            end_cursor = page_info.get("endCursor")

            if not has_next_page:
                break

            if not end_cursor:
                return SeedResult(
                    seed=seed,
                    records=all_records,
                    raw_count=raw_count,
                    excluded_count=excluded_count,
                    pages=pages,
                    status="failed",
                    attempts=1,
                    error=(
                        "hasNextPage=True but "
                        "endCursor is empty."
                    ),
                )

            cursor = str(end_cursor)

            if pages > 1:
                self._sleep_random()

        return SeedResult(
            seed=seed,
            records=all_records,
            raw_count=raw_count,
            excluded_count=excluded_count,
            pages=pages,
            status="success",
            attempts=1,
        )

    def _request_page(
        self,
        *,
        seed: GeographicSeed,
        cursor: str | None,
    ) -> tuple[
        list[dict[str, Any]],
        dict[str, Any],
        int,
        int,
    ] | None:
        """
        Request and parse one GetStores API page.

        :param seed: Geographic ZIP seed used for the request.
        :param cursor: Pagination cursor from the previous page.
        :return: Records, page metadata, raw count, and excluded count,
            or None when all retry attempts fail.
        """
        payload = {
            "operationName": OPERATION_NAME,
            "variables": {
                "count": self.page_size,
                "storeBrowsingModes": list(
                    STORE_BROWSING_MODES
                ),
                "zipcode": seed.zip_code,
                "cursor": cursor,
            },
            "query": self._query(),
        }

        for attempt in range(
            1,
            MAX_RETRIES + 1,
        ):
            try:
                response = self._session.post(
                    API_URL,
                    json=payload,
                    timeout=30,
                )

                if response.status_code != 200:
                    raise RuntimeError(
                        (
                            f"HTTP {response.status_code}: "
                            f"{response.text[:300]!r}"
                        )
                    )

                data = response.json()

                if data.get("errors"):
                    raise RuntimeError(
                        json.dumps(
                            data["errors"],
                            ensure_ascii=False,
                        )
                    )

                stores = (
                    data.get("data", {})
                    .get("stores", {})
                )

                edges = stores.get(
                    "edges",
                    []
                )
                page_info = stores.get(
                    "pageInfo",
                    {}
                )

                raw_count = 0
                excluded_count = 0
                records: list[dict[str, Any]] = []

                for edge in edges:
                    node = edge.get(
                        "node",
                        {}
                    )

                    if not isinstance(
                        node,
                        dict,
                    ):
                        continue

                    raw_count += 1

                    normalized = self._normalize_store(
                        node,
                        seed,
                    )

                    if normalized is None:
                        excluded_count += 1
                        continue

                    records.append(
                        normalized
                    )

                return (
                    records,
                    page_info,
                    raw_count,
                    excluded_count,
                )

            except Exception as exc:
                if attempt == MAX_RETRIES:
                    print(
                        "Request failed: "
                        f"seed={seed.zip_code} "
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

    # ------------------------------------------------------------------
    # Normalize / filtering
    # ------------------------------------------------------------------

    def _normalize_store(
        self,
        node: dict[str, Any],
        seed: GeographicSeed,
    ) -> dict[str, Any] | None:
        """
        Normalize an API store node and apply location filtering.

        :param node: Raw store node returned by the GetStores API.
        :param seed: ZIP seed that produced the store result.
        :return: Normalized store record, or None when excluded.
        """
        code = node.get("code")

        if code in (None, ""):
            self._record_excluded(
                node=node,
                seed=seed,
                reason="missing_store_code",
            )
            return None

        name = str(
            node.get("name")
            or ""
        ).strip()

        normalized_name = name.lower()

        for pattern in EXCLUDED_NAME_PATTERNS:
            if pattern in normalized_name:
                self._record_excluded(
                    node=node,
                    seed=seed,
                    reason=f"name_pattern:{pattern}",
                )
                return None

        address = node.get(
            "address"
        ) or {}

        location = address.get(
            "location"
        ) or {}

        services = (
            node.get(
                "availableServices"
            )
            or {}
        )

        return {
            "retailer": RETAILER,
            "retailer_key": RETAILER_KEY,
            "store_name": (
                name
                or None
            ),
            "retailer_store_id": str(
                code
            ),
            "store_number": str(
                code
            ),
            "store_slug": (
                str(
                    node.get(
                        "slug"
                    )
                    or ""
                ).strip()
                or None
            ),
            "address": self._clean(
                address.get("street")
            ),
            "address2": self._clean(
                address.get("street2")
            ),
            "city": self._clean(
                address.get("city")
            ),
            "state": self._clean(
                address.get("state")
            ),
            "zip_code": self._clean(
                address.get("zipcode")
            ),
            "full_address": self._clean(
                address.get("fullAddress")
            ),
            "latitude": self._float_or_none(
                location.get("lat")
            ),
            "longitude": self._float_or_none(
                location.get("lng")
            ),
            "pickup_available": bool(
                services.get("pickup")
            ),
            "delivery_available": bool(
                services.get("delivery")
            ),
            "instore_available": bool(
                services.get("instore")
            ),
            "scan_pay_go_legacy": bool(
                services.get("scanPayGoLegacy")
            ),
            "source": (
                "Giant Eagle official "
                "GetStores API"
            ),
            "source_type": "json",
            "query_zip": seed.zip_code,
        }

    def _record_excluded(
        self,
        *,
        node: dict[str, Any],
        seed: GeographicSeed,
        reason: str,
    ) -> None:
        """
        Record an excluded API node for acquisition auditing.

        :param node: Raw store node excluded from the final dataset.
        :param seed: ZIP seed that produced the node.
        :param reason: Reason the node was excluded.
        """
        address = node.get(
            "address"
        ) or {}

        self.excluded_records.append(
            {
                "reason": reason,
                "query_zip": seed.zip_code,
                "store_name": str(
                    node.get("name")
                    or ""
                ).strip(),
                "store_code": str(
                    node.get("code")
                    or ""
                ).strip(),
                "slug": str(
                    node.get("slug")
                    or ""
                ).strip(),
                "state": self._clean(
                    address.get("state")
                ),
                "city": self._clean(
                    address.get("city")
                ),
                "zip_code": self._clean(
                    address.get("zipcode")
                ),
            }
        )

    # ------------------------------------------------------------------
    # Merge
    # ------------------------------------------------------------------

    @staticmethod
    def _merge_records(
        raw_records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Merge overlapping results by state and retailer store ID.

        :param raw_records: Store records collected across all ZIP seeds.
        :return: Deduplicated and sorted store records.
        """
        merged: dict[
            tuple[str, str],
            dict[str, Any],
        ] = {}

        for record in raw_records:
            state = (
                str(
                    record.get(
                        "state"
                    )
                    or ""
                )
                .strip()
                .upper()
            )
            store_id = record.get(
                "retailer_store_id"
            )

            if not store_id:
                continue

            key = (
                state,
                str(store_id),
            )

            existing = merged.get(key)

            if existing is None:
                merged[key] = dict(record)
            else:
                merged[key] = (
                    GiantEagleAcquisitionStrategy
                    ._merge_two(
                        existing,
                        record,
                    )
                )

        records = list(
            merged.values()
        )

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
        Merge non-empty values from two overlapping store records.

        :param first: Existing normalized store record.
        :param second: Newly acquired overlapping store record.
        :return: Combined store record.
        """
        merged = dict(first)

        for key, value in second.items():
            if (
                merged.get(key)
                in (None, "")
                and value
                not in (None, "")
            ):
                merged[key] = value

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

    @staticmethod
    def _validate(
        *,
        seeds: list[GeographicSeed],
        successful_seeds: int,
        raw_records: list[dict[str, Any]],
        records: list[dict[str, Any]],
        stopped_early: bool,
    ) -> dict[str, Any]:
        """
        Validate acquisition coverage and store-record completeness.

        :param seeds: ZIP seeds included in the acquisition run.
        :param successful_seeds: Number of successfully acquired seeds.
        :param raw_records: Records collected before deduplication.
        :param records: Final deduplicated store records.
        :param stopped_early: Whether fail-fast terminated the run.
        :return: Validation and coverage metrics.
        """
        state_counts: dict[str, int] = {}
        store_name_counts: dict[str, int] = {}

        for record in records:
            state = (
                record.get(
                    "state"
                )
                or "UNKNOWN"
            )
            state_counts[state] = (
                state_counts.get(
                    state,
                    0,
                )
                + 1
            )

            name = (
                record.get(
                    "store_name"
                )
                or "UNKNOWN"
            )
            store_name_counts[name] = (
                store_name_counts.get(
                    name,
                    0,
                )
                + 1
            )

        return {
            "valid": (
                bool(records)
                and not stopped_early
            ),
            "total_records": len(
                records
            ),
            "unique_store_ids": len(
                {
                    (
                        row.get(
                            "state"
                        ),
                        row.get(
                            "retailer_store_id"
                        ),
                    )
                    for row in records
                    if row.get(
                        "retailer_store_id"
                    )
                }
            ),
            "raw_api_records": len(
                raw_records
            ),
            "duplicate_records_merged": max(
                0,
                len(raw_records)
                - len(records),
            ),
            "missing_store_ids": sum(
                not row.get(
                    "retailer_store_id"
                )
                for row in records
            ),
            "missing_addresses": sum(
                not row.get(
                    "full_address"
                )
                for row in records
            ),
            "missing_coordinates": sum(
                row.get(
                    "latitude"
                ) is None
                or row.get(
                    "longitude"
                ) is None
                for row in records
            ),
            "regional_zip_seeds": len(
                seeds
            ),
            "successful_seed_queries": (
                successful_seeds
            ),
            "empty_seed_queries": 0,
            "failed_seed_queries": 0,
            "stopped_early": stopped_early,
            "state_counts": dict(
                sorted(
                    state_counts.items()
                )
            ),
            "store_name_counts": dict(
                sorted(
                    store_name_counts.items()
                )
            ),
            "excluded_records": 0,
        }

    @staticmethod
    def _notes() -> list[str]:
        """Return notes describing the Giant Eagle acquisition approach."""
        return [
            (
                "Official source: Giant Eagle GetStores API "
                "at core.shop.gianteagle.com/api/v2."
            ),
            (
                "The request matches the current official locator "
                "browsing modes: pickup + delivery."
            ),
            (
                "Regional ZIP seeds cover Ohio, Pennsylvania, "
                "West Virginia, Maryland and Indiana."
            ),
            (
                "Each ZIP is fully paginated using pageInfo.endCursor "
                "until hasNextPage is false."
            ),
            (
                "Store.code is used as retailer_store_id/store_number."
            ),
            (
                "Coordinates are taken directly from address.location."
            ),
            (
                "Obvious non-supermarket service locations are excluded "
                "and retained in the exclusion audit."
            ),
            (
                "Overlapping ZIP results are merged by "
                "(state, retailer_store_id)."
            ),
        ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean(
        value: Any,
    ) -> str | None:
        """
        Normalize a value to trimmed text.

        :param value: Value to normalize.
        :return: Trimmed text, or None for empty values.
        """
        if value is None:
            return None

        text = str(value).strip()
        return text or None

    @staticmethod
    def _float_or_none(
        value: Any,
    ) -> float | None:
        """
        Convert a value to float when possible.

        :param value: Value to convert.
        :return: Parsed float, or None when conversion fails.
        """
        try:
            return float(value)
        except (
            TypeError,
            ValueError,
        ):
            return None

    @staticmethod
    def _query() -> str:
        """Return the GraphQL GetStores query used by the official locator."""
        return (
            "query GetStores("
            "$zipcode: ZipCode!, "
            "$storeBrowsingModes: [BrowsingMode!], "
            "$count: Int, "
            "$cursor: String"
            ") { "
            "stores("
            " zipcode: $zipcode"
            " storeBrowsingModes: $storeBrowsingModes"
            " first: $count"
            " after: $cursor"
            ") { "
            "edges { "
            "cursor "
            "node { "
            "address { "
            "fullAddress "
            "city "
            "state "
            "street "
            "street2 "
            "zipcode "
            "location { lat lng __typename } "
            "__typename "
            "} "
            "availableServices { "
            "pickup "
            "delivery "
            "instore "
            "scanPayGoLegacy "
            "__typename "
            "} "
            "code "
            "name "
            "slug "
            "__typename "
            "} "
            "__typename "
            "} "
            "pageInfo { "
            "endCursor "
            "hasNextPage "
            "__typename "
            "} "
            "__typename "
            "} "
            "}"
        )

    def _sleep_random(self) -> None:
        """Sleep for a random interval within the configured delay range."""
        time.sleep(
            random.uniform(
                self.min_delay,
                self.max_delay,
            )
        )