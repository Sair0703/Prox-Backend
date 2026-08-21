# jobs/backfill_store_ids.py
# Backfills store_id + all Phase 3 match fields for existing flyer_deals rows.
# By default only processes rows where store_id IS NULL (safe, fast).
# Use --all-rows to reprocess already-matched rows (e.g. after a logic fix).
#
# Restart-safe: the default filter is store_id IS NULL, so already-matched rows
# are never touched again — no duplicates possible on restart.
#
# Usage:
#   PYTHONPATH=. python jobs/backfill_store_ids.py
#   PYTHONPATH=. python jobs/backfill_store_ids.py --retailer kroger
#   PYTHONPATH=. python jobs/backfill_store_ids.py --start-id 1817081
#   PYTHONPATH=. python jobs/backfill_store_ids.py --dry-run

import argparse
import json
import logging
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, Future

from config.supabase import supabase
from services.store_matching import find_store_for_deal, get_match_cache_stats

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("BACKFILL")

PAGE_SIZE       = 1000  # rows fetched per page
BATCH_WRITE_MAX = 25    # max IDs per in_() call — small enough to never timeout
RATE_LIMIT      = 0.0   # no artificial sleep needed; fetch/write overlap handles pacing
MIN_CHUNK       = 1     # floor when auto-halving on timeout

TIMEOUT_CODE    = "57014"  # PostgreSQL statement_timeout


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def _fetch_page(last_id: int, retailer_filter, only_unmatched) -> list:
    q = (
        supabase.table("flyer_deals")
        .select("id, retailer_key, retailer, zip_code")
        .gt("id", last_id)
        .order("id")
        .limit(PAGE_SIZE)
    )
    if retailer_filter:
        q = q.or_(
            f"retailer_key.eq.{retailer_filter},"
            f"retailer.eq.{retailer_filter}"
        )
    if only_unmatched:
        q = q.is_("store_id", "null")
    return q.execute().data or []


# ---------------------------------------------------------------------------
# Build payload
# ---------------------------------------------------------------------------

def _build_payload(match) -> dict:
    return {
        "store_id":              match.retailer_store_id,
        "match_confidence":      match.match_confidence,
        "candidate_store_count": match.candidate_store_count,
        "matched_by":            match.matched_by,
        "candidate_store_ids": (
            match.candidate_store_ids
            if match.match_confidence == "zip_multi" and match.candidate_store_ids
            else None
        ),
    }


# ---------------------------------------------------------------------------
# Chunk writer — retries with halved size on statement_timeout (57014)
# ---------------------------------------------------------------------------

def _write_chunk(ids: list, payload: dict, depth: int = 0) -> tuple[int, int]:
    """
    Write a list of IDs with the given payload.
    On a 57014 timeout, splits in half and retries each piece recursively.
    Returns (success_count, error_count).
    """
    if not ids:
        return 0, 0

    try:
        supabase.table("flyer_deals").update(payload).in_("id", ids).execute()
        return len(ids), 0

    except Exception as e:
        err_str = str(e)
        code    = getattr(e, "code", None) or (
            e.args[0].get("code") if e.args and isinstance(e.args[0], dict) else None
        )

        # Statement timeout — halve and retry
        if TIMEOUT_CODE in err_str or code == TIMEOUT_CODE:
            if len(ids) <= MIN_CHUNK:
                logger.error(f"Timeout on single row id={ids[0]} — skipping")
                return 0, 1

            mid = len(ids) // 2
            left, right = ids[:mid], ids[mid:]
            logger.warning(
                f"Timeout on {len(ids)}-row chunk (depth={depth}) — "
                f"splitting into {len(left)} + {len(right)}"
            )
            time.sleep(0.5 * (depth + 1))  # back off proportionally
            s1, e1 = _write_chunk(left,  payload, depth + 1)
            s2, e2 = _write_chunk(right, payload, depth + 1)
            return s1 + s2, e1 + e2

        # Non-timeout error — log and skip
        logger.error(f"Batch update failed ({len(ids)} rows): {e}")
        return 0, len(ids)


# ---------------------------------------------------------------------------
# Batch write — groups rows by identical payload, calls _write_chunk per group
# ---------------------------------------------------------------------------

def _write_batch(matched_pairs: list[tuple], dry_run: bool) -> tuple[int, int]:
    """
    Groups (deal_id, match) pairs by identical payload.
    Sends one UPDATE per group using id=in.(…), chunked to BATCH_WRITE_MAX.
    Returns (success_count, error_count).
    """
    groups: dict[str, list] = defaultdict(list)
    for deal_id, match in matched_pairs:
        payload = _build_payload(match)
        key = json.dumps(payload, sort_keys=True, default=str)
        groups[key].append((deal_id, payload))

    success = 0
    errors  = 0

    for key, items in groups.items():
        ids     = [item[0] for item in items]
        payload = items[0][1]

        if dry_run:
            logger.info(f"[DRY-RUN] {len(ids)} rows → {payload}")
            success += len(ids)
            continue

        # Chunk to BATCH_WRITE_MAX, retry each chunk on timeout
        for i in range(0, len(ids), BATCH_WRITE_MAX):
            chunk = ids[i : i + BATCH_WRITE_MAX]
            s, e  = _write_chunk(chunk, payload)
            success += s
            errors  += e

    return success, errors


# ---------------------------------------------------------------------------
# Process a single page — matching logic only, no I/O
# ---------------------------------------------------------------------------

def _process_page(page: list, stats: dict) -> list[tuple]:
    matched_pairs = []
    for deal in page:
        stats["processed"] += 1
        rkey  = deal.get("retailer_key") or deal.get("retailer") or ""
        zcode = deal.get("zip_code") or ""

        if not rkey:
            logger.warning(f"{deal['id']} has no retailer info — skipping")
            stats["skipped_null"] += 1
            continue

        try:
            match = find_store_for_deal(
                retailer_raw=rkey,
                zip_code=zcode,
                city=None,
                state=None,
                address=None,
            )
            if match.store_id:
                matched_pairs.append((deal["id"], match))
                conf = match.match_confidence
                if conf in stats:
                    stats[conf] += 1
            else:
                stats["no_match"] += 1
        except Exception as e:
            logger.error(f"Error on {deal['id']}: {e}")
            stats["errors"] += 1

    return matched_pairs


# ---------------------------------------------------------------------------
# Main loop  (fetch page N+1 while writing page N)
# ---------------------------------------------------------------------------

def run_backfill(
    retailer_filter=None,
    dry_run=False,
    only_unmatched=True,
    start_id=0,
) -> dict:
    """
    Restart-safe: default filter is store_id IS NULL.
    Already-matched rows are skipped automatically — no duplicates on restart.
    """
    stats = {
        "processed":    0,
        "skipped_null": 0,
        "matched":      0,
        "zip_single":   0,
        "zip_multi":    0,
        "city_state":   0,
        "created":      0,
        "no_match":     0,
        "errors":       0,
    }

    last_id = start_id
    if start_id:
        logger.info(f"Resuming from id > {start_id}")

    page_num   = 0
    total_rows = 0

    with ThreadPoolExecutor(max_workers=2) as executor:
        # Kick off the first fetch
        fetch_future: Future = executor.submit(
            _fetch_page, last_id, retailer_filter, only_unmatched
        )

        while True:
            page = fetch_future.result()
            if not page:
                break

            page_num   += 1
            total_rows += len(page)
            next_last_id = page[-1]["id"]
            is_last      = len(page) < PAGE_SIZE

            logger.info(
                f"Page {page_num} — fetched {len(page)} rows after id={last_id} "
                f"({'unmatched only' if only_unmatched else 'all rows'}) "
                f"| total so far: {total_rows}"
            )

            # Start fetching the NEXT page immediately (overlaps with write below)
            if not is_last:
                fetch_future = executor.submit(
                    _fetch_page, next_last_id, retailer_filter, only_unmatched
                )

            # Match + write current page (runs while next fetch is in-flight)
            matched_pairs = _process_page(page, stats)

            if matched_pairs:
                n_groups = len(
                    set(
                        json.dumps(_build_payload(m), sort_keys=True, default=str)
                        for _, m in matched_pairs
                    )
                )
                success, errors = _write_batch(matched_pairs, dry_run)
                stats["matched"] += success
                stats["errors"]  += errors
                logger.info(
                    f"Page {page_num} wrote {success} rows across {n_groups} group(s)"
                    + (f" — {errors} errors" if errors else "")
                )

            last_id = next_last_id

            if is_last:
                break

    stats["cache"] = get_match_cache_stats()
    logger.info(f"Done: {stats}")
    return stats


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill store_id on flyer_deals")
    parser.add_argument("--retailer",  default=None,
                        help="Limit to one retailer e.g. kroger, publix")
    parser.add_argument("--start-id",  type=int, default=0,
                        help="Resume from this deal ID (exclusive)")
    parser.add_argument("--all-rows",  action="store_true",
                        help="Reprocess already-matched rows")
    parser.add_argument("--dry-run",   action="store_true",
                        help="Print payloads without writing to DB")
    args = parser.parse_args()

    run_backfill(
        retailer_filter=args.retailer,
        dry_run=args.dry_run,
        only_unmatched=not args.all_rows,
        start_id=args.start_id,
    )
