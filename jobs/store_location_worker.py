# jobs/store_location_worker.py
"""Continuous Railway worker for flyer_deals store-location attribution."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config.supabase import supabase
from jobs.backfill_store_ids import run_backfill
from services.store_location_matcher import SOURCE_RETAILER_KEY_OVERRIDES

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("STORE_LOCATION_WORKER")

LOCAL_TZ = ZoneInfo(os.getenv("STORE_LOCATION_WEEK_TIMEZONE", "America/Los_Angeles"))
STORE_KEY_PAGE_SIZE = 1000
ACTIVE_SLEEP_SECONDS = float(os.getenv("STORE_LOCATION_ACTIVE_SLEEP_SECONDS", "5"))
IDLE_SLEEP_SECONDS = float(os.getenv("STORE_LOCATION_IDLE_SLEEP_SECONDS", "300"))
ERROR_SLEEP_SECONDS = float(os.getenv("STORE_LOCATION_ERROR_SLEEP_SECONDS", "15"))
WEEKLY_ONLY = os.getenv("STORE_LOCATION_WEEKLY_ONLY", "true").lower() not in {
    "0",
    "false",
    "no",
}


def latest_wednesday_start_utc(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    local = current.astimezone(LOCAL_TZ)
    days_since_wednesday = (local.weekday() - 2) % 7
    start_local = (local - timedelta(days=days_since_wednesday)).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    return start_local.astimezone(timezone.utc)


def load_store_retailer_keys() -> set[str]:
    keys: set[str] = set()
    offset = 0

    while True:
        result = (
            supabase.table("store_locations")
            .select("retailer_key")
            .order("id")
            .range(offset, offset + STORE_KEY_PAGE_SIZE - 1)
            .execute()
        )
        page = result.data or []
        for row in page:
            key = str(row.get("retailer_key") or "").strip().lower()
            if key:
                keys.add(key)

        if len(page) < STORE_KEY_PAGE_SIZE:
            break
        offset += STORE_KEY_PAGE_SIZE

    return keys


def source_keys_to_process(store_keys: set[str]) -> list[str]:
    # Normal source keys match store keys. Versioned source keys are explicitly
    # added only when their target store key exists.
    keys = set(store_keys)
    for source_key, target_key in SOURCE_RETAILER_KEY_OVERRIDES.items():
        if target_key in store_keys:
            keys.add(source_key)
    return sorted(keys)


def run_cycle() -> dict:
    store_keys = load_store_retailer_keys()
    source_keys = source_keys_to_process(store_keys)
    week_start = latest_wednesday_start_utc() if WEEKLY_ONLY else None
    created_after = week_start.isoformat() if week_start else None

    logger.info(
        "Starting cycle, source_keys=%s, store_keys=%s, created_after=%s",
        len(source_keys),
        len(store_keys),
        created_after,
    )

    totals = {
        "processed": 0,
        "matched": 0,
        "no_match": 0,
        "errors": 0,
        "retailers_with_matches": 0,
        "retailer_failures": 0,
    }

    for source_key in source_keys:
        try:
            stats = run_backfill(
                retailer_filter=source_key,
                only_unmatched=True,
                created_after=created_after,
            )
        except Exception:
            # A statement timeout or one retailer-specific failure should not
            # restart the entire cycle and reload the full store catalog. Skip
            # the failing retailer for this pass and retry it next cycle.
            totals["errors"] += 1
            totals["retailer_failures"] += 1
            logger.exception(
                "retailer=%s failed during backfill; continuing with next retailer",
                source_key,
            )
            continue

        totals["processed"] += int(stats.get("processed", 0))
        totals["matched"] += int(stats.get("matched", 0))
        totals["no_match"] += int(stats.get("no_match", 0))
        totals["errors"] += int(stats.get("errors", 0))

        if stats.get("matched", 0):
            totals["retailers_with_matches"] += 1
            logger.info(
                "retailer=%s matched=%s processed=%s no_match=%s",
                source_key,
                stats.get("matched", 0),
                stats.get("processed", 0),
                stats.get("no_match", 0),
            )

    logger.info("Cycle complete: %s", totals)
    return totals


def main() -> None:
    logger.info(
        "Store-location worker started, weekly_only=%s, max_nearby_miles=%s",
        WEEKLY_ONLY,
        os.getenv("STORE_LOCATION_MAX_NEARBY_MILES", "15"),
    )

    while True:
        try:
            totals = run_cycle()
            sleep_for = (
                ACTIVE_SLEEP_SECONDS if totals["matched"] > 0 else IDLE_SLEEP_SECONDS
            )
            logger.info("Sleeping %.1fs before next cycle", sleep_for)
            time.sleep(sleep_for)
        except KeyboardInterrupt:
            logger.info("Worker interrupted, exiting")
            return
        except Exception:
            logger.exception("Worker cycle failed")
            time.sleep(ERROR_SLEEP_SECONDS)


if __name__ == "__main__":
    main()
