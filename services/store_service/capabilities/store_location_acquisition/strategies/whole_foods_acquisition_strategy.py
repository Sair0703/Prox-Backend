from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import json
import random
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://www.wholefoodsmarket.com/aplf/list"

RETAILER = "Whole Foods Market"
RETAILER_KEY = "whole_foods"

ALM_BRAND_ID = "VUZHIFdob2xlIEZvb2Rz"
CONTEXT = "wholefoods"

# Put higher-likelihood Whole Foods states first so short runs are useful.
DEFAULT_STATES = (
    "CA", "NY", "IL", "WA", "TX", "FL", "MA", "NJ",
    "PA", "VA", "MD", "CO", "OR", "AZ",
    "NC", "GA", "MI", "OH", "CT", "RI", "NH", "VT",
    "MN", "WI", "TN", "UT", "NV", "NM", "KS", "MO",
    "OK", "LA", "SC", "DE", "ME", "WV", "ID", "IA",
    "AL", "AR", "MS", "NE", "MT", "ND", "SD", "WY",
    "KY", "IN", "AK", "HI",
)

REQUEST_TIMEOUT = 30
MAX_RETRIES = 4
BACKOFFS = (2.0, 4.0, 8.0, 16.0)

MIN_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 2.5

ZIPS_PER_COUNTY = 1

# Empty results are normal and must NOT trigger fail-fast.
# We only fail-fast on repeated request/HTML/parser failures.
MAX_CONSECUTIVE_HARD_FAILURES = 3

DEBUG_ROOT = "debug"


@dataclass(slots=True)
class CountyZipSeed:
    state: str
    county: str
    zip_code: str


class LocatorResponseError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        final_url: str | None = None,
        content_type: str | None = None,
        html_length: int | None = None,
        html: str | None = None,
    ) -> None:
        """Initialize acquisition configuration and run state.

        :param message: Message.
        :param status_code: Status code.
        :param final_url: Final url.
        :param content_type: Content type.
        :param html_length: Html length.
        :param html: HTML content to parse.
        :return: Result produced by init  .
        """
        super().__init__(message)
        self.status_code = status_code
        self.final_url = final_url
        self.content_type = content_type
        self.html_length = html_length
        self.html = html


class WholeFoodsAcquisitionStrategy:
    """
    Whole Foods Market official locator acquisition v3.

    Key v3 fixes:
      - A valid locator page with zero stores is a normal result.
      - Empty ZIP/county results are recorded but never stop the run.
      - Fail-fast applies only to hard request/HTML/parser failures.
      - State ordering prioritizes high-density Whole Foods states for
        useful short test runs.
      - Requests remain conservative: one session, one request at a time,
        randomized delay, exponential backoff.
    """

    def __init__(
        self,
        *,
        states: tuple[str, ...] = DEFAULT_STATES,
        min_delay: float = MIN_DELAY_SECONDS,
        max_delay: float = MAX_DELAY_SECONDS,
        zips_per_county: int = ZIPS_PER_COUNTY,
        max_consecutive_hard_failures: int = (
            MAX_CONSECUTIVE_HARD_FAILURES
        ),
        debug_root: str = DEBUG_ROOT,
    ) -> None:
        """Initialize acquisition configuration and run state.

        :param states: States.
        :param min_delay: Min delay.
        :param max_delay: Max delay.
        :param zips_per_county: Zips per county.
        :param max_consecutive_hard_failures: Max consecutive hard failures.
        :param debug_root: Debug root.
        :return: Result produced by init  .
        """
        self.states = tuple(states)
        self.min_delay = max(0.0, min_delay)
        self.max_delay = max(self.min_delay, max_delay)
        self.zips_per_county = max(1, zips_per_county)
        self.max_consecutive_hard_failures = max(
            1,
            max_consecutive_hard_failures,
        )
        self.debug_root = debug_root

        self.failed_seeds: list[dict[str, Any]] = []
        self.empty_seeds: list[dict[str, Any]] = []
        self.excluded_cards: list[dict[str, Any]] = []

        self._session = requests.Session()
        self._session.headers.update(
            {
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,image/avif,image/webp,image/apng,*/*;"
                    "q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Referer": "https://www.wholefoodsmarket.com/",
                "Sec-Fetch-Dest": "iframe",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/151.0.0.0 Safari/537.36"
                ),
            }
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(
        self,
        *,
        seed_limit: int | None = None,
    ) -> dict[str, Any]:
        """Run the complete store location acquisition workflow.

        :param seed_limit: Optional maximum number of geographic seeds to process.
        :return: Acquired records, validation results, and run metadata.
        """
        seeds = self._load_county_zip_seeds()

        if seed_limit is not None:
            seeds = seeds[: max(0, seed_limit)]

        print(
            f"Whole Foods county ZIP seeds: {len(seeds)}"
        )

        raw_cards: list[dict[str, Any]] = []
        successful_nonempty_seeds = 0
        successful_empty_seeds = 0
        consecutive_hard_failures = 0
        stopped_early = False

        for index, seed in enumerate(
            seeds,
            start=1,
        ):
            if index > 1:
                self._sleep_random()

            try:
                html, request_meta = self._fetch_seed(seed)

                self._validate_html_shape(
                    html,
                    seed,
                    request_meta,
                )

                cards = self._parse_store_cards(
                    html,
                    seed,
                )

                if cards:
                    successful_nonempty_seeds += 1
                    raw_cards.extend(cards)
                else:
                    successful_empty_seeds += 1
                    self.empty_seeds.append(
                        {
                            "state": seed.state,
                            "county": seed.county,
                            "zip_code": seed.zip_code,
                            "status_code": request_meta[
                                "status_code"
                            ],
                            "final_url": request_meta[
                                "final_url"
                            ],
                            "content_type": request_meta[
                                "content_type"
                            ],
                            "html_length": len(html),
                            "card_marker_count": request_meta[
                                "card_marker_count"
                            ],
                        }
                    )

                # A successful 200 locator page, even with zero stores,
                # resets the hard-failure streak.
                consecutive_hard_failures = 0

            except Exception as exc:
                consecutive_hard_failures += 1

                error_entry = {
                    "state": seed.state,
                    "county": seed.county,
                    "zip_code": seed.zip_code,
                    "error": repr(exc),
                }

                if isinstance(exc, LocatorResponseError):
                    error_entry.update(
                        {
                            "status_code": exc.status_code,
                            "final_url": exc.final_url,
                            "content_type": exc.content_type,
                            "html_length": exc.html_length,
                        }
                    )

                    if exc.html:
                        debug_path = self._save_debug_html(
                            seed,
                            exc.html,
                            prefix="failure",
                        )
                        if debug_path:
                            error_entry[
                                "debug_html"
                            ] = str(debug_path)

                self.failed_seeds.append(
                    error_entry
                )

                if (
                    consecutive_hard_failures
                    >= self.max_consecutive_hard_failures
                ):
                    stopped_early = True
                    print(
                        "Stopping early: "
                        f"{consecutive_hard_failures} consecutive "
                        "hard failures."
                    )
                    break

            if (
                index % 10 == 0
                or index == len(seeds)
                or stopped_early
            ):
                print(
                    f"[{index}/{len(seeds)}] "
                    f"raw cards={len(raw_cards)} "
                    f"non-empty seeds={successful_nonempty_seeds} "
                    f"empty seeds={successful_empty_seeds} "
                    f"failed seeds={len(self.failed_seeds)}"
                )

            if stopped_early:
                break

        records = self._merge_cards(
            raw_cards
        )

        validation = self._validate(
            seeds=seeds,
            successful_nonempty_seeds=successful_nonempty_seeds,
            successful_empty_seeds=successful_empty_seeds,
            raw_cards=raw_cards,
            records=records,
            stopped_early=stopped_early,
        )

        return {
            "retailer": RETAILER,
            "retailer_key": RETAILER_KEY,
            "source_type": "html",
            "records": records,
            "validation": validation,
            "county_zip_seeds": [
                {
                    "state": seed.state,
                    "county": seed.county,
                    "zip_code": seed.zip_code,
                }
                for seed in seeds
            ],
            "failed_seeds": self.failed_seeds,
            "empty_seeds": self.empty_seeds,
            "excluded_cards": self.excluded_cards,
            "notes": self._notes(),
        }

    # ------------------------------------------------------------------
    # Seeds
    # ------------------------------------------------------------------

    def _load_county_zip_seeds(
        self,
    ) -> list[CountyZipSeed]:
        """Load county zip seeds.

        :return: Result produced by load county zip seeds.
        """
        try:
            import pgeocode
        except ImportError as exc:
            raise RuntimeError(
                "pgeocode is required. Install with: pip install pgeocode"
            ) from exc

        nomi = pgeocode.Nominatim("us")
        data = nomi._data.copy()

        required = {
            "postal_code",
            "state_code",
            "county_name",
            "latitude",
            "longitude",
        }

        missing = required - set(data.columns)
        if missing:
            raise RuntimeError(
                "pgeocode dataset missing columns: "
                f"{sorted(missing)}"
            )

        data = data[
            data["state_code"].isin(
                self.states
            )
        ].copy()

        data = data.dropna(
            subset=[
                "postal_code",
                "state_code",
                "county_name",
            ]
        )

        data["postal_code"] = (
            data["postal_code"]
            .astype(str)
            .str.extract(
                r"(\d{5})",
                expand=False,
            )
        )

        data = data.dropna(
            subset=["postal_code"]
        )

        seeds: list[CountyZipSeed] = []

        # Respect the supplied high-density state ordering.
        state_rank = {
            state: index
            for index, state in enumerate(
                self.states
            )
        }

        for (
            state,
            county,
        ), group in data.groupby(
            ["state_code", "county_name"],
            dropna=True,
        ):
            group = group.copy()

            geo = group.dropna(
                subset=[
                    "latitude",
                    "longitude",
                ]
            )

            if not geo.empty:
                center_lat = float(
                    geo["latitude"].mean()
                )
                center_lon = float(
                    geo["longitude"].mean()
                )

                geo = geo.copy()
                geo["center_distance"] = (
                    (
                        geo["latitude"]
                        - center_lat
                    ) ** 2
                    + (
                        geo["longitude"]
                        - center_lon
                    ) ** 2
                )

                candidates = geo.sort_values(
                    "center_distance"
                )
            else:
                candidates = group.sort_values(
                    "postal_code"
                )

            chosen = candidates.head(
                self.zips_per_county
            )

            for _, row in chosen.iterrows():
                seeds.append(
                    CountyZipSeed(
                        state=str(state),
                        county=str(county),
                        zip_code=str(
                            row["postal_code"]
                        ).zfill(5),
                    )
                )

        seeds.sort(
            key=lambda item: (
                state_rank.get(
                    item.state,
                    9999,
                ),
                item.county,
                item.zip_code,
            )
        )

        return seeds

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _fetch_seed(
        self,
        seed: CountyZipSeed,
    ) -> tuple[str, dict[str, Any]]:
        """Fetch seed.

        :param seed: County ZIP seed associated with the request.
        :return: Result produced by fetch seed.
        """
        params = {
            "almBrandId": ALM_BRAND_ID,
            "context": CONTEXT,
            "postalCode": seed.zip_code,
        }

        last_error: Exception | None = None

        for attempt in range(
            1,
            MAX_RETRIES + 1,
        ):
            try:
                response = self._session.get(
                    BASE_URL,
                    params=params,
                    timeout=REQUEST_TIMEOUT,
                    allow_redirects=True,
                )

                content_type = response.headers.get(
                    "content-type",
                    "",
                )

                card_marker_count = response.text.count(
                    "data-store-details-page-button"
                )

                request_meta = {
                    "status_code": response.status_code,
                    "final_url": response.url,
                    "content_type": content_type,
                    "card_marker_count": card_marker_count,
                }

                if response.status_code != 200:
                    raise LocatorResponseError(
                        (
                            f"HTTP {response.status_code}: "
                            f"{response.text[:300]!r}"
                        ),
                        status_code=response.status_code,
                        final_url=response.url,
                        content_type=content_type,
                        html_length=len(
                            response.text
                        ),
                        html=response.text,
                    )

                if not response.text:
                    raise LocatorResponseError(
                        "Empty HTML response",
                        status_code=response.status_code,
                        final_url=response.url,
                        content_type=content_type,
                        html_length=0,
                        html=response.text,
                    )

                if "text/html" not in content_type.lower():
                    raise LocatorResponseError(
                        (
                            "Unexpected content-type="
                            f"{content_type!r}"
                        ),
                        status_code=response.status_code,
                        final_url=response.url,
                        content_type=content_type,
                        html_length=len(
                            response.text
                        ),
                        html=response.text,
                    )

                return (
                    response.text,
                    request_meta,
                )

            except (
                requests.RequestException,
                LocatorResponseError,
            ) as exc:
                last_error = exc

                if attempt == MAX_RETRIES:
                    break

                time.sleep(
                    BACKOFFS[
                        min(
                            attempt - 1,
                            len(BACKOFFS) - 1,
                        )
                    ]
                )

        raise RuntimeError(
            f"Failed seed {seed.zip_code}: "
            f"{last_error!r}"
        )

    def _validate_html_shape(
        self,
        html: str,
        seed: CountyZipSeed,
        request_meta: dict[str, Any],
    ) -> None:
        """Validate html shape.

        :param html: HTML content to parse.
        :param seed: County ZIP seed associated with the request.
        :param request_meta: HTTP metadata collected for the locator response.
        :return: Result produced by validate html shape.
        """
        markers = (
            "aplf-list-container",
            "Find a store near you",
            "data-action=\"list-item-button\"",
        )

        found = sum(
            marker in html
            for marker in markers
        )

        if found < 2:
            debug_path = self._save_debug_html(
                seed,
                html,
                prefix="shape_mismatch",
            )

            raise LocatorResponseError(
                (
                    "Whole Foods locator HTML does not "
                    "match the expected page shape. "
                    f"Found {found}/{len(markers)} markers. "
                    f"debug={debug_path}"
                ),
                status_code=request_meta[
                    "status_code"
                ],
                final_url=request_meta[
                    "final_url"
                ],
                content_type=request_meta[
                    "content_type"
                ],
                html_length=len(html),
                html=html,
            )

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_store_cards(
        self,
        html: str,
        seed: CountyZipSeed,
    ) -> list[dict[str, Any]]:
        """Parse store cards.

        :param html: HTML content to parse.
        :param seed: County ZIP seed associated with the request.
        :return: Result produced by parse store cards.
        """
        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        cards: list[dict[str, Any]] = []

        nodes = soup.select(
            "[data-store-details-page-button]"
        )

        for node in nodes:
            payload = self._parse_json_attribute(
                node,
                "data-store-details-page-button",
            )

            if not payload:
                continue

            normalized = self._normalize_payload(
                payload,
                seed,
            )

            if normalized:
                cards.append(
                    normalized
                )

        # Some pages may omit the detail attribute and only expose pickup
        # payloads.
        if not cards:
            for node in soup.select(
                "[data-iframe-store-pickup-button]"
            ):
                payload = self._parse_json_attribute(
                    node,
                    "data-iframe-store-pickup-button",
                )

                if not payload:
                    continue

                normalized = self._normalize_payload(
                    payload,
                    seed,
                )

                if normalized:
                    cards.append(
                        normalized
                    )

        return self._dedupe_cards_in_response(
            cards
        )

    def _normalize_payload(
        self,
        payload: dict[str, Any],
        seed: CountyZipSeed,
    ) -> dict[str, Any] | None:
        """Normalize payload.

        :param payload: Raw retailer payload to normalize.
        :param seed: County ZIP seed associated with the request.
        :return: Result produced by normalize payload.
        """
        location = payload.get("location")

        if not isinstance(location, dict):
            return None

        if location.get(
            "locationType"
        ) != "STORE":
            self.excluded_cards.append(
                {
                    "reason": "not_store",
                    "seed_zip": seed.zip_code,
                    "location_id": location.get(
                        "locationId"
                    ),
                }
            )
            return None

        brand = location.get("brand") or {}
        brand_name = (
            brand.get("defaultString")
            if isinstance(brand, dict)
            else None
        )

        if (
            brand_name
            and brand_name != RETAILER
        ):
            self.excluded_cards.append(
                {
                    "reason": "wrong_brand",
                    "seed_zip": seed.zip_code,
                    "location_id": location.get(
                        "locationId"
                    ),
                    "brand": brand_name,
                }
            )
            return None

        address = location.get(
            "address"
        ) or {}
        geocode = location.get(
            "geocode"
        ) or {}

        address_lines = address.get(
            "addressLines"
        )

        street = None

        if (
            isinstance(
                address_lines,
                list,
            )
            and address_lines
        ):
            street = self._clean(
                address_lines[0]
            )

        city = self._clean(
            address.get("city")
        )
        state = self._clean(
            address.get("state")
        )
        zip_code = self._clean(
            address.get("postalCode")
        )

        location_id = self._clean(
            location.get("locationId")
        )
        store_code = self._clean(
            location.get("storeCode")
        )
        store_name = self._clean(
            location.get("locationName")
        )

        if not store_name:
            return None

        if not location_id and not store_code:
            return None

        return {
            "retailer": RETAILER,
            "retailer_key": RETAILER_KEY,
            "store_name": store_name,
            "retailer_store_id": store_code,
            "store_number": store_code,
            "source_location_id": location_id,
            "address": street,
            "city": city,
            "state": state,
            "zip_code": zip_code,
            "full_address": self._build_full_address(
                street,
                city,
                state,
                zip_code,
            ),
            "phone": self._clean(
                location.get("phoneNumber")
            ),
            "latitude": self._float_or_none(
                geocode.get("latitude")
            ),
            "longitude": self._float_or_none(
                geocode.get("longitude")
            ),
            "whole_foods_market_folder": self._clean(
                location.get(
                    "wholeFoodsMarketFolder"
                )
            ),
            "location_type": self._clean(
                location.get("locationType")
            ),
            "brand": self._clean(
                brand_name
            ),
            "seed_state": seed.state,
            "seed_county": seed.county,
            "seed_zip": seed.zip_code,
            "source": (
                "Whole Foods official store locator"
            ),
            "source_type": "html",
        }

    @staticmethod
    def _parse_json_attribute(
        node: Any,
        attribute: str,
    ) -> dict[str, Any] | None:
        """Parse json attribute.

        :param node: HTML node to inspect.
        :param attribute: HTML attribute containing serialized JSON.
        :return: Result produced by parse json attribute.
        """
        raw = node.get(attribute)

        if not raw:
            return None

        try:
            payload = json.loads(raw)
        except (
            json.JSONDecodeError,
            TypeError,
        ):
            return None

        return (
            payload
            if isinstance(
                payload,
                dict,
            )
            else None
        )

    @staticmethod
    def _dedupe_cards_in_response(
        cards: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Deduplicate cards in response.

        :param cards: Store cards to deduplicate.
        :return: Result produced by dedupe cards in response.
        """
        seen: set[str] = set()
        result: list[dict[str, Any]] = []

        for card in cards:
            key = (
                card.get(
                    "retailer_store_id"
                )
                or card.get(
                    "source_location_id"
                )
                or WholeFoodsAcquisitionStrategy._identity_key(
                    card
                )
            )

            if key in seen:
                continue

            seen.add(key)
            result.append(card)

        return result

    # ------------------------------------------------------------------
    # Merge
    # ------------------------------------------------------------------

    def _merge_cards(
        self,
        cards: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Merge cards.

        :param cards: Store cards to deduplicate.
        :return: Result produced by merge cards.
        """
        merged: dict[
            str,
            dict[str, Any],
        ] = {}

        for card in cards:
            key = self._identity_key(
                card
            )

            existing = merged.get(
                key
            )

            if existing is None:
                merged[key] = dict(card)
                continue

            merged[key] = self._merge_two(
                existing,
                card,
            )

        records = list(
            merged.values()
        )

        records.sort(
            key=lambda row: (
                row.get("state") or "",
                row.get("city") or "",
                row.get("store_name") or "",
            )
        )

        return records

    @staticmethod
    def _identity_key(
        record: dict[str, Any],
    ) -> str:
        """Handle identity key.

        :param record: Record.
        :return: Result produced by identity key.
        """
        store_code = record.get(
            "retailer_store_id"
        )

        if store_code:
            return (
                f"storecode:{store_code}"
            )

        source_id = record.get(
            "source_location_id"
        )

        if source_id:
            return (
                f"location:{source_id}"
            )

        parts = [
            WholeFoodsAcquisitionStrategy._normalize_text(
                record.get(field)
            )
            for field in (
                "address",
                "city",
                "state",
                "zip_code",
            )
        ]

        return (
            "address:"
            + "|".join(parts)
        )

    @staticmethod
    def _merge_two(
        first: dict[str, Any],
        second: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge two.

        :param first: Existing store record.
        :param second: Overlapping store record.
        :return: Result produced by merge two.
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

        for field in (
            "seed_state",
            "seed_county",
            "seed_zip",
        ):
            first_value = first.get(field)
            second_value = second.get(field)

            if (
                first_value
                and second_value
                and first_value != second_value
            ):
                provenance_key = (
                    f"{field}_all"
                )

                values: set[str] = set()

                existing = merged.get(
                    provenance_key
                )

                if existing:
                    values.update(
                        str(existing).split("|")
                    )

                values.add(
                    str(first_value)
                )
                values.add(
                    str(second_value)
                )

                merged[
                    provenance_key
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
        seeds: list[CountyZipSeed],
        successful_nonempty_seeds: int,
        successful_empty_seeds: int,
        raw_cards: list[dict[str, Any]],
        records: list[dict[str, Any]],
        stopped_early: bool,
    ) -> dict[str, Any]:
        """Handle validate.

        :param seeds: County ZIP seeds included in the run.
        :param successful_nonempty_seeds: Number of successful seeds that returned stores.
        :param successful_empty_seeds: Number of successful seeds that returned no stores.
        :param raw_cards: Raw store cards collected across geographic seeds.
        :param records: Store records to validate or process.
        :param stopped_early: Whether fail-fast stopped the acquisition early.
        :return: Result produced by validate.
        """
        state_counts: dict[str, int] = {}

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

        with_store_id = sum(
            bool(
                record.get(
                    "retailer_store_id"
                )
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
                not self.failed_seeds
                and not stopped_early
                and bool(records)
            ),
            "total_records": len(records),
            "raw_card_records": len(
                raw_cards
            ),
            "duplicate_records_merged": max(
                0,
                len(raw_cards)
                - len(records),
            ),
            "with_store_id": with_store_id,
            "missing_store_id": (
                len(records)
                - with_store_id
            ),
            "missing_addresses": (
                missing_addresses
            ),
            "missing_phones": (
                missing_phones
            ),
            "missing_coordinates": (
                missing_coordinates
            ),
            "county_zip_seeds": len(
                seeds
            ),
            "successful_nonempty_seeds": (
                successful_nonempty_seeds
            ),
            "successful_empty_seeds": (
                successful_empty_seeds
            ),
            "failed_seeds": len(
                self.failed_seeds
            ),
            "stopped_early": stopped_early,
            "state_counts": dict(
                sorted(
                    state_counts.items()
                )
            ),
            "issues": (
                ["failed_seed_queries"]
                if self.failed_seeds
                else []
            )
            + (
                ["stopped_early"]
                if stopped_early
                else []
            ),
        }

    # ------------------------------------------------------------------
    # Debug
    # ------------------------------------------------------------------

    def _save_debug_html(
        self,
        seed: CountyZipSeed,
        html: str,
        *,
        prefix: str,
    ) -> str | None:
        """Save debug html.

        :param seed: County ZIP seed associated with the request.
        :param html: HTML content to parse.
        :param prefix: Filename prefix describing the debug condition.
        :return: Result produced by save debug html.
        """
        try:
            root = (
                Path(self.debug_root)
                / "whole_foods"
            )

            root.mkdir(
                parents=True,
                exist_ok=True,
            )

            filename = (
                f"{prefix}_"
                f"{seed.state}_"
                f"{seed.county}_"
                f"{seed.zip_code}.html"
            )

            target = root / filename
            target.write_text(
                html,
                encoding="utf-8",
                errors="replace",
            )

            return str(target)

        except Exception:
            return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _sleep_random(self) -> None:
        """Pause for random.

        :return: Result produced by sleep random.
        """
        time.sleep(
            random.uniform(
                self.min_delay,
                self.max_delay,
            )
        )

    @staticmethod
    def _build_full_address(
        street: str | None,
        city: str | None,
        state: str | None,
        zip_code: str | None,
    ) -> str | None:
        """Build full address.

        :param street: Street.
        :param city: City or locality component.
        :param state: State name or abbreviation.
        :param zip_code: Postal-code component.
        :return: Result produced by build full address.
        """
        locality = None

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
    def _normalize_text(
        value: Any,
    ) -> str:
        """Normalize text.

        :param value: Value to normalize or convert.
        :return: Result produced by normalize text.
        """
        if value is None:
            return ""

        return re.sub(
            r"[^a-z0-9]+",
            " ",
            str(value).strip().lower(),
        ).strip()

    @staticmethod
    def _clean(
        value: Any,
    ) -> str | None:
        """Handle clean.

        :param value: Value to normalize or convert.
        :return: Result produced by clean.
        """
        if value is None:
            return None

        value = str(value).strip()
        return value or None

    @staticmethod
    def _float_or_none(
        value: Any,
    ) -> float | None:
        """Convert or none.

        :param value: Value to normalize or convert.
        :return: Result produced by float or none.
        """
        try:
            return float(value)
        except (
            TypeError,
            ValueError,
        ):
            return None

    @staticmethod
    def _notes() -> list[str]:
        """Handle notes.

        :return: Result produced by notes.
        """
        return [
            (
                "Official source: Whole Foods Market "
                "APLF store locator HTML."
            ),
            (
                "Valid locator responses with zero stores are treated "
                "as normal empty geographic results."
            ),
            (
                "Only hard request/HTML/parser failures count toward "
                "fail-fast."
            ),
            (
                "State ordering prioritizes higher-likelihood Whole Foods "
                "states so limited test runs produce useful data earlier."
            ),
            (
                "The acquisition is intentionally single-threaded with "
                "a conservative randomized delay."
            ),
            (
                "storeCode is the preferred retailer store ID when exposed."
            ),
            (
                "locationId is preserved separately as the source location ID."
            ),
            (
                "Only locationType=STORE records for Whole Foods Market "
                "are retained."
            ),
            (
                "Overlapping geographic results are merged by storeCode, "
                "then source location ID/address fallback."
            ),
        ]