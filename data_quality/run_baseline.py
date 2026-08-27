"""Export a read-only Prox production data-quality baseline via Supabase REST."""

from __future__ import annotations

import argparse
import csv
import os
from collections import Counter, defaultdict
from pathlib import Path

RETAILERS = {
    "kroger": "Kroger",
    "whole foods": "Whole Foods",
    "wholefoods": "Whole Foods",
    "harris teeter": "Harris Teeter",
    "harristeeter": "Harris Teeter",
    "aldi": "Aldi v2",
}
QA_FIELDS = [
    "id", "retailer", "retailer_key", "product_name", "product_price",
    "product_size", "brand", "category", "base_amount", "base_unit",
    "match_key", "canonical_product_name", "store_id", "match_confidence",
    "processed_at", "projection_status", "search_status", "deals_status",
    "cart_status", "qa_label", "issue_type", "reviewer_notes",
]
IMPACT = {
    "wrong_identity": 5, "price_invalid": 5, "search_fallback": 4,
    "store_unresolved": 4, "canonical_missing": 4, "category_conflict": 3,
    "size_invalid": 3, "brand_missing": 2, "store_gps_missing": 2,
}


def fetch_all(client, table: str, columns: str) -> list[dict]:
    rows: list[dict] = []
    page_size = 250
    last_id = 0
    while True:
        batch = (
            client.table(table)
            .select(columns)
            .gt("id", last_id)
            .order("id")
            .limit(page_size)
            .execute()
            .data
            or []
        )
        rows.extend(batch)
        if len(batch) < page_size:
            return rows
        last_id = batch[-1]["id"]
    raise RuntimeError(f"{table} exceeded the 10,000,000-row safety limit")


def fetch_retailer_deals(client, columns: str) -> list[dict]:
    rows: list[dict] = []
    for retailer_key in ("kroger", "wholefoods", "whole_foods", "harristeeter", "aldi"):
        for offset in range(0, 1_000_000, 250):
            batch = (
                client.table("flyer_deals")
                .select(columns)
                .eq("retailer_key", retailer_key)
                .range(offset, offset + 249)
                .execute()
                .data
                or []
            )
            rows.extend(batch)
            if len(batch) < 250:
                break
    unique = {row["id"]: row for row in rows}
    return list(unique.values())


def retailer_name(row: dict) -> str:
    raw = (row.get("retailer_key") or row.get("retailer") or "").lower().strip()
    for token, name in RETAILERS.items():
        if token in raw:
            return name
    return row.get("retailer_key") or row.get("retailer") or "__unknown__"


def pct(n: int, total: int) -> str:
    return f"{100 * n / total:.2f}" if total else "0.00"


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qa-csv", type=Path)
    args = parser.parse_args()
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise SystemExit("SUPABASE_URL and SUPABASE_KEY are required; no live baseline was produced.")
    from supabase import create_client

    client = create_client(url, key)
    deals = fetch_retailer_deals(
        client,
        "id,retailer,retailer_key,product_name,product_price,product_size,brand,category,"
        "base_amount,base_unit,match_key,canonical_product_name,store_id,match_confidence,processed_at",
    )
    store_ids = sorted({row["store_id"] for row in deals if row.get("store_id")})
    stores: list[dict] = []
    for start in range(0, len(store_ids), 200):
        stores.extend(
            client.table("store_locations")
            .select("id,latitude,longitude,lat,lng,geocode_confidence,updated_at")
            .in_("id", store_ids[start:start + 200])
            .execute()
            .data
            or []
        )
    store_by_id = {row.get("id"): row for row in stores}
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in deals:
        row["_retailer_name"] = retailer_name(row)
        has_identity = bool(str(row.get("canonical_product_name") or "").strip()) and bool(
            str(row.get("match_key") or "").strip()
        )
        row["projection_status"] = "identity_present" if has_identity else "identity_missing"
        row["search_status"] = "visible" if has_identity else "fallback_or_hidden"
        row["deals_status"] = (
            "visible" if row.get("product_price", 0) and row["product_price"] > 0 and row.get("store_id")
            else "hidden"
        )
        row["cart_status"] = (
            "candidate" if row["deals_status"] == "visible" and str(row.get("product_name") or "").strip()
            else "hidden"
        )
        grouped[row["_retailer_name"]].append(row)

    scorecard = []
    for name, rows in sorted(grouped.items(), key=lambda item: -len(item[1])):
        conflicts = defaultdict(set)
        for row in rows:
            if row.get("match_key") and row.get("category"):
                conflicts[row["match_key"]].add(str(row["category"]).strip().lower())
        conflict_rows = sum(
            1 for row in rows if len(conflicts.get(row.get("match_key"), set())) > 1
        )
        invalid_price = sum(
            1 for row in rows
            if row.get("product_price") is None or row["product_price"] <= 0
        )
        size_flagged = sum(
            1 for row in rows
            if row.get("base_amount") is not None
            and (row["base_amount"] <= 0 or not str(row.get("base_unit") or "").strip())
        )
        fallback = sum(
            1 for row in rows
            if not str(row.get("canonical_product_name") or "").strip()
            or not str(row.get("match_key") or "").strip()
            or row.get("match_confidence") in (None, "none", "created")
        )
        gps_missing = sum(
            1 for row in rows
            if row.get("store_id") and not (
                store_by_id.get(row["store_id"], {}).get("latitude",
                store_by_id.get(row["store_id"], {}).get("lat")) is not None
                and store_by_id.get(row["store_id"], {}).get("longitude",
                store_by_id.get(row["store_id"], {}).get("lng")) is not None
            )
        )
        scorecard.append({
            "retailer": name, "rows": len(rows),
            "canonical_fill_pct": pct(sum(bool(str(r.get("canonical_product_name") or "").strip()) for r in rows), len(rows)),
            "brand_fill_pct": pct(sum(bool(str(r.get("brand") or "").strip()) for r in rows), len(rows)),
            "category_conflict_rows": conflict_rows,
            "size_flagged_pct": pct(size_flagged, len(rows)),
            "invalid_price_pct": pct(invalid_price, len(rows)),
            "search_fallback_pct": pct(fallback, len(rows)),
            "store_unresolved_pct": pct(sum(not r.get("store_id") for r in rows), len(rows)),
            "store_gps_missing_pct": pct(gps_missing, len(rows)),
            "newest_processed_at": max((r.get("processed_at") or "") for r in rows),
        })
    out = Path(__file__).parent
    write_csv(out / "scorecard_by_retailer.csv", scorecard, list(scorecard[0]) if scorecard else ["retailer"])

    categories_by_key: dict[str, set[str]] = defaultdict(set)
    for row in deals:
        if row.get("match_key") and row.get("category"):
            categories_by_key[row["match_key"]].add(str(row["category"]).strip().lower())
    issue_rows: dict[str, set] = defaultdict(set)
    examples: dict[str, str] = {}
    for row in deals:
        checks = {
            "canonical_missing": not str(row.get("canonical_product_name") or "").strip(),
            "brand_missing": not str(row.get("brand") or "").strip(),
            "category_conflict": len(categories_by_key.get(row.get("match_key"), set())) > 1,
            "price_invalid": row.get("product_price") is None or row["product_price"] <= 0,
            "size_invalid": row.get("base_amount") is not None and (
                row["base_amount"] <= 0 or not str(row.get("base_unit") or "").strip()
            ),
            "search_fallback": (
                not str(row.get("canonical_product_name") or "").strip()
                or not str(row.get("match_key") or "").strip()
                or row.get("match_confidence") in (None, "none", "created")
            ),
            "store_unresolved": not row.get("store_id"),
            "store_gps_missing": bool(row.get("store_id")) and not (
                store_by_id.get(row["store_id"], {}).get("latitude",
                store_by_id.get(row["store_id"], {}).get("lat")) is not None
                and store_by_id.get(row["store_id"], {}).get("longitude",
                store_by_id.get(row["store_id"], {}).get("lng")) is not None
            ),
        }
        for issue, present in checks.items():
            if present:
                issue_rows[issue].add(str(row["id"]))
                examples.setdefault(issue, str(row["id"]))
    for row in deals:
        row["retailer"] = row.pop("_retailer_name")
        row["qa_label"] = row.get("qa_label", "")
        row["issue_type"] = row.get("issue_type", "")
        row["reviewer_notes"] = row.get("reviewer_notes", "")
    if args.qa_csv and args.qa_csv.exists():
        with args.qa_csv.open(newline="", encoding="utf-8") as handle:
            labeled = {r["id"]: r for r in csv.DictReader(handle)}
        for row in deals:
            row.update({k: v for k, v in labeled.get(str(row["id"]), {}).items() if k in ("qa_label", "issue_type", "reviewer_notes")})
    labels = Counter(
        r.get("issue_type") for r in deals
        if r.get("qa_label") == "incorrect" and r.get("issue_type")
    )
    for issue, ids in issue_rows.items():
        affected = labels.get(issue, len(ids))
        recurrence = affected / len(deals) if deals else 0
        ranked.append({
            "issue_type": issue, "affected_rows": affected,
            "user_impact": IMPACT.get(issue, 1),
            "recurrence": f"{recurrence:.4f}",
            "rank_score": round(affected * IMPACT.get(issue, 1) * recurrence, 2),
            "example_id": examples.get(issue, ""),
        })
    ranked.sort(key=lambda r: (-r["rank_score"], r["issue_type"]))
    write_csv(out / "issue_register.csv", ranked[:10], [
        "issue_type", "affected_rows", "user_impact", "recurrence",
        "rank_score", "example_id",
    ])

    targets = {"Kroger": 62, "Whole Foods": 63, "Harris Teeter": 63, "Aldi v2": 62}
    sample: list[dict] = []
    for name, limit in targets.items():
        sample.extend(sorted(grouped.get(name, []), key=lambda r: str(r.get("id")))[:limit])
    write_csv(out / "qa_sample_250.csv", sample, QA_FIELDS)
    print(f"Read-only export complete: {len(deals)} deals, {len(stores)} stores, {len(sample)} QA rows")


if __name__ == "__main__":
    main()
