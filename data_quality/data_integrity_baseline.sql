-- Prox production data-quality baseline (READ ONLY)
-- Run in the Supabase SQL editor with a read-only role.
-- No statement below mutates production data.
--
-- Assumptions confirmed from the application code:
--   flyer_deals: id, product_name, product_price, product_size, brand,
--     category, base_amount, base_unit, match_key, canonical_product_name,
--     retailer, retailer_key, store_id, match_confidence, processed_at
--   store_locations: id, retailer, retailer_key, address, zip_code,
--     latitude/longitude (or lat/lng), geocode_confidence, updated_at
-- If a deployment lacks one of the optional columns, remove that metric from
-- the SELECT rather than adding a write-side migration.

-- 1) Confirm the deployed schema before interpreting any metric.
select table_name, column_name, data_type
from information_schema.columns
where table_schema = 'public'
  and table_name in (
    'flyer_deals', 'store_locations', 'search_cache',
    'best_deals_comprehensive', 'source_products'
  )
order by table_name, ordinal_position;

-- 2) Retailer scorecard. Null rates are row counts and percentages.
with base as (
  select
    coalesce(nullif(lower(trim(retailer_key)), ''), lower(trim(retailer))) as retailer,
    product_name, product_price, product_size, brand, category,
    base_amount, base_unit, match_key, canonical_product_name,
    store_id, match_confidence, processed_at
  from public.flyer_deals
), metrics as (
  select retailer, count(*) as rows,
    count(*) filter (where nullif(trim(canonical_product_name), '') is not null) as canonical_filled,
    count(*) filter (where nullif(trim(brand), '') is not null) as brand_filled,
    count(*) filter (where nullif(trim(category), '') is not null) as category_filled,
    count(*) filter (where nullif(trim(match_key), '') is not null) as match_key_filled,
    count(*) filter (where product_price is null or product_price <= 0) as invalid_price,
    count(*) filter (where product_size is null or trim(product_size) = '') as size_missing,
    count(*) filter (where base_amount is not null and (base_amount <= 0 or base_unit is null)) as size_flagged,
    count(*) filter (where store_id is null) as store_unresolved,
    count(*) filter (where match_confidence is null or match_confidence in ('none', 'created')) as search_fallback,
    min(processed_at) as oldest_processed_at,
    max(processed_at) as newest_processed_at
  from base
  group by retailer
)
select *,
  round(100.0 * canonical_filled / nullif(rows, 0), 2) as canonical_fill_pct,
  round(100.0 * brand_filled / nullif(rows, 0), 2) as brand_fill_pct,
  round(100.0 * category_filled / nullif(rows, 0), 2) as category_fill_pct,
  round(100.0 * invalid_price / nullif(rows, 0), 2) as invalid_price_pct,
  round(100.0 * size_flagged / nullif(rows, 0), 2) as size_flagged_pct,
  round(100.0 * store_unresolved / nullif(rows, 0), 2) as store_unresolved_pct,
  round(100.0 * search_fallback / nullif(rows, 0), 2) as search_fallback_pct
from metrics
order by rows desc, retailer;

-- 3) Category conflicts: one canonical identity assigned to multiple categories.
select match_key, count(*) as row_count,
       count(distinct lower(trim(category))) filter (where category is not null) as category_count,
       array_agg(distinct category order by category) filter (where category is not null) as categories
from public.flyer_deals
where nullif(trim(match_key), '') is not null
group by match_key
having count(distinct lower(trim(category))) filter (where category is not null) > 1
order by row_count desc;

-- 4) Invalid prices and suspicious size values.
select id, retailer, retailer_key, product_name, product_price, product_size,
       base_amount, base_unit, match_key, processed_at
from public.flyer_deals
where product_price is null or product_price <= 0
   or (base_amount is not null and base_amount <= 0)
   or (base_amount is not null and nullif(trim(base_unit), '') is null)
order by processed_at desc nulls last, id
limit 1000;

-- 5) Retailer freshness (days since newest processed row).
select coalesce(nullif(lower(trim(retailer_key)), ''), lower(trim(retailer)) ) as retailer,
       count(*) as rows, max(processed_at) as newest_processed_at,
       round(extract(epoch from (now() - max(processed_at))) / 86400.0, 1) as age_days
from public.flyer_deals
group by 1
order by age_days desc nulls first;

-- 6) Store health and deal-to-store resolution.
select coalesce(nullif(lower(trim(fd.retailer_key)), ''), lower(trim(fd.retailer))) as retailer,
       count(*) as deal_rows,
       count(*) filter (where fd.store_id is not null) as resolved_deals,
       count(*) filter (where sl.id is null and fd.store_id is not null) as dangling_store_ids,
       count(distinct fd.store_id) as stores_used,
       count(distinct sl.id) filter (where sl.latitude is not null and sl.longitude is not null) as stores_with_gps
from public.flyer_deals fd
left join public.store_locations sl on sl.id = fd.store_id
group by 1
order by deal_rows desc;

-- 7) Search fallback candidates: rows the API cannot represent as a canonical
-- product and rows excluded by the > 0 price predicate.
select id, retailer, retailer_key, product_name, product_price,
       match_key, canonical_product_name, match_confidence, store_id
from public.flyer_deals
where nullif(trim(canonical_product_name), '') is null
   or nullif(trim(match_key), '') is null
   or product_price is null or product_price <= 0
order by id
limit 1000;

-- 8) Canonical v2.7 projection parity (timeout-safe).
-- The broad all-retailer joins previously timed out in Supabase. Run the
-- summary query once per retailer_key below. Each query uses the same indexed
-- equality filter that succeeded during the audit and returns only three
-- counts. The view is the projection consumed by /best-deals.
--
-- Replace 'kroger' with one value at a time:
--   kroger, harristeeter, aldi, aldiv2, wholefoods
select
  count(distinct fd.match_key) as source_identity_keys,
  count(distinct fd.match_key) filter (where bd.match_key is not null) as projected_identity_keys,
  count(distinct fd.match_key) filter (where bd.match_key is null) as missing_projection_keys
from public.flyer_deals fd
left join public.best_deals_comprehensive bd
  on bd.match_key = fd.match_key
where fd.match_key is not null
  and fd.retailer_key = 'kroger';

-- Projection-only sanity check (small table; should complete quickly).
select count(*) as projection_rows
from public.best_deals_comprehensive;

-- To inspect examples without a broad sort, run one retailer at a time:
select fd.match_key,
       fd.canonical_product_name as flyer_canonical_name,
       bd.canonical_product_name as projection_canonical_name,
       fd.brand as flyer_brand,
       bd.brand as projection_brand
from public.flyer_deals fd
join public.best_deals_comprehensive bd using (match_key)
where fd.retailer_key = 'kroger'
  and (
    fd.canonical_product_name is distinct from bd.canonical_product_name
    or fd.brand is distinct from bd.brand
  )
limit 25;

-- 9) Surface eligibility by source row. These predicates mirror the actual
-- code paths: Search requires canonical identity and match_key; Deals requires
-- a positive price and a resolved nearby store; Cart inherits Deals and matches
-- product_name text. A row can be valid in flyer_deals yet invisible downstream.
select retailer, count(*) as rows,
  count(*) filter (where nullif(trim(canonical_product_name), '') is not null
                   and nullif(trim(match_key), '') is not null) as search_eligible,
  count(*) filter (where product_price > 0 and store_id is not null) as deals_eligible,
  count(*) filter (where product_price > 0 and store_id is not null
                   and nullif(trim(product_name), '') is not null) as cart_eligible,
  count(*) filter (where nullif(trim(canonical_product_name), '') is not null
                   and nullif(trim(match_key), '') is not null
                   and (product_price is null or product_price <= 0
                        or store_id is null)) as projected_but_not_surfaceable
from public.flyer_deals
group by retailer
order by rows desc;

-- 10) Deterministic 250-row QA sample (62/63/63/62), ready for manual labels.
with ranked as (
  select fd.*,
    row_number() over (
      partition by case
        when lower(coalesce(retailer_key, retailer, '')) like '%kroger%' then 'Kroger'
        when lower(coalesce(retailer_key, retailer, '')) like '%whole%food%' then 'Whole Foods'
        when lower(coalesce(retailer_key, retailer, '')) like '%harris%teeter%' then 'Harris Teeter'
        when lower(coalesce(retailer_key, retailer, '')) like '%aldi%' then 'Aldi v2'
      end
      order by md5(id::text)
    ) as retailer_row
  from public.flyer_deals fd
  where lower(coalesce(retailer_key, retailer, '')) like '%kroger%'
     or lower(coalesce(retailer_key, retailer, '')) like '%whole%food%'
     or lower(coalesce(retailer_key, retailer, '')) like '%harris%teeter%'
     or lower(coalesce(retailer_key, retailer, '')) like '%aldi%'
)
select id, retailer, retailer_key, product_name, product_price, product_size,
       brand, category, base_amount, base_unit, match_key,
       canonical_product_name, store_id, match_confidence, processed_at,
       cast(null as text) as projection_status,
       cast(null as text) as search_status,
       cast(null as text) as deals_status,
       cast(null as text) as cart_status,
       cast(null as text) as qa_label, cast(null as text) as issue_type,
       cast(null as text) as reviewer_notes
from ranked
where (retailer = 'Kroger' and retailer_row <= 62)
   or (retailer = 'Whole Foods' and retailer_row <= 63)
   or (retailer = 'Harris Teeter' and retailer_row <= 63)
   or (retailer = 'Aldi v2' and retailer_row <= 62)
order by retailer, retailer_row;
