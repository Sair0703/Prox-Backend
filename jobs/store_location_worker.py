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

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("STORE_LOCATION_WORKER")

LOCAL_TZ = ZoneInfo(os.getenv("STORE_LOCATION_WEEK_TIMEZONE", "America/Los_Angeles"))
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


def _week_start_id(week_start: datetime) -> int:
    """Return an ID cursor just before the first row created this flyer week.

    The backfill table already has a partial index on id WHERE store_id IS NULL.
    Scanning unmatched rows forward from the weekly ID boundary is dramatically
    cheaper than issuing one retailer_key-filtered query per retailer, because
    retailer_key currently has no supporting backfill index.
    """
    result = (
        supabase.table("flyer_deals")
        .select("id,created_at")
        .gte("created_at", week_start.isoformat())
        .order("created_at")
        .order("id")
        .limit(1)
        .execute()
    )
    rows = result.data or []
    if not rows:
        return 0

    first_id = int(rows[0]["id"])
    # The backfill query is exclusive (id > start_id).
    return max(0, first_id - 1)


def run_cycle() -> dict:
    week_start = latest_wednesday_start_utc() if WEEKLY_ONLY else None

    if week_start is not None:
        start_id = _week_start_id(week_start)
        logger.info(
            "Starting global weekly cycle, week_start=%s, start_id=%s",
            week_start.isoformat(),
            start_id,
        )
        # Important: do not also add created_at or retailer_key filters here.
        # The efficient production path is the existing partial index
        # idx_flyer_deals_unmatched_id (id WHERE store_id IS NULL), starting at
        # the current week's ID boundary. The matcher itself keeps retailer_key
        # authoritative and loads only same-retailer store_locations candidates.
        stats = run_backfill(
            retailer_filter=None,
            only_unmatched=True,
            start_id=start_id,
            created_after=None,
        )
    else:
        logger.info("Starting global all-history unmatched cycle")
        stats = run_backfill(
            retailer_filter=None,
            only_unmatched=True,
            start_id=0,
            created_after=None,
        )

    totals = {
        "processed": int(stats.get("processed", 0)),
        "matched": int(stats.get("matched", 0)),
        "no_match": int(stats.get("no_match", 0)),
        "errors": int(stats.get("errors", 0)),
        "pages": int(stats.get("pages", 0)),
        "last_id": int(stats.get("last_id", 0)),
    }
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
