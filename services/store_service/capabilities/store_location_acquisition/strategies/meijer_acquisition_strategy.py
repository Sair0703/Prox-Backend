from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
import time

import requests
from tqdm import tqdm


MEIJER_SEARCH_URL = (
    "https://www.meijer.com/bin/meijer/store/search"
)

RETAILER = "Meijer"
RETAILER_KEY = "meijer"

RADIUS_MILES = 1000
WORKERS = 8
REQUEST_TIMEOUT = 30
MAX_RETRIES = 4
BACKOFFS = (1.0, 2.0, 4.0, 8.0)

# Meijer retail footprint states represented by the acquisition scope.
MEIJER_STATES = {
    "Michigan",
    "Illinois",
    "Indiana",
    "Ohio",
    "Wisconsin",
    "Kentucky",
}

# Geographic seeds are intentionally kept in code so the acquisition
# does not depend on a third-party county/ZIP service at runtime.
#
# These are coverage seeds, not authoritative store data. Store authority
# comes only from Meijer's official search API.
MEIJER_LOCATION_SEEDS: list[str] = [
    # ================================================================
    # Michigan
    # ================================================================
    "Detroit, Michigan",
    "Grand Rapids, Michigan",
    "Lansing, Michigan",
    "Ann Arbor, Michigan",
    "Flint, Michigan",
    "Kalamazoo, Michigan",
    "Saginaw, Michigan",
    "Muskegon, Michigan",
    "Traverse City, Michigan",
    "Battle Creek, Michigan",
    "Jackson, Michigan",
    "Bay City, Michigan",
    "Midland, Michigan",
    "Mount Pleasant, Michigan",
    "Marquette, Michigan",
    "Monroe, Michigan",
    "Port Huron, Michigan",
    "Holland, Michigan",
    "Norton Shores, Michigan",
    "Adrian, Michigan",

    "Escanaba, Michigan",
    "Iron Mountain, Michigan",
    "Ironwood, Michigan",
    "Houghton, Michigan",
    "Sault Ste. Marie, Michigan",
    "Petoskey, Michigan",
    "Gaylord, Michigan",
    "Alpena, Michigan",
    "Cadillac, Michigan",
    "Big Rapids, Michigan",
    "Manistee, Michigan",
    "Ludington, Michigan",
    "Benton Harbor, Michigan",
    "St. Joseph, Michigan",
    "Coldwater, Michigan",
    "Hillsdale, Michigan",
    "Ionia, Michigan",
    "Owosso, Michigan",
    "Alma, Michigan",
    "Clare, Michigan",
    "Grayling, Michigan",
    "Cheboygan, Michigan",
    "Charlevoix, Michigan",
    "Rogers City, Michigan",
    "Mackinaw City, Michigan",
    "Newberry, Michigan",
    "Munising, Michigan",
    "Manistique, Michigan",
    "Menominee, Michigan",
    "Ontonagon, Michigan",
    "Escanaba, Michigan",
    "Gladstone, Michigan",
    "St. Ignace, Michigan",

    "Grand Haven, Michigan",
    "South Haven, Michigan",
    "Allegan, Michigan",
    "Three Rivers, Michigan",
    "Sturgis, Michigan",
    "Niles, Michigan",
    "Dowagiac, Michigan",
    "Paw Paw, Michigan",
    "Marshall, Michigan",
    "Albion, Michigan",
    "Charlotte, Michigan",
    "Hastings, Michigan",
    "Greenville, Michigan",
    "Lowell, Michigan",
    "Ionia, Michigan",
    "Portland, Michigan",
    "Eaton Rapids, Michigan",

    "Howell, Michigan",
    "Brighton, Michigan",
    "Fenton, Michigan",
    "Grand Blanc, Michigan",
    "Lapeer, Michigan",
    "Davison, Michigan",
    "Burton, Michigan",
    "Clio, Michigan",
    "Owosso, Michigan",
    "Corunna, Michigan",
    "St. Johns, Michigan",

    "Ypsilanti, Michigan",
    "Canton, Michigan",
    "Livonia, Michigan",
    "Novi, Michigan",
    "Northville, Michigan",
    "Farmington Hills, Michigan",
    "Southfield, Michigan",
    "Royal Oak, Michigan",
    "Troy, Michigan",
    "Pontiac, Michigan",
    "Rochester Hills, Michigan",
    "Sterling Heights, Michigan",
    "Warren, Michigan",
    "Roseville, Michigan",
    "Clinton Township, Michigan",
    "Chesterfield, Michigan",
    "Macomb, Michigan",
    "Shelby Township, Michigan",
    "Waterford, Michigan",
    "White Lake, Michigan",
    "Commerce Township, Michigan",
    "West Bloomfield, Michigan",

    "Dearborn, Michigan",
    "Dearborn Heights, Michigan",
    "Taylor, Michigan",
    "Allen Park, Michigan",
    "Lincoln Park, Michigan",
    "Southgate, Michigan",
    "Wyandotte, Michigan",
    "Woodhaven, Michigan",
    "Brownstown, Michigan",
    "Belleville, Michigan",

    # ================================================================
    # Ohio
    # ================================================================
    "Columbus, Ohio",
    "Cleveland, Ohio",
    "Cincinnati, Ohio",
    "Toledo, Ohio",
    "Dayton, Ohio",
    "Akron, Ohio",
    "Canton, Ohio",
    "Youngstown, Ohio",
    "Mansfield, Ohio",
    "Lima, Ohio",
    "Findlay, Ohio",
    "Sandusky, Ohio",
    "Springfield, Ohio",
    "Middletown, Ohio",
    "Hamilton, Ohio",
    "Lorain, Ohio",
    "Wooster, Ohio",
    "Medina, Ohio",
    "Bowling Green, Ohio",
    "Delaware, Ohio",

    "Marion, Ohio",
    "Marysville, Ohio",
    "Lancaster, Ohio",
    "Newark, Ohio",
    "Zanesville, Ohio",
    "Chillicothe, Ohio",
    "Portsmouth, Ohio",
    "Athens, Ohio",
    "Marietta, Ohio",
    "Cambridge, Ohio",
    "Steubenville, Ohio",
    "Ashtabula, Ohio",
    "Elyria, Ohio",
    "Norwalk, Ohio",
    "Fremont, Ohio",
    "Defiance, Ohio",
    "Van Wert, Ohio",
    "Sidney, Ohio",
    "Troy, Ohio",
    "Greenville, Ohio",
    "Piqua, Ohio",
    "Xenia, Ohio",
    "Fairborn, Ohio",
    "Lebanon, Ohio",
    "Wilmington, Ohio",

    # ================================================================
    # Indiana
    # ================================================================
    "Indianapolis, Indiana",
    "Fort Wayne, Indiana",
    "South Bend, Indiana",
    "Evansville, Indiana",
    "Lafayette, Indiana",
    "Bloomington, Indiana",
    "Terre Haute, Indiana",
    "Muncie, Indiana",
    "Kokomo, Indiana",
    "Elkhart, Indiana",
    "Gary, Indiana",
    "Michigan City, Indiana",
    "Columbus, Indiana",
    "Richmond, Indiana",
    "Anderson, Indiana",

    "Goshen, Indiana",
    "Warsaw, Indiana",
    "Marion, Indiana",
    "Huntington, Indiana",
    "Auburn, Indiana",
    "Angola, Indiana",
    "Logansport, Indiana",
    "Peru, Indiana",
    "Wabash, Indiana",
    "New Castle, Indiana",
    "Shelbyville, Indiana",
    "Franklin, Indiana",
    "Greenwood, Indiana",
    "Plainfield, Indiana",
    "Avon, Indiana",
    "Brownsburg, Indiana",
    "Noblesville, Indiana",
    "Fishers, Indiana",
    "Carmel, Indiana",
    "Westfield, Indiana",
    "Valparaiso, Indiana",
    "Portage, Indiana",
    "Merrillville, Indiana",
    "Crown Point, Indiana",

    # ================================================================
    # Illinois
    # ================================================================
    "Chicago, Illinois",
    "Rockford, Illinois",
    "Peoria, Illinois",
    "Springfield, Illinois",
    "Champaign, Illinois",
    "Bloomington, Illinois",
    "Aurora, Illinois",
    "Joliet, Illinois",
    "Naperville, Illinois",
    "Elgin, Illinois",
    "Waukegan, Illinois",
    "Kankakee, Illinois",
    "DeKalb, Illinois",
    "Decatur, Illinois",
    "Normal, Illinois",

    "Bolingbrook, Illinois",
    "Plainfield, Illinois",
    "Oswego, Illinois",
    "Yorkville, Illinois",
    "St. Charles, Illinois",
    "Geneva, Illinois",
    "Batavia, Illinois",
    "Schaumburg, Illinois",
    "Hoffman Estates, Illinois",
    "Algonquin, Illinois",
    "Crystal Lake, Illinois",
    "McHenry, Illinois",
    "Gurnee, Illinois",
    "Round Lake, Illinois",
    "Mundelein, Illinois",
    "Vernon Hills, Illinois",
    "Danville, Illinois",
    "Pontiac, Illinois",

    # ================================================================
    # Wisconsin
    # ================================================================
    "Milwaukee, Wisconsin",
    "Madison, Wisconsin",
    "Green Bay, Wisconsin",
    "Appleton, Wisconsin",
    "Kenosha, Wisconsin",
    "Racine, Wisconsin",
    "Eau Claire, Wisconsin",
    "Oshkosh, Wisconsin",
    "Wausau, Wisconsin",
    "La Crosse, Wisconsin",
    "Janesville, Wisconsin",
    "Fond du Lac, Wisconsin",
    "Sheboygan, Wisconsin",
    "Manitowoc, Wisconsin",

    "West Bend, Wisconsin",
    "Grafton, Wisconsin",
    "Waukesha, Wisconsin",
    "Brookfield, Wisconsin",
    "New Berlin, Wisconsin",
    "Oak Creek, Wisconsin",
    "Franklin, Wisconsin",
    "Pleasant Prairie, Wisconsin",
    "Beloit, Wisconsin",
    "Watertown, Wisconsin",
    "Beaver Dam, Wisconsin",
    "Stevens Point, Wisconsin",
    "Wisconsin Rapids, Wisconsin",
    "Marshfield, Wisconsin",
    "Greenfield, Wisconsin",
    "Sun Prairie, Wisconsin",

    # ================================================================
    # Kentucky
    # ================================================================
    "Louisville, Kentucky",
    "Lexington, Kentucky",
    "Bowling Green, Kentucky",
    "Covington, Kentucky",
    "Florence, Kentucky",
    "Owensboro, Kentucky",
    "Richmond, Kentucky",
    "Georgetown, Kentucky",
    "Nicholasville, Kentucky",
    "Elizabethtown, Kentucky",

    "Frankfort, Kentucky",
    "Shelbyville, Kentucky",
    "La Grange, Kentucky",
    "Bardstown, Kentucky",
    "Radcliff, Kentucky",
    "Danville, Kentucky",
    "Harrodsburg, Kentucky",
    "Winchester, Kentucky",
    "Paris, Kentucky",
    "Independence, Kentucky",
    "Erlanger, Kentucky",
    "Newport, Kentucky",
    "Fort Thomas, Kentucky",
]


@dataclass(slots=True)
class LocationResult:
    seed: str
    status_code: int | None
    total_results: int
    returned_results: int
    records: list[dict[str, Any]]
    error: str | None = None
    attempts: int = 0


class MeijerAcquisitionStrategy:
    """
    Meijer authoritative store-location acquisition.

    Primary source:
        Meijer official store search API.

    Endpoint:
        /bin/meijer/store/search
        ?locationQuery=<geographic seed>&radius=1000

    Acquisition model:
        geographic seed enumeration -> API results -> merge by mfcStoreId

    Important observations from the live API:
        - The response exposes pagination metadata.
        - Common page parameters such as page, pageNumber, and currentPage
          were observed to be ignored by the endpoint.
        - The front-end behaves as a capped nearby/load-more search.
        - Therefore this strategy does not depend on an undocumented page
          parameter. It intentionally uses overlapping geographic seeds and
          unions all returned stores by the authoritative mfcStoreId.

    Store authority:
        mfcStoreId is used as retailer_store_id/store_number.

    Direct fields:
        address, city, state, ZIP, phone, latitude, longitude, and
        displayName are taken directly from the official API.

    Non-retail/service POS:
        Records without mfcStoreId are excluded from the canonical
        retail-location output.
    """

    retailer = RETAILER
    retailer_key = RETAILER_KEY
    source_type = "json"
    provider = "Meijer official store search API"

    def __init__(
        self,
        *,
        radius_miles: int = RADIUS_MILES,
        workers: int = WORKERS,
    ) -> None:
        """Initialize acquisition configuration and run state.

        :param radius_miles: Search radius in miles.
        :param workers: Maximum number of concurrent workers.
        :return: Result produced by init  .
        """
        self.radius_miles = radius_miles
        self.workers = workers

        self.http_status_counts: dict[int, int] = {}
        self.failed_seeds: list[dict[str, Any]] = []
        self.seed_results: list[LocationResult] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self) -> dict[str, Any]:
        """Run the complete store location acquisition workflow.

        :return: Acquired records, validation results, and run metadata.
        """
        seeds = list(
            dict.fromkeys(
                MEIJER_LOCATION_SEEDS
            )
        )

        self._print_header(
            seeds
        )

        results = self._fetch_seeds(
            seeds
        )

        records_by_store_id: dict[
            str,
            dict[str, Any],
        ] = {}

        raw_record_count = 0
        excluded_without_store_id = 0

        for result in results:
            self.seed_results.append(
                result
            )

            if result.error:
                self.failed_seeds.append(
                    {
                        "seed": result.seed,
                        "status_code": (
                            result.status_code
                        ),
                        "error": result.error,
                        "attempts": result.attempts,
                    }
                )
                continue

            raw_record_count += len(
                result.records
            )

            for record in result.records:
                store_id = record.get(
                    "retailer_store_id"
                )

                if not store_id:
                    excluded_without_store_id += 1
                    continue

                records_by_store_id[
                    str(store_id)
                ] = record

        records = sorted(
            records_by_store_id.values(),
            key=self._store_sort_key,
        )

        validation = self._validate(
            records=records,
            raw_record_count=raw_record_count,
            excluded_without_store_id=(
                excluded_without_store_id
            ),
            seed_count=len(seeds),
        )

        return {
            "retailer": self.retailer,
            "retailer_key": self.retailer_key,
            "source_type": self.source_type,
            "provider": self.provider,
            "records": records,
            "validation": validation,
            "seed_count": len(seeds),
            "successful_seeds": (
                len(seeds)
                - len(self.failed_seeds)
            ),
            "failed_seeds": self.failed_seeds,
            "seed_results": [
                {
                    "seed": item.seed,
                    "status_code": item.status_code,
                    "total_results": item.total_results,
                    "returned_results": item.returned_results,
                    "attempts": item.attempts,
                    "records": len(item.records),
                    "error": item.error,
                }
                for item in self.seed_results
            ],
            "http_status_counts": (
                self.http_status_counts
            ),
            "notes": self._build_notes(),
        }

    # ------------------------------------------------------------------
    # HTTP acquisition
    # ------------------------------------------------------------------

    def _fetch_seeds(
        self,
        seeds: list[str],
    ) -> list[LocationResult]:
        """Fetch seeds.

        :param seeds: Geographic seeds included in the run.
        :return: Result produced by fetch seeds.
        """
        results: list[LocationResult] = []

        with ThreadPoolExecutor(
            max_workers=self.workers
        ) as executor:
            futures = {
                executor.submit(
                    self._fetch_seed,
                    seed,
                ): seed
                for seed in seeds
            }

            for future in tqdm(
                as_completed(futures),
                total=len(futures),
                desc="Meijer geographic seeds",
                unit="seed",
            ):
                results.append(
                    future.result()
                )

        return results

    def _fetch_seed(
        self,
        seed: str,
    ) -> LocationResult:
        """Fetch seed.

        :param seed: Geographic seed used for the request.
        :return: Result produced by fetch seed.
        """
        params = {
            "locationQuery": seed,
            "radius": self.radius_miles,
        }

        last_status: int | None = None
        last_error: str | None = None

        session = self._build_session()

        for attempt in range(
            1,
            MAX_RETRIES + 1,
        ):
            try:
                if attempt > 1:
                    time.sleep(
                        BACKOFFS[
                            attempt - 2
                        ]
                    )

                response = session.get(
                    MEIJER_SEARCH_URL,
                    params=params,
                    timeout=REQUEST_TIMEOUT,
                )

                last_status = response.status_code

                self.http_status_counts[
                    response.status_code
                ] = (
                    self.http_status_counts.get(
                        response.status_code,
                        0,
                    )
                    + 1
                )

                if response.status_code != 200:
                    last_error = (
                        f"HTTP {response.status_code}"
                    )
                    continue

                if not response.text.strip():
                    last_error = (
                        "Empty response body"
                    )
                    continue

                try:
                    payload = response.json()
                except ValueError as exc:
                    last_error = (
                        f"Invalid JSON response: {exc}"
                    )
                    continue

                return self._parse_result(
                    seed=seed,
                    payload=payload,
                    attempt=attempt,
                )

            except requests.RequestException as exc:
                last_error = repr(exc)

        return LocationResult(
            seed=seed,
            status_code=last_status,
            total_results=0,
            returned_results=0,
            records=[],
            error=last_error,
            attempts=MAX_RETRIES,
        )

    @staticmethod
    def _build_session() -> requests.Session:
        """Build session.

        :return: Result produced by build session.
        """
        session = requests.Session()

        session.headers.update(
            {
                "Accept": (
                    "application/json, text/plain, */*"
                ),
                "Accept-Language": (
                    "en-US,en;q=0.9"
                ),
                "Cache-Control": (
                    "no-cache, no-store, "
                    "must-revalidate, max-age=-1, private"
                ),
                "Pragma": "no-cache",
                "Referer": (
                    "https://www.meijer.com/"
                    "shopping/store-finder.html"
                ),
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/151.0.0.0 Safari/537.36"
                ),
            }
        )

        return session

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_result(
        self,
        *,
        seed: str,
        payload: dict[str, Any],
        attempt: int,
    ) -> LocationResult:
        """Parse result.

        :param seed: Geographic seed used for the request.
        :param payload: JSON payload returned by the retailer endpoint.
        :param attempt: Request attempt number.
        :return: Result produced by parse result.
        """
        pagination = (
            payload.get(
                "pagination"
            )
            or {}
        )

        points = (
            payload.get(
                "pointsOfService"
            )
            or []
        )

        total_results = (
            self._safe_int(
                pagination.get(
                    "totalResults"
                )
            )
        )

        records = self._normalize_points(
            points
        )

        return LocationResult(
            seed=seed,
            status_code=200,
            total_results=total_results,
            returned_results=len(points),
            records=records,
            attempts=attempt,
        )

    @staticmethod
    def _normalize_points(
        points: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Normalize points.

        :param points: Raw point-of-service records returned by the API.
        :return: Result produced by normalize points.
        """
        records: list[dict[str, Any]] = []

        for point in points:
            store_id = point.get(
                "mfcStoreId"
            )

            # Non-retail/service POS can appear in the endpoint without
            # a canonical retail store ID. Keep them out of the final
            # retailer location dataset.
            if not store_id:
                continue

            address = (
                point.get(
                    "address"
                )
                or {}
            )

            region = (
                address.get(
                    "region"
                )
                or {}
            )

            geo = (
                point.get(
                    "geoPoint"
                )
                or {}
            )

            state = MeijerAcquisitionStrategy._normalize_state(
                region.get(
                    "isocode"
                )
            )

            records.append(
                {
                    "retailer": RETAILER,
                    "retailer_key": RETAILER_KEY,
                    "retailer_store_id": str(
                        store_id
                    ),
                    "store_number": str(
                        store_id
                    ),
                    "store_name": (
                        point.get(
                            "displayName"
                        )
                        or point.get(
                            "name"
                        )
                    ),
                    "address": address.get(
                        "line1"
                    ),
                    "city": address.get(
                        "town"
                    ),
                    "state": state,
                    "zip_code": address.get(
                        "postalCode"
                    ),
                    "full_address": (
                        MeijerAcquisitionStrategy
                        ._build_full_address(
                            address,
                            state,
                        )
                    ),
                    "phone": point.get(
                        "phone"
                    ),
                    "latitude": geo.get(
                        "latitude"
                    ),
                    "longitude": geo.get(
                        "longitude"
                    ),
                    "source": (
                        "Meijer official store search API"
                    ),
                    "source_type": "json",
                }
            )

        return records

    @staticmethod
    def _normalize_state(
        value: Any,
    ) -> str | None:
        """Normalize state.

        :param value: Value to normalize or convert.
        :return: Result produced by normalize state.
        """
        if not value:
            return None

        text = str(value).strip()

        mapping = {
            "US-MI": "MI",
            "US-IL": "IL",
            "US-IN": "IN",
            "US-OH": "OH",
            "US-WI": "WI",
            "US-KY": "KY",
        }

        return mapping.get(
            text,
            text,
        )

    @staticmethod
    def _build_full_address(
        address: dict[str, Any],
        state: str | None,
    ) -> str | None:
        """Build full address.

        :param address: Address.
        :param state: State name or abbreviation.
        :return: Result produced by build full address.
        """
        parts = [
            address.get(
                "line1"
            ),
            address.get(
                "town"
            ),
            state,
            address.get(
                "postalCode"
            ),
        ]

        values = [
            str(value).strip()
            for value in parts
            if value
        ]

        return (
            ", ".join(values)
            if values
            else None
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate(
        self,
        *,
        records: list[dict[str, Any]],
        raw_record_count: int,
        excluded_without_store_id: int,
        seed_count: int,
    ) -> dict[str, Any]:
        """Handle validate.

        :param records: Store records to process.
        :param raw_record_count: Raw record count.
        :param excluded_without_store_id: Excluded without store id.
        :param seed_count: Seed count.
        :return: Result produced by validate.
        """
        ids = [
            str(
                record.get(
                    "retailer_store_id"
                )
            )
            for record in records
            if record.get(
                "retailer_store_id"
            )
        ]

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

        missing_coordinates = sum(
            record.get(
                "latitude"
            ) is None
            or record.get(
                "longitude"
            ) is None
            for record in records
        )

        missing_phones = sum(
            not record.get(
                "phone"
            )
            for record in records
        )

        state_counts: dict[str, int] = {}

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

        unexpected_states = {
            state: count
            for state, count
            in state_counts.items()
            if state not in {
                "MI",
                "IL",
                "IN",
                "OH",
                "WI",
                "KY",
            }
        }

        valid = not (
            missing_ids
            or missing_addresses
            or unexpected_states
            or self.failed_seeds
        )

        issues: list[str] = []

        if missing_ids:
            issues.append(
                "missing_store_ids"
            )

        if missing_addresses:
            issues.append(
                "missing_addresses"
            )

        if unexpected_states:
            issues.append(
                "unexpected_states"
            )

        if self.failed_seeds:
            issues.append(
                "failed_seeds"
            )

        return {
            "valid": valid,
            "total_records": len(records),
            "unique_store_ids": len(
                set(ids)
            ),
            "raw_record_count": (
                raw_record_count
            ),
            "excluded_without_store_id": (
                excluded_without_store_id
            ),
            "duplicate_records_merged": (
                raw_record_count
                - excluded_without_store_id
                - len(records)
            ),
            "missing_store_ids": (
                missing_ids
            ),
            "missing_addresses": (
                missing_addresses
            ),
            "missing_coordinates": (
                missing_coordinates
            ),
            "missing_phones": (
                missing_phones
            ),
            "seed_count": seed_count,
            "successful_seeds": (
                seed_count
                - len(self.failed_seeds)
            ),
            "failed_seeds": len(
                self.failed_seeds
            ),
            "state_counts": state_counts,
            "unexpected_states": (
                unexpected_states
            ),
            "issues": issues,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_int(
        value: Any,
    ) -> int:
        """Handle safe int.

        :param value: Value to normalize or convert.
        :return: Result produced by safe int.
        """
        try:
            return int(
                value
            )
        except (
            TypeError,
            ValueError,
        ):
            return 0

    @staticmethod
    def _store_sort_key(
        record: dict[str, Any],
    ) -> tuple[int, str]:
        """Handle store sort key.

        :param record: Store record to process.
        :return: Result produced by store sort key.
        """
        value = str(
            record.get(
                "retailer_store_id",
                "",
            )
        )

        if value.isdigit():
            return (
                0,
                f"{int(value):010d}",
            )

        return (
            1,
            value,
        )

    @staticmethod
    def _print_header(
        seeds: list[str],
    ) -> None:
        """Handle print header.

        :param seeds: Geographic seeds included in the run.
        :return: Result produced by print header.
        """
        print("=" * 72)
        print(
            "Meijer Acquisition Strategy v1"
        )
        print("=" * 72)
        print(
            "Source: Meijer official store search API"
        )
        print(
            "Method: requests + JSON"
        )
        print(
            "Hierarchy: geographic seed -> radius=1000 "
            "-> pointsOfService -> merge by mfcStoreId"
        )
        print(
            "Store ID: official mfcStoreId"
        )
        print(
            "Coordinates: official geoPoint.latitude/longitude"
        )
        print(
            "States: MI, IL, IN, OH, WI, KY"
        )
        print(
            f"Geographic seeds: {len(seeds)}"
        )
        print(
            f"Radius: {RADIUS_MILES}"
        )
        print(
            f"Workers: {WORKERS}"
        )
        print()

    @staticmethod
    def _build_notes() -> list[str]:
        """Build notes.

        :return: Result produced by build notes.
        """
        return [
            (
                "Official source: Meijer store search API."
            ),
            (
                "The acquisition uses geographic seeds across the six "
                "states represented in the Meijer footprint."
            ),
            (
                "Each seed is queried with radius=1000 miles."
            ),
            (
                "The observed API exposes pagination metadata, but the "
                "usual page, pageNumber, and currentPage query parameters "
                "were ignored during testing."
            ),
            (
                "The strategy therefore does not depend on an undocumented "
                "next-page parameter; it enumerates overlapping geographic "
                "searches and merges the returned locations."
            ),
            (
                "All returned POS records are merged by official mfcStoreId."
            ),
            (
                "Records without mfcStoreId are excluded from the canonical "
                "retail-location output because the API can expose "
                "non-retail/service POS records."
            ),
            (
                "Address, city, state, ZIP, phone, store name, and "
                "latitude/longitude come directly from Meijer's API."
            ),
        ]