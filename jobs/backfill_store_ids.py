# jobs/backfill_store_ids.py
# Backfills store_id + Phase 3 store-match fields for existing flyer_deals rows.
# By default only processes rows where store_id IS NULL.
#
# Safe behavior:
# - Uses flyer_deals.retailer_key as the authoritative source key.
# - Never collapses distinct banners (QFC, Ralphs, Mariano's, FoodsCo, etc.).
# - Never creates synthetic store_locations rows.
# - Existing non-null store_id values are not overwritten unless --all-rows is used.
# - Does not write candidate_store_ids because the legacy DB column is uuid[]
#   while public.store_locations.id is integer. store_id remains authoritative.
#
# Usage:
#   PYTHONPATH=. python jobs/backfill_store_ids.py
#   PYTHONPATH=. python jobs/backfill_store_ids.py --retailer ralphs
#   PYTHONPATH=. python jobs/backfill_store_ids.py --created-after 2026-08-26T07:00:00+00:00
#   PYTHONPATH=. python jobs/backfill_store_ids.py --dry-run

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor

from config.supabase import supabase
from services.store_location_matcher import (
    find_store_for_deal,
    get_match_cache_stats,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("BACKFILL")

DEFAULT_PAGE_SIZE = int(os.getenv("STORE_LOCATION_PAGE_SIZE", "1000"))
BATCH_WRITE_MAX = int(os.getenv("STORE_LOCATION_WRITE_CHUNK", "50"))
MIN_CHUNK = 1
TIMEOUT_CODE = "57014"


def _fetch_page(
    last_id: int,
    retailer_filter: str | None,
    only_unmatched: bool,
    created_after: str | None,
    page_size: int,
) -> list:
    q = (
        supabase.table("flyer_deals")
        .select("id, retailer_key, retailer, zip_code, store_lat, store_lng")
        .gt("id", last_id)
        .order("id")
        .limit(page_size)
    )

    if retailer_filter:
        q = q.eq("retailer_key", retailer_filter)
    if only_unmatched:
        q = q.is_("store_id", "null")
    if created_after:
        q = q.gte("created_at", created_after)

    return q.execute().data or []


def _build_payload(match) -> dict:
    # candidate_store_ids is intentionally omitted here. The current production
    # column is uuid[] but public.store_locations.id, flyer_deals.store_id, and
    # the matcher all use integer IDs. Including integer candidates causes
    # Postgres to reject the entire PATCH, including the valid store_id.
    # Keep the chosen store_id plus match metadata now. Candidate re-resolution
    # can be restored after the candidate ID schema is migrated deliberately.
    return {
        "store_id": match.store_id,
        "match_confidence": match.match_confidence,
        "candidate_store_count": match.candidate_store_count,
        "matched_by": match.matched_by,
    }


def _write_chunk(ids: list, payload: dict, depth: int = 0) -> tuple[int, int]:
    if not ids:
        return 0, 0

    try:
        supabase.table("flyer_deals").update(payload).in_("id", ids).execute()
        return len(ids), 0

    except Exception as exc:
        err_str = str(exc)
        code = getattr(exc, "code", None) or (
            exc.args[0].get("code")
            if exc.args and isinstance(exc.args[0], dict)
            else None
        )

        if TIMEOUT_CODE in err_str or code == TIMEOUT_CODE:
            if len(ids) <= MIN_CHUNK:
                logger.error("Timeout on single row id=%s, skipping", ids[0])
                return 0, 1

            mid = len(ids) // 2
            left, right = ids[:mid], ids[mid:]
            logger.warning(
                "Timeout on %s-row chunk (depth=%s), splitting into %s + %s",
                len(ids),
                depth,
                len(left),
                len(right),
            )
            time.sleep(0.5 * (depth + 1))
            s1, e1 = _write_chunk(left, payload, depth + 1)
            s2, e2 = _write_chunk(right, payload, depth + 1)
            return s1 + s2, e1 + e2

        logger.error("Batch update failed (%s rows): %s", len(ids), exc)
        return 0, len(ids)


def _write_batch(matched_pairs: list[tuple], dry_run: bool) -> tuple[int, int]:
    groups: dict[str, list] = defaultdict(list)

    for deal_id, match in matched_pairs:
        payload = _build_payload(match)
        key = json.dumps(payload, sort_keys=True, default=str)
        groups[key].append((deal_id, payload))

    success = 0
    errors = 0

    for items in groups.values():
        ids = [item[0] for item in items]
        payload = items[0][1]

        if dry_run:
            logger.info("[DRY-RUN] %s rows -> %s", len(ids), payload)
            success += len(ids)
            continue

        for i in range(0, len(ids), BATCH_WRITE_MAX):
            chunk = ids[i : i + BATCH_WRITE_MAX]
            s, e = _write_chunk(chunk, payload)
            success += s
            errors += e

    return success, errors


def _process_page(page: list, stats: dict) -> list[tuple]:
    matched_pairs = []

    for deal in page:
        stats["processed"] += 1
        source_key = (deal.get("retailer_key") or "").strip()
        retailer_raw = deal.get("retailer") or source_key
        zip_code = str(deal.get("zip_code") or "").strip()

        if not source_key:
            stats["skipped_null_retailer"] += 1
            continue
        if not zip_code:
            stats["skipped_null_zip"] += 1
            continue

        try:
            match = find_store_for_deal(
                retailer_key=source_key,
                retailer_raw=retailer_raw,
                zip_code=zip_code,
                deal_lat=deal.get("store_lat"),
                deal_lng=deal.get("store_lng"),
            )
            if match.store_id is not None:
                matched_pairs.append((deal["id"], match))
                stats[match.match_confidence] = (
                    stats.get(match.match_confidence, 0) + 1
                )
            else:
                stats["no_match"] += 1
                stats["no_match_reasons"][match.matched_by] = (
                    stats["no_match_reasons"].get(match.matched_by, 0) + 1
                )
        except Exception as exc:
            logger.exception("Store match error on flyer_deals.id=%s: %s", deal["id"], exc)
            stats["errors"] += 1

    return matched_pairs


def run_backfill(
    retailer_filter: str | None = None,
    dry_run: bool = False,
    only_unmatched: bool = True,
    start_id: int = 0,
    created_after: str | None = None,
    page_size: int | None = None,
    max_pages: int | None = None,
) -> dict:
    """Run a restart-safe store-location backfill.

    created_after should be an ISO-8601 timestamptz string when supplied.
    """
    effective_page_size = page_size or DEFAULT_PAGE_SIZE
    stats = {
        "processed": 0,
        "skipped_null_retailer": 0,
        "skipped_null_zip": 0,
        "matched": 0,
        "zip_single": 0,
        "zip_multi": 0,
        "nearby_single": 0,
        "nearby_multi": 0,
        "no_match": 0,
        "errors": 0,
        "no_match_reasons": {},
    }

    last_id = start_id
    page_num = 0
    total_rows = 0

    if start_id:
        logger.info("Resuming from id > %s", start_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        fetch_future: Future = executor.submit(
            _fetch_page,
            last_id,
            retailer_filter,
            only_unmatched,
            created_after,
            effective_page_size,
        )

        while True:
            page = fetch_future.result()
            if not page:
                break

            page_num += 1
            total_rows += len(page)
            next_last_id = page[-1]["id"]
            is_last = len(page) < effective_page_size

            logger.info(
                "Page %s, retailer=%s, fetched=%s after id=%s, total=%s",
                page_num,
                retailer_filter or "*",
                len(page),
                last_id,
                total_rows,
            )

            if not is_last and not (max_pages and page_num >= max_pages):
                fetch_future = executor.submit(
                    _fetch_page,
                    next_last_id,
                    retailer_filter,
                    only_unmatched,
                    created_after,
                    effective_page_size,
                )

            matched_pairs = _process_page(page, stats)

            if matched_pairs:
                success, errors = _write_batch(matched_pairs, dry_run)
                stats["matched"] += success
                stats["errors"] += errors
                logger.info(
                    "Page %s wrote %s matched rows%s",
                    page_num,
                    success,
                    f", {errors} write errors" if errors else "",
                )

            last_id = next_last_id

            if is_last or (max_pages and page_num >= max_pages):
                break

    stats["pages"] = page_num
    stats["last_id"] = last_id
    stats["cache"] = get_match_cache_stats()
    logger.info("Done retailer=%s: %s", retailer_filter or "*", stats)
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill store_id on flyer_deals")
    parser.add_argument(
        "--retailer",
        default=None,
        help="Limit to one flyer_deals.retailer_key, e.g. ralphs or qfc",
    )
    parser.add_argument(
        "--start-id",
        type=int,
        default=0,
        help="Resume from this flyer_deals.id (exclusive)",
    )
    parser.add_argument(
        "--created-after",
        default=None,
        help="Only process rows created at/after this ISO-8601 timestamptz",
    )
    parser.add_argument(
        "--all-rows",
        action="store_true",
        help="Reprocess already-matched rows",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log payloads without writing to the database",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Optional cap for validation runs",
    )
    args = parser.parse_args()

    run_backfill(
        retailer_filter=args.retailer,
        dry_run=args.dry_run,
        only_unmatched=not args.all_rows,
        start_id=args.start_id,
        created_after=args.created_after,
        max_pages=args.max_pages,
    )
