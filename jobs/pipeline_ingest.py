# jobs/pipeline_ingest.py
# Ingests new flyer deals and writes all Phase 3 fields at insert time.
# Called by the scraper pipeline for all new incoming deals.

import logging
import time
from datetime import datetime
from config.supabase import get_supabase_client
from services.store_matching import find_store_for_deal, get_match_cache_stats

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger   = logging.getLogger(__name__)
supabase = get_supabase_client()

BATCH_SIZE = 50
RATE_LIMIT = 0.05


def _build_match_payload(match) -> dict:
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


def ingest_deals(deals: list[dict], dry_run: bool = False) -> dict:
    """
    Upsert a list of deal dicts from any retailer source.

    Required fields per dict:
        product_name,
        retailer_key (or retailer_name), zip_code

    Optional:
        product_price, image_link,
        product_size, coupon_detail,
        retailer_address, flyer_id,
        city, state, address, latitude, longitude

    dry_run=True  → runs store matching (reads only) but skips all
                    writes to flyer_deals.  Safe to use in CI or ad-hoc
                    checks without polluting the table.
    """
    stats = {
        "total":      len(deals),
        "inserted":   0,
        "no_store":   0,
        "errors":     0,
        "dry_run":    dry_run,
        "started_at": datetime.utcnow().isoformat(),
    }

    batch: list[dict] = []

    for i, raw in enumerate(deals, 1):
        try:
            retailer_raw = raw.get("retailer_name") or raw.get("retailer_key", "")
            zip_code     = (raw.get("zip_code") or "").strip()

            match = find_store_for_deal(
                retailer_raw=retailer_raw,
                zip_code=zip_code,
                city=raw.get("city"),
                state=raw.get("state"),
                address=raw.get("address"),
                deal_lat=raw.get("latitude"),
                deal_lng=raw.get("longitude"),
            )

            if not match.store_id:
                logger.warning(
                    f"[INGEST] No store for deal {i}: {retailer_raw}/{zip_code}"
                )
                stats["no_store"] += 1

            row = {
                # ── core product fields (matched to actual flyer_deals schema) ──
                "product_name":    raw["product_name"],
                "product_price":   raw.get("product_price") or raw.get("price"),
                "image_link":      raw.get("image_link")    or raw.get("image_url"),
                "product_size":    raw.get("product_size"),
                "coupon_detail":   raw.get("coupon_detail"),
                # ── retailer / location fields ──
                "retailer":        retailer_raw,
                "retailer_key":    raw.get("retailer_key") or retailer_raw,
                "retailer_address": raw.get("retailer_address") or raw.get("address"),
                "zip_code":        zip_code,
                # ── flyer_id used as upsert conflict key ──
                # NOTE: confirm flyer_id column exists in flyer_deals before
                # running live. Query:
                #   SELECT column_name FROM information_schema.columns
                #   WHERE table_name = 'flyer_deals' AND column_name = 'flyer_id';
                "flyer_id":        raw.get("flyer_id"),
                # ── store matching fields ──
                **_build_match_payload(match),
            }

            # strip keys where value is None to avoid overwriting existing data
            row = {k: v for k, v in row.items() if v is not None}

            batch.append(row)

            if len(batch) >= BATCH_SIZE:
                if not dry_run:
                    _flush(batch, stats)
                else:
                    logger.info(f"[INGEST dry_run] would flush {len(batch)} rows")
                    stats["inserted"] += len(batch)
                batch = []
                time.sleep(RATE_LIMIT)

        except Exception as e:
            logger.error(f"[INGEST] Error on deal {i}: {e}")
            stats["errors"] += 1

    if batch:
        if not dry_run:
            _flush(batch, stats)
        else:
            logger.info(f"[INGEST dry_run] would flush {len(batch)} rows")
            stats["inserted"] += len(batch)

    stats["finished_at"]    = datetime.utcnow().isoformat()
    stats["cache_snapshot"] = get_match_cache_stats()
    logger.info(f"[INGEST] Complete: {stats}")
    return stats


def _flush(batch: list[dict], stats: dict) -> None:
    try:
        res = (
            supabase.table("flyer_deals")
            .upsert(batch, on_conflict="flyer_id")
            .execute()
        )
        count = len(res.data or [])
        stats["inserted"] += count
        logger.info(f"[INGEST] Flushed {count} rows")
    except Exception as e:
        logger.error(f"[INGEST] Flush error: {e}")
        stats["errors"] += len(batch)