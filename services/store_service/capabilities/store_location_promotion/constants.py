# services/store_service/capabilities/store_location_promotion/constants.py

from __future__ import annotations

from pathlib import Path


STAGING_TABLE = "staging_store_locations"
MANUAL_REVIEW_TABLE = "staging_store_locations_manual_review"
STORE_LOCATIONS_TABLE = "store_locations_v2"
PROMOTION_FAILURE_TABLE = "staging_store_location_promotion_failures"

# Promoted canonical store locations originate from retailer-provided data.
PROMOTION_SOURCE = "retailer"

PROMOTION_ROOT = Path(__file__).resolve().parent
OUTPUT_ROOT = PROMOTION_ROOT / "_output"

GEOCODER_CACHE_PATH = (
    Path(__file__).resolve().parents[4] / "data" / "geocode_cache.json"
)

# Batch staging-side writes to reduce Supabase round trips on large runs.
WRITE_BATCH_SIZE = 200