# Prox data flow and integrity checkpoints

```text
Retailer/source collectors
  Kroger API | Whole Foods/Aldi/Harris Teeter acquisition strategies | other scrapers
        |
        v
jobs/pipeline_ingest.py
  normalize retailer + ZIP/address -> services/store_matching.py
  resolve store_id / match_confidence / candidate stores
        |
        v
public.flyer_deals
  product_name, price, size, retailer, store_id, processed_at
        |
        +--> scoring/product_normalizer.py
        |      brand/category/organic + base_amount/base_unit
        |      canonical_product_name + match_key
        |
        +--> supabase price-history sync
        |      match_key + store_id + observed_date
        |
        +--> API /search and /deals
        |      canonical identity/projection, positive price,
        |      nearby resolved stores; fallback when identity is absent
        |
        +--> mobile flyer browse
        |      flyer_deals -> normalizeFlyerDeals -> Deals/cart surfaces
        |
        +--> cart optimizer
               product-name matching -> store totals -> distance penalty

Canonical v2.7 audit boundary:
  source_product classification RPCs
    -> incremental canonical projection/finalization
    -> best_deals_comprehensive projection
    -> API response mapping
    -> Search / Deals / Cart visibility

The audit compares flyer_deals identity/brand/price to the projection view,
then applies each surface's real eligibility predicates. This distinguishes
"bad source row", "projection mismatch", and "correct but not surfaceable".

Primary integrity checkpoints:
  source completeness -> retailer key/address -> store resolution/GPS
  canonical identity -> brand/category/size enrichment -> valid price
  freshness -> search visibility -> cart correctness
```

The production baseline is read-only. The four-retailer QA sample is generated
deterministically by `run_baseline.py`; reviewers label each row as
`correct`, `incorrect`, or `uncertain` and assign one issue type.
