# services/store_service/capabilities/store_location_promotion/store_location_promotion_service.py

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Mapping

from tqdm import tqdm

from config.supabase import get_supabase_client
from services.store_service.capabilities.store_info_normalization.store_info_normalization_service import (
    StoreInfoNormalizationService,
)
from services.store_service.capabilities.store_location_promotion.constants import (
    GEOCODER_CACHE_PATH,
    MANUAL_REVIEW_TABLE,
    OUTPUT_ROOT,
    PROMOTION_FAILURE_TABLE,
    PROMOTION_SOURCE,
    STAGING_TABLE,
    STORE_LOCATIONS_TABLE,
    WRITE_BATCH_SIZE,
)
from services.store_service.capabilities.store_location_promotion.models import (
    PromotionAuditWriter,
)
from services.store_service.geocoders.geocoder import Geocoder, geocode_store


class StoreLocationPromotionService:
    """Promotes eligible staging store locations into the canonical store table."""

    def __init__(
        self,
        *,
        target_retailer: str | None = None,
        test_staging_ids: set[int] | None = None,
    ) -> None:
        """
        Initialize the staging-to-canonical promotion workflow.

        :param target_retailer: Optional retailer filter for a targeted promotion run.
        :param test_staging_ids: Optional staging IDs for a narrow local test run.
        """
        self.target_retailer = target_retailer
        self.test_staging_ids = test_staging_ids or set()

        self._supabase = get_supabase_client()
        self._normalizer = StoreInfoNormalizationService()
        self._geocoder = Geocoder(
            cache_path=GEOCODER_CACHE_PATH,
            geoapify_min_delay_seconds=0.25,
        )

    @staticmethod
    def _clean(value: Any) -> str | None:
        """Normalize an optional value to a non-empty string."""
        if value is None:
            return None

        if isinstance(value, str):
            value = value.strip()
            return value if value else None

        text = str(value).strip()
        return text if text else None

    @staticmethod
    def _json_dumps(value: Any) -> str | None:
        """Serialize an optional value for audit output."""
        if value is None:
            return None

        return json.dumps(
            value,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )

    @staticmethod
    def _provider_base(provider: str | None) -> str | None:
        """Return the underlying geocode provider name."""
        if provider is None:
            return None

        if provider.startswith("cache:"):
            provider = provider.split(":", 1)[1]

        provider = provider.strip().lower()
        return provider or None

    def _is_target_retailer(self, retailer: str | None) -> bool:
        """Check whether a retailer is included in the current run."""
        if self.target_retailer is None:
            return True

        return self._clean(retailer) == self.target_retailer

    def _fetch_all_rows(
        self,
        table_name: str,
        select_columns: str = "*",
        filters: list[tuple[str, str, Any]] | None = None,
        page_size: int = 1000,
        order_column: str | None = "id",
        order_desc: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Fetch all matching rows from a Supabase table using pagination.

        :param table_name: Source table to query.
        :param select_columns: Supabase column selection expression.
        :param filters: Optional query filters expressed as method, column, and value.
        :param page_size: Maximum number of rows fetched per request.
        :param order_column: Optional column used to keep pagination deterministic.
        :param order_desc: Whether to order the pagination column descending.
        :return: All rows returned by the paginated query.
        """
        rows: list[dict[str, Any]] = []
        start = 0

        while True:
            end = start + page_size - 1
            query = self._supabase.table(table_name).select(select_columns)

            for method_name, column, value in filters or []:
                query = getattr(query, method_name)(column, value)

            if order_column is not None:
                query = query.order(order_column, desc=order_desc)

            response = query.range(start, end).execute()
            batch = response.data or []

            if not batch:
                break

            rows.extend(batch)

            if len(batch) < page_size:
                break

            start += page_size

        return rows

    def _get_current_max_store_location_id(self) -> int:
        """Return the current highest canonical store-location ID."""
        response = (
            self._supabase.table(STORE_LOCATIONS_TABLE)
            .select("id")
            .order("id", desc=True)
            .limit(1)
            .execute()
        )

        if not response.data:
            return 0

        return int(response.data[0]["id"])

    def _load_manual_review_map(self) -> dict[int, str]:
        """Load manual-review status by staging store-location ID."""
        rows = self._fetch_all_rows(
            MANUAL_REVIEW_TABLE,
            "staging_store_location_id, review_status",
            order_column="staging_store_location_id",
        )

        review_map: dict[int, str] = {}

        for row in rows:
            staging_id = row.get("staging_store_location_id")
            review_status = self._clean(row.get("review_status")) or "pending"

            if staging_id is not None:
                review_map[int(staging_id)] = review_status.lower()

        return review_map

    def _load_already_promoted_staging_ids(self) -> set[int]:
        """Load staging IDs that already reference canonical store locations."""
        rows = self._fetch_all_rows(
            STAGING_TABLE,
            "id, promoted_store_location_v2_id",
        )

        promoted_ids: set[int] = set()

        for row in rows:
            if row.get("promoted_store_location_v2_id") is not None:
                promoted_ids.add(int(row["id"]))

        return promoted_ids

    def _flush_promotion_failure_rows(
        self,
        pending_rows: list[dict[str, Any]],
    ) -> int:
        """
        Persist buffered promotion failures.

        :param pending_rows: Failure records waiting to be written.
        :return: Number of persisted failure records.
        """
        if not pending_rows:
            return 0

        self._supabase.table(PROMOTION_FAILURE_TABLE).upsert(
            pending_rows,
            on_conflict="staging_store_location_id",
        ).execute()

        return len(pending_rows)

    def _flush_staging_updates(
        self,
        pending_rows: list[dict[str, Any]],
    ) -> int:
        """
        Persist buffered promotion results on staging rows.

        :param pending_rows: Staging promotion updates waiting to be written.
        :return: Number of staging rows updated.
        """
        if not pending_rows:
            return 0

        for payload in pending_rows:
            row_id = payload["id"]

            update_payload = {
                "promoted_store_location_v2_id": payload.get(
                    "promoted_store_location_v2_id"
                ),
                "promoted_at_utc": payload.get("promoted_at_utc"),
                "promotion_error": payload.get("promotion_error"),
            }

            (
                self._supabase.table(STAGING_TABLE)
                .update(update_payload)
                .eq("id", row_id)
                .execute()
            )

        return len(pending_rows)

    def _flush_failure_clears(
        self,
        pending_ids: set[int],
    ) -> int:
        """
        Remove resolved failure records after successful promotion.

        :param pending_ids: Successfully promoted staging IDs with failures to clear.
        :return: Number of staging IDs cleared from the failure table.
        """
        if not pending_ids:
            return 0

        (
            self._supabase.table(PROMOTION_FAILURE_TABLE)
            .delete()
            .in_(
                "staging_store_location_id",
                sorted(pending_ids),
            )
            .execute()
        )

        return len(pending_ids)

    @staticmethod
    def _record_promotion_failure(
        staging_store_location_id: int,
        *,
        retailer: str | None,
        store_id: str | None,
        normalized_full_address: str | None,
        failure_reason: str,
        failure_details: str | None = None,
    ) -> dict[str, Any]:
        """
        Build a promotion-failure table payload.

        :param staging_store_location_id: Staging record that failed promotion.
        :param retailer: Normalized retailer name.
        :param store_id: Retailer-specific store identifier.
        :param normalized_full_address: Address used by the promotion workflow.
        :param failure_reason: Stable promotion failure code.
        :param failure_details: Optional diagnostic details.
        :return: Promotion-failure persistence payload.
        """
        return {
            "staging_store_location_id": staging_store_location_id,
            "retailer": retailer,
            "store_id": store_id,
            "normalized_full_address": normalized_full_address,
            "failure_reason": failure_reason,
            "failure_details": failure_details,
        }

    @staticmethod
    def _mark_staging_promotion_result(
        staging_store_location_id: int,
        promoted_store_location_v2_id: int | None = None,
        promotion_error: str | None = None,
    ) -> dict[str, Any]:
        """
        Build a staging-table promotion update.

        :param staging_store_location_id: Staging record being updated.
        :param promoted_store_location_v2_id: Canonical store ID created on success.
        :param promotion_error: Failure information retained on unsuccessful promotion.
        :return: Staging promotion update payload.
        """
        payload: dict[str, Any] = {
            "id": staging_store_location_id,
            "promoted_store_location_v2_id": promoted_store_location_v2_id,
            "promotion_error": promotion_error,
        }

        if promoted_store_location_v2_id is not None:
            payload["promoted_at_utc"] = datetime.now(timezone.utc).isoformat()
        else:
            payload["promoted_at_utc"] = None

        return payload

    @staticmethod
    def _print_failure_debug(
        *,
        staging_store_location_id: int,
        retailer: str | None,
        store_id: str | None,
        geocode_address: str | None,
        failure_reason: str,
        failure_details: str | None,
    ) -> None:
        """Print the relevant context for a failed promotion."""
        print()
        print("=" * 80)
        print("PROMOTION FAILURE")
        print(f"Staging ID   : {staging_store_location_id}")
        print(f"Retailer     : {retailer}")
        print(f"Store ID     : {store_id}")
        print(f"Address      : {geocode_address}")
        print(f"Reason       : {failure_reason}")

        if failure_details:
            print(f"Details      : {failure_details}")

        print("=" * 80)
        print()

    def _build_audit_row(
        self,
        *,
        run_id: str,
        row: Mapping[str, Any],
        status: str,
        normalized: Any = None,
        review_status: str | None = None,
        already_promoted_store_location_v2_id: int | None = None,
        payload: dict[str, Any] | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        geocode_confidence: str | None = None,
        geocode_provider: str | None = None,
        store_location_id: int | None = None,
        failure_reason: str | None = None,
        failure_details: str | None = None,
        promotion_error: str | None = None,
    ) -> dict[str, Any]:
        """Build one complete promotion audit row."""
        processed_at = datetime.now(timezone.utc).isoformat()

        retailer_raw = (
            normalized.raw_retailer
            if normalized is not None
            else self._clean(row.get("retailer"))
        )
        retailer = (
            normalized.retailer
            if normalized is not None
            else self._clean(row.get("retailer"))
        )
        retailer_key_sent = (
            normalized.retailer_key
            if normalized is not None
            else None
        )
        store_type = (
            normalized.store_type
            if normalized is not None
            else self._clean(row.get("store_type"))
        )
        store_number = (
            normalized.store_number
            if normalized is not None
            else self._clean(row.get("store_number"))
        )
        store_name = (
            normalized.store_name
            if normalized is not None
            else None
        )
        store_url = (
            normalized.store_url
            if normalized is not None
            else self._clean(row.get("store_url"))
        )
        source_sitemap = (
            normalized.source_sitemap
            if normalized is not None
            else self._clean(row.get("source_sitemap"))
        )
        phone = (
            normalized.phone
            if normalized is not None
            else self._clean(row.get("phone"))
        )
        extraction_source = (
            normalized.extraction_source
            if normalized is not None
            else self._clean(row.get("extraction_source"))
        )
        scrape_status = (
            normalized.scrape_status
            if normalized is not None
            else self._clean(row.get("scrape_status"))
        )
        http_status = (
            normalized.http_status
            if normalized is not None
            else row.get("http_status")
        )
        error_message = (
            normalized.error_message
            if normalized is not None
            else self._clean(row.get("error_message"))
        )
        street_address = (
            normalized.street_address
            if normalized is not None
            else self._clean(row.get("street_address"))
        )
        address_city = (
            normalized.address_city
            if normalized is not None
            else self._clean(row.get("address_city"))
        )
        address_state = (
            normalized.address_state
            if normalized is not None
            else self._clean(row.get("address_state"))
        )
        city = (
            normalized.city
            if normalized is not None
            else self._clean(row.get("city"))
        )
        state = (
            normalized.state
            if normalized is not None
            else self._clean(row.get("state"))
        )
        zip_code = (
            normalized.zip_code
            if normalized is not None
            else self._clean(row.get("zip_code"))
        )
        address = (
            normalized.address
            if normalized is not None
            else self._clean(row.get("address"))
        )
        full_address = (
            normalized.full_address
            if normalized is not None
            else self._clean(row.get("full_address"))
        )
        geocode_address = (
            normalized.full_address or normalized.address
            if normalized is not None
            else (
                self._clean(row.get("full_address"))
                or self._clean(row.get("address"))
            )
        )
        normalized_reason_codes = (
            ",".join(normalized.reason_codes)
            if normalized is not None
            else None
        )
        normalization_notes = (
            " | ".join(normalized.normalization_notes)
            if normalized is not None
            else None
        )

        return {
            "run_id": run_id,
            "processed_at_utc": processed_at,
            "staging_store_location_id": int(row["id"]),
            "status": status,
            "review_status": review_status,
            "already_promoted_store_location_v2_id": (
                already_promoted_store_location_v2_id
            ),
            "retailer_raw": retailer_raw,
            "retailer": retailer,
            "retailer_key_sent": retailer_key_sent,
            "store_type": store_type,
            "store_number": store_number,
            "store_name": store_name,
            "store_url": store_url,
            "source_sitemap": source_sitemap,
            "phone": phone,
            "extraction_source": extraction_source,
            "scrape_status": scrape_status,
            "http_status": http_status,
            "error_message": error_message,
            "street_address": street_address,
            "address_city": address_city,
            "address_state": address_state,
            "city": city,
            "state": state,
            "zip_code": zip_code,
            "address": address,
            "full_address": full_address,
            "geocode_address": geocode_address,
            "normalized_reason_codes": normalized_reason_codes,
            "normalization_notes": normalization_notes,
            "latitude": latitude,
            "longitude": longitude,
            "geocode_confidence": geocode_confidence,
            "geocode_provider": geocode_provider,
            "geocode_provider_base": self._provider_base(
                geocode_provider
            ),
            "store_location_id": store_location_id,
            "failure_reason": failure_reason,
            "failure_details": failure_details,
            "promotion_error": promotion_error,
            "payload_json": self._json_dumps(payload),
            "staging_row_json": self._json_dumps(row),
        }

    def _flush_buffers(
        self,
        *,
        pending_promotion_failure_rows: list[dict[str, Any]],
        pending_staging_updates: list[dict[str, Any]],
        pending_failure_clear_ids: set[int],
    ) -> int:
        """
        Persist all buffered promotion-side database changes.

        :param pending_promotion_failure_rows: Promotion failures awaiting upsert.
        :param pending_staging_updates: Staging promotion results awaiting update.
        :param pending_failure_clear_ids: Successful staging IDs whose failures should be removed.
        :return: Number of promotion failure rows persisted.
        """
        failure_count = self._flush_promotion_failure_rows(
            pending_promotion_failure_rows
        )
        pending_promotion_failure_rows.clear()

        self._flush_staging_updates(pending_staging_updates)
        pending_staging_updates.clear()

        self._flush_failure_clears(pending_failure_clear_ids)
        pending_failure_clear_ids.clear()

        return failure_count

    def promote(self) -> None:
        """Run the staging-to-canonical store-location promotion workflow."""
        run_started_at = datetime.now(timezone.utc)
        run_id = run_started_at.strftime("%Y%m%d_%H%M%S")
        run_output_dir = OUTPUT_ROOT / run_id
        audit = PromotionAuditWriter(run_output_dir, run_id)

        legacy_store_location_cutoff = (
            self._get_current_max_store_location_id()
        )

        print()
        print("=" * 72)
        print(f"Run ID: {run_id}")
        print(f"Output directory: {run_output_dir}")
        print(
            "Legacy store_locations cutoff id: "
            f"{legacy_store_location_cutoff}"
        )
        print(
            "New promoted store_locations will have id > "
            f"{legacy_store_location_cutoff}"
        )
        print("=" * 72)

        manual_review_map = self._load_manual_review_map()
        already_promoted_staging_ids = (
            self._load_already_promoted_staging_ids()
        )

        if self.test_staging_ids:
            staging_rows = self._fetch_all_rows(
                STAGING_TABLE,
                "*",
                filters=[
                    (
                        "in_",
                        "id",
                        sorted(self.test_staging_ids),
                    )
                ],
            )
        else:
            staging_rows = self._fetch_all_rows(
                STAGING_TABLE,
                "*",
            )

        inserted = 0
        skipped_already_promoted = 0
        skipped_due_to_review = 0
        skipped_target_retailer = 0
        validation_failed = 0
        geocode_failed = 0
        insert_failed = 0
        promotion_failures_recorded = 0

        rows_with_valid_addresses = 0
        rows_without_address = 0
        rows_geocoded_successfully = 0
        rows_geocoded_by_primary_provider = 0
        rows_geocoded_by_fallback_provider = 0
        rows_geocoded_by_cached_provider = 0
        rows_still_missing_coordinates = 0
        rows_ready_for_promotion = 0
        rows_geocode_failed = 0
        staging_rows_considered = 0

        status_counts: Counter[str] = Counter()
        geocode_provider_counts: Counter[str] = Counter()
        geocode_confidence_counts: Counter[str] = Counter()

        pending_promotion_failure_rows: list[dict[str, Any]] = []
        pending_staging_updates: list[dict[str, Any]] = []
        pending_failure_clear_ids: set[int] = set()

        for row in tqdm(
            staging_rows,
            desc="Promoting staging rows",
            unit="row",
        ):
            staging_store_location_id = int(row["id"])
            review_status = manual_review_map.get(
                staging_store_location_id
            )
            already_promoted_store_location_v2_id = row.get(
                "promoted_store_location_v2_id"
            )

            normalized = self._normalizer.normalize(row)
            geocode_address = (
                normalized.full_address
                or normalized.address
            )

            if (
                self.target_retailer is not None
                and not self._is_target_retailer(
                    normalized.retailer
                )
            ):
                skipped_target_retailer += 1
                status = "SKIPPED_TARGET_RETAILER"
                status_counts[status] += 1

                audit.write_result(
                    self._build_audit_row(
                        run_id=run_id,
                        row=row,
                        status=status,
                        normalized=normalized,
                        review_status=review_status,
                        already_promoted_store_location_v2_id=(
                            already_promoted_store_location_v2_id
                        ),
                        failure_reason=status,
                    )
                )
                continue

            staging_rows_considered += 1

            if geocode_address:
                rows_with_valid_addresses += 1
            else:
                rows_without_address += 1

            if (
                staging_store_location_id
                in already_promoted_staging_ids
            ):
                skipped_already_promoted += 1
                status = "SKIPPED_ALREADY_PROMOTED"
                status_counts[status] += 1

                audit.write_result(
                    self._build_audit_row(
                        run_id=run_id,
                        row=row,
                        status=status,
                        normalized=normalized,
                        review_status=review_status,
                        already_promoted_store_location_v2_id=(
                            already_promoted_store_location_v2_id
                        ),
                        failure_reason=status,
                    )
                )
                continue

            if review_status in {"pending", "deleted"}:
                skipped_due_to_review += 1
                status = "SKIPPED_MANUAL_REVIEW"
                status_counts[status] += 1

                audit.write_result(
                    self._build_audit_row(
                        run_id=run_id,
                        row=row,
                        status=status,
                        normalized=normalized,
                        review_status=review_status,
                        already_promoted_store_location_v2_id=(
                            already_promoted_store_location_v2_id
                        ),
                        failure_reason=status,
                    )
                )
                continue

            if not geocode_address:
                failure_reason = "MISSING_ADDRESS"
                failure_details = (
                    "No address available for geocoding."
                )

                self._print_failure_debug(
                    staging_store_location_id=(
                        staging_store_location_id
                    ),
                    retailer=normalized.retailer,
                    store_id=normalized.store_number,
                    geocode_address=geocode_address,
                    failure_reason=failure_reason,
                    failure_details=failure_details,
                )

                status = failure_reason
                status_counts[status] += 1

                audit_row = self._build_audit_row(
                    run_id=run_id,
                    row=row,
                    status=status,
                    normalized=normalized,
                    review_status=review_status,
                    already_promoted_store_location_v2_id=(
                        already_promoted_store_location_v2_id
                    ),
                    failure_reason=failure_reason,
                    failure_details=failure_details,
                    promotion_error=failure_reason,
                )
                audit.write_result(audit_row)
                audit.write_failure(audit_row)

                pending_promotion_failure_rows.append(
                    self._record_promotion_failure(
                        staging_store_location_id,
                        retailer=normalized.retailer,
                        store_id=normalized.store_number,
                        normalized_full_address=(
                            geocode_address
                        ),
                        failure_reason=failure_reason,
                        failure_details=failure_details,
                    )
                )
                pending_staging_updates.append(
                    self._mark_staging_promotion_result(
                        staging_store_location_id=(
                            staging_store_location_id
                        ),
                        promoted_store_location_v2_id=None,
                        promotion_error=failure_reason,
                    )
                )

                if (
                    len(pending_promotion_failure_rows)
                    >= WRITE_BATCH_SIZE
                ):
                    promotion_failures_recorded += (
                        self._flush_buffers(
                            pending_promotion_failure_rows=(
                                pending_promotion_failure_rows
                            ),
                            pending_staging_updates=(
                                pending_staging_updates
                            ),
                            pending_failure_clear_ids=(
                                pending_failure_clear_ids
                            ),
                        )
                    )

                continue

            validation_reason = (
                ",".join(normalized.reason_codes)
                if normalized.reason_codes
                else None
            )

            if validation_reason is not None:
                failure_reason = "VALIDATION_FAILED"
                failure_details = validation_reason

                self._print_failure_debug(
                    staging_store_location_id=(
                        staging_store_location_id
                    ),
                    retailer=normalized.retailer,
                    store_id=normalized.store_number,
                    geocode_address=geocode_address,
                    failure_reason=failure_reason,
                    failure_details=failure_details,
                )

                status = failure_reason
                status_counts[status] += 1
                validation_failed += 1

                audit_row = self._build_audit_row(
                    run_id=run_id,
                    row=row,
                    status=status,
                    normalized=normalized,
                    review_status=review_status,
                    already_promoted_store_location_v2_id=(
                        already_promoted_store_location_v2_id
                    ),
                    failure_reason=failure_reason,
                    failure_details=failure_details,
                    promotion_error=validation_reason,
                )
                audit.write_result(audit_row)
                audit.write_failure(audit_row)

                pending_promotion_failure_rows.append(
                    self._record_promotion_failure(
                        staging_store_location_id,
                        retailer=normalized.retailer,
                        store_id=normalized.store_number,
                        normalized_full_address=(
                            geocode_address
                        ),
                        failure_reason=failure_reason,
                        failure_details=failure_details,
                    )
                )
                pending_staging_updates.append(
                    self._mark_staging_promotion_result(
                        staging_store_location_id=(
                            staging_store_location_id
                        ),
                        promoted_store_location_v2_id=None,
                        promotion_error=validation_reason,
                    )
                )

                if (
                    len(pending_promotion_failure_rows)
                    >= WRITE_BATCH_SIZE
                ):
                    promotion_failures_recorded += (
                        self._flush_buffers(
                            pending_promotion_failure_rows=(
                                pending_promotion_failure_rows
                            ),
                            pending_staging_updates=(
                                pending_staging_updates
                            ),
                            pending_failure_clear_ids=(
                                pending_failure_clear_ids
                            ),
                        )
                    )

                continue

            (
                latitude,
                longitude,
                geocode_confidence,
                geocode_provider,
                geocode_failure_reason,
                geocode_failure_details,
            ) = geocode_store(
                retailer=normalized.retailer or "",
                zip_code=normalized.zip_code or "",
                address=geocode_address,
                geocoder=self._geocoder,
            )

            if latitude is None or longitude is None:
                failure_reason = (
                    geocode_failure_reason
                    or "GEOCODE_FAILED"
                )
                failure_details = (
                    geocode_failure_details
                    or (
                        "Nominatim and Geoapify returned "
                        "no coordinates."
                    )
                )

                self._print_failure_debug(
                    staging_store_location_id=(
                        staging_store_location_id
                    ),
                    retailer=normalized.retailer,
                    store_id=normalized.store_number,
                    geocode_address=geocode_address,
                    failure_reason=failure_reason,
                    failure_details=failure_details,
                )

                status = failure_reason
                status_counts[status] += 1
                geocode_failed += 1
                rows_geocode_failed += 1
                rows_still_missing_coordinates += 1

                audit_row = self._build_audit_row(
                    run_id=run_id,
                    row=row,
                    status=status,
                    normalized=normalized,
                    review_status=review_status,
                    already_promoted_store_location_v2_id=(
                        already_promoted_store_location_v2_id
                    ),
                    failure_reason=failure_reason,
                    failure_details=failure_details,
                    promotion_error=failure_reason,
                )
                audit.write_result(audit_row)
                audit.write_failure(audit_row)

                pending_promotion_failure_rows.append(
                    self._record_promotion_failure(
                        staging_store_location_id,
                        retailer=normalized.retailer,
                        store_id=normalized.store_number,
                        normalized_full_address=(
                            geocode_address
                        ),
                        failure_reason=failure_reason,
                        failure_details=failure_details,
                    )
                )
                pending_staging_updates.append(
                    self._mark_staging_promotion_result(
                        staging_store_location_id=(
                            staging_store_location_id
                        ),
                        promoted_store_location_v2_id=None,
                        promotion_error=failure_reason,
                    )
                )

                if (
                    len(pending_promotion_failure_rows)
                    >= WRITE_BATCH_SIZE
                ):
                    promotion_failures_recorded += (
                        self._flush_buffers(
                            pending_promotion_failure_rows=(
                                pending_promotion_failure_rows
                            ),
                            pending_staging_updates=(
                                pending_staging_updates
                            ),
                            pending_failure_clear_ids=(
                                pending_failure_clear_ids
                            ),
                        )
                    )

                continue

            rows_geocoded_successfully += 1
            rows_ready_for_promotion += 1

            provider_base = self._provider_base(
                geocode_provider
            )

            if provider_base == "nominatim":
                rows_geocoded_by_primary_provider += 1
            elif provider_base == "geoapify":
                rows_geocoded_by_fallback_provider += 1
            elif provider_base == "cache":
                rows_geocoded_by_cached_provider += 1

            if geocode_confidence:
                geocode_confidence_counts[
                    geocode_confidence
                ] += 1

            if geocode_provider:
                geocode_provider_counts[
                    geocode_provider
                ] += 1

            payload = normalized.to_store_locations_payload(
                source=PROMOTION_SOURCE,
                latitude=latitude,
                longitude=longitude,
                geocode_source=geocode_provider,
                geocode_confidence=geocode_confidence,
                geocoded_at=datetime.now(timezone.utc),
                osm_id=None,
                show_on_map=True,
            )

            try:
                insert_response = (
                    self._supabase.table(
                        STORE_LOCATIONS_TABLE
                    )
                    .insert(payload)
                    .execute()
                )
                inserted_rows = insert_response.data or []

                if not inserted_rows:
                    raise RuntimeError(
                        "store_locations insert returned no rows."
                    )

                store_location_id = int(
                    inserted_rows[0]["id"]
                )

                pending_failure_clear_ids.add(
                    staging_store_location_id
                )
                pending_staging_updates.append(
                    self._mark_staging_promotion_result(
                        staging_store_location_id=(
                            staging_store_location_id
                        ),
                        promoted_store_location_v2_id=(
                            store_location_id
                        ),
                        promotion_error=None,
                    )
                )

                inserted += 1
                already_promoted_staging_ids.add(
                    staging_store_location_id
                )

                status = "SUCCESS"
                status_counts[status] += 1

                audit.write_result(
                    self._build_audit_row(
                        run_id=run_id,
                        row=row,
                        status=status,
                        normalized=normalized,
                        review_status=review_status,
                        already_promoted_store_location_v2_id=(
                            already_promoted_store_location_v2_id
                        ),
                        payload=payload,
                        latitude=latitude,
                        longitude=longitude,
                        geocode_confidence=(
                            geocode_confidence
                        ),
                        geocode_provider=geocode_provider,
                        store_location_id=store_location_id,
                    )
                )

            except Exception as exc:  # noqa: BLE001
                failure_reason = (
                    "STORE_LOCATION_INSERT_FAILED"
                )
                failure_details = str(exc)

                print("=" * 80)
                print("STORE_LOCATION INSERT PAYLOAD")
                print("=" * 80)

                debug_payload = {
                    "retailer": payload.get("retailer"),
                    "retailer_key": payload.get(
                        "retailer_key"
                    ),
                    "store_name": payload.get("store_name"),
                    "store_id": payload.get("store_id"),
                    "source": payload.get("source"),
                    "latitude": payload.get("latitude"),
                    "longitude": payload.get("longitude"),
                }

                print(
                    json.dumps(
                        debug_payload,
                        indent=2,
                        ensure_ascii=False,
                    )
                )
                print("=" * 80)

                if hasattr(exc, "args"):
                    print("args:")
                    print(exc.args)

                if hasattr(exc, "message"):
                    print("message:")
                    print(exc.message)

                print("=" * 80)

                self._print_failure_debug(
                    staging_store_location_id=(
                        staging_store_location_id
                    ),
                    retailer=normalized.retailer,
                    store_id=normalized.store_number,
                    geocode_address=geocode_address,
                    failure_reason=failure_reason,
                    failure_details=failure_details,
                )

                status = failure_reason
                status_counts[status] += 1
                insert_failed += 1

                audit_row = self._build_audit_row(
                    run_id=run_id,
                    row=row,
                    status=status,
                    normalized=normalized,
                    review_status=review_status,
                    already_promoted_store_location_v2_id=(
                        already_promoted_store_location_v2_id
                    ),
                    payload=payload,
                    latitude=latitude,
                    longitude=longitude,
                    geocode_confidence=geocode_confidence,
                    geocode_provider=geocode_provider,
                    failure_reason=failure_reason,
                    failure_details=failure_details,
                    promotion_error=(
                        f"{failure_reason}:"
                        f"{exc.__class__.__name__}"
                    ),
                )
                audit.write_result(audit_row)
                audit.write_failure(audit_row)

                pending_promotion_failure_rows.append(
                    self._record_promotion_failure(
                        staging_store_location_id,
                        retailer=normalized.retailer,
                        store_id=normalized.store_number,
                        normalized_full_address=(
                            geocode_address
                        ),
                        failure_reason=failure_reason,
                        failure_details=failure_details,
                    )
                )
                pending_staging_updates.append(
                    self._mark_staging_promotion_result(
                        staging_store_location_id=(
                            staging_store_location_id
                        ),
                        promoted_store_location_v2_id=None,
                        promotion_error=(
                            f"{failure_reason}:"
                            f"{exc.__class__.__name__}"
                        ),
                    )
                )

            if (
                len(pending_staging_updates)
                >= WRITE_BATCH_SIZE
                or len(pending_failure_clear_ids)
                >= WRITE_BATCH_SIZE
                or len(pending_promotion_failure_rows)
                >= WRITE_BATCH_SIZE
            ):
                promotion_failures_recorded += (
                    self._flush_buffers(
                        pending_promotion_failure_rows=(
                            pending_promotion_failure_rows
                        ),
                        pending_staging_updates=(
                            pending_staging_updates
                        ),
                        pending_failure_clear_ids=(
                            pending_failure_clear_ids
                        ),
                    )
                )

        promotion_failures_recorded += (
            self._flush_buffers(
                pending_promotion_failure_rows=(
                    pending_promotion_failure_rows
                ),
                pending_staging_updates=(
                    pending_staging_updates
                ),
                pending_failure_clear_ids=(
                    pending_failure_clear_ids
                ),
            )
        )

        run_finished_at = datetime.now(timezone.utc)

        summary = {
            "run_id": run_id,
            "run_started_at_utc": (
                run_started_at.isoformat()
            ),
            "run_finished_at_utc": (
                run_finished_at.isoformat()
            ),
            "elapsed_seconds": round(
                (
                    run_finished_at
                    - run_started_at
                ).total_seconds(),
                3,
            ),
            "output_dir": str(run_output_dir),
            "output_files": {
                "results_csv": str(audit.results_path),
                "failures_csv": str(audit.failures_path),
                "summary_json": str(audit.summary_path),
            },
            "target_retailer": self.target_retailer,
            "test_staging_ids": sorted(
                self.test_staging_ids
            ),
            "legacy_store_location_cutoff_id": (
                legacy_store_location_cutoff
            ),
            "staging_rows_fetched": len(staging_rows),
            "staging_rows_considered": (
                staging_rows_considered
            ),
            "records_with_valid_addresses": (
                rows_with_valid_addresses
            ),
            "records_without_address": (
                rows_without_address
            ),
            "records_geocoded_successfully": (
                rows_geocoded_successfully
            ),
            "records_geocoded_by_primary_provider": (
                rows_geocoded_by_primary_provider
            ),
            "records_geocoded_by_fallback_provider": (
                rows_geocoded_by_fallback_provider
            ),
            "records_geocoded_by_cached_provider": (
                rows_geocoded_by_cached_provider
            ),
            "records_still_missing_coordinates": (
                rows_still_missing_coordinates
            ),
            "records_ready_for_promotion": (
                rows_ready_for_promotion
            ),
            "records_geocode_failed": (
                rows_geocode_failed
            ),
            "inserted_into_store_locations": inserted,
            "skipped_already_promoted": (
                skipped_already_promoted
            ),
            "skipped_due_to_review": (
                skipped_due_to_review
            ),
            "skipped_target_retailer": (
                skipped_target_retailer
            ),
            "validation_failed": validation_failed,
            "geocode_failed": geocode_failed,
            "insert_failed": insert_failed,
            "promotion_failures_recorded": (
                promotion_failures_recorded
            ),
            "status_counts": dict(status_counts),
            "geocode_provider_counts": dict(
                geocode_provider_counts
            ),
            "geocode_confidence_counts": dict(
                geocode_confidence_counts
            ),
        }

        audit.write_summary(summary)
        audit.close()

        print()
        print("=" * 72)
        print("Promote Staging Store Locations Summary")
        print("=" * 72)
        print(
            f"Inserted into store_locations: {inserted}"
        )
        print(
            "Skipped because already promoted: "
            f"{skipped_already_promoted}"
        )
        print(
            "Skipped because manual review pending/deleted: "
            f"{skipped_due_to_review}"
        )
        print(
            "Skipped because retailer mismatch: "
            f"{skipped_target_retailer}"
        )
        print(
            f"Validation failed: {validation_failed}"
        )
        print(f"Geocode failed: {geocode_failed}")
        print(f"Insert failed: {insert_failed}")
        print(
            "Promotion failures recorded: "
            f"{promotion_failures_recorded}"
        )
        print(f"Output directory: {run_output_dir}")