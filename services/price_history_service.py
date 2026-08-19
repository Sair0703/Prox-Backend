import logging
from datetime import datetime, timedelta, timezone
from config.supabase import get_supabase_client

logger = logging.getLogger(__name__)

TABLE      = "price_history"
BATCH_SIZE = 500
MAX_ALIAS_DEPTH = 8


def upsert_price_history(rows: list[dict]) -> int:
    if not rows:
        return 0
    client  = get_supabase_client()
    written = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        client.table(TABLE)\
            .upsert(batch, on_conflict="match_key,store_id,observed_date")\
            .execute()
        written += len(batch)
        logger.info(f"Upserted {written}/{len(rows)} price_history rows")
    return written


def _legacy_format_variants(match_key: str) -> list[str]:
    """Return historical casing/size variants for a legacy v1 match key."""
    if match_key.startswith("v2|"):
        return [match_key]

    parts = match_key.rsplit("|", 1)
    if len(parts) != 2:
        return [match_key]
    base, size = parts
    brand_canonical = base.split("|", 1)
    if len(brand_canonical) != 2:
        return [match_key]
    brand, canonical = brand_canonical
    brand_lower = brand.lower()

    sizes = [size]
    if size != "no_size":
        try:
            numeric_size = float(size)
            if numeric_size.is_integer():
                size_with_decimal = f"{numeric_size:.1f}"
                if size_with_decimal not in sizes:
                    sizes.append(size_with_decimal)
            size_without_decimal = f"{numeric_size:g}"
            if size_without_decimal not in sizes:
                sizes.append(size_without_decimal)
        except (TypeError, ValueError):
            pass
        sizes.append("no_size")

    brands = [brand]
    if brand_lower != brand:
        brands.append(brand_lower)

    variants: list[str] = []
    for variant_size in sizes:
        for variant_brand in brands:
            variant = f"{variant_brand}|{canonical}|{variant_size}"
            if variant not in variants:
                variants.append(variant)
    return variants


def _alias_history_keys(match_key: str) -> list[str]:
    """Return safe current + historical keys for one product identity.

    Forward resolution is only followed when the legacy key has exactly one
    terminal successor. Reverse traversal only follows aliases marked as a
    unique successor. Ambiguous legacy keys therefore stay on their original
    history instead of being copied across multiple v2 products.
    """
    client = get_supabase_client()
    resolved_key = match_key

    try:
        resolved_rows = client.rpc(
            "resolve_match_key_v2",
            {"p_match_key": match_key, "p_max_depth": MAX_ALIAS_DEPTH},
        ).execute().data or []
        if resolved_rows:
            terminal_count = int(resolved_rows[0].get("terminal_count") or 0)
            if terminal_count == 1:
                resolved_key = resolved_rows[0].get("resolved_match_key") or match_key
    except Exception as exc:
        logger.debug("match-key v2 forward resolution unavailable: %s", exc)
        return [match_key]

    keys = [resolved_key]
    seen = {resolved_key}
    frontier = [resolved_key]

    try:
        for _ in range(MAX_ALIAS_DEPTH):
            if not frontier:
                break
            rows = (
                client.table("match_key_aliases_v2")
                .select("old_match_key")
                .in_("new_match_key", frontier)
                .eq("is_unique_successor", True)
                .execute()
                .data or []
            )
            next_frontier: list[str] = []
            for row in rows:
                old_key = row.get("old_match_key")
                if old_key and old_key not in seen:
                    seen.add(old_key)
                    keys.append(old_key)
                    next_frontier.append(old_key)
            frontier = next_frontier
    except Exception as exc:
        logger.debug("match-key v2 reverse alias lookup unavailable: %s", exc)

    if match_key not in seen:
        keys.append(match_key)
    return keys


def _match_key_variants(match_key: str) -> list[str]:
    """Return safe v2 aliases plus legacy format variants in lookup order."""
    variants: list[str] = []
    for alias_key in _alias_history_keys(match_key):
        for candidate in _legacy_format_variants(alias_key):
            if candidate not in variants:
                variants.append(candidate)
    return variants or [match_key]


def _query_history(client, key: str, store_id: str, since: str) -> list[dict]:
    return client.table(TABLE)\
        .select("observed_at, product_price, store_id")\
        .eq("match_key", key)\
        .eq("store_id", store_id)\
        .gte("observed_at", since)\
        .order("observed_at", desc=False)\
        .execute().data or []


def get_price_history(match_key: str, store_id: str, days: int = 90) -> list[dict]:
    client = get_supabase_client()
    since  = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    for key in _match_key_variants(match_key):
        rows = _query_history(client, key, store_id, since)
        if rows:
            return rows
    return []


def get_baseline_price(match_key: str, store_id: str, days: int = 90) -> float | None:
    history = get_price_history(match_key, store_id, days)
    if not history:
        return None
    by_date: dict[str, list[float]] = {}
    for row in history:
        day = row["observed_at"][:10]
        by_date.setdefault(day, []).append(float(row["product_price"]))
    daily_mins = [min(prices) for prices in by_date.values()]
    return round(sum(daily_mins) / len(daily_mins), 2)


def get_latest_price(match_key: str, store_id: str) -> dict | None:
    client = get_supabase_client()
    for key in _match_key_variants(match_key):
        res = client.table(TABLE)\
            .select("product_price, observed_at, flyer_id")\
            .eq("match_key", key)\
            .eq("store_id", store_id)\
            .order("observed_at", desc=True)\
            .limit(1)\
            .execute()
        if res.data:
            return res.data[0]
    return None


def get_all_match_key_store_pairs() -> list[dict]:
    client = get_supabase_client()
    res    = client.table(TABLE).select("match_key, store_id").execute()
    seen, pairs = set(), []
    for row in res.data or []:
        key = (row["match_key"], row["store_id"])
        if key not in seen:
            seen.add(key)
            pairs.append({"match_key": row["match_key"], "store_id": row["store_id"]})
    return pairs
