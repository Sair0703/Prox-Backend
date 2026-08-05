type FlyerDealRow = {
  id: number;
  match_key: string | null;
  brand: string | null;
  canonical_product_name: string | null;
  product_price: number | string | null;
  base_amount: number | string | null;
  base_unit: string | null;
  store_id: number | string | null;
  processed_at: string | null;
};

type PriceHistoryRow = {
  match_key: string;
  store_id: number | string;
  brand: string | null;
  canonical_product_name: string | null;
  size_oz: number | null;
  product_price: number;
  observed_at: string;
  observed_date: string;
};

type UpsertResult = {
  written: number;
  batches: number;
};

const TARGET_TABLE = "price_history";
const WRITE_BATCH = 2000;
const DEFAULT_BATCH_SIZE = 2000;
const MAX_BATCH_SIZE = 10000;
const MAX_RETRIES = 3;
const MAX_PRICE = 999999;
const MIN_REAL_SIZE_OZ = 1.5;

const SIZE_IN_NAME_RE = /(\d+(?:\.\d+)?)\s*(oz|fl\.?\s*oz|g|gram|lb|pound|ml|liter)\b/gi;

Deno.serve(async (req) => {
  const startedAt = new Date();

  try {
    const body = await readJson(req);
    const dryRun = body?.dry_run === true;
    const batchSize = normalizeBatchSize(body?.batch_size);

    if (dryRun) {
      return jsonResponse({
        ok: true,
        dry_run: true,
        batch_size: batchSize,
        note: "Dry run is a no-op because claiming rows mutates flyer_deals.",
        duration_ms: Date.now() - startedAt.getTime(),
      });
    }

    const claimedRows = await claimPriceHistoryRows(batchSize);
    const claimedIds = claimedRows.map((row) => row.id);
    const observedAt = new Date().toISOString();
    const historyRows = buildPriceHistoryRows(claimedRows, observedAt);
    const upsert = await upsertPriceHistory(historyRows);
    const markedProcessed = claimedIds.length
      ? await markPriceHistoryRowsProcessed(claimedIds)
      : 0;

    return jsonResponse({
      ok: true,
      dry_run: false,
      batch_size: batchSize,
      rows_claimed: claimedRows.length,
      rows_grouped: historyRows.length,
      rows_written: upsert.written,
      write_batches: upsert.batches,
      rows_marked_processed: markedProcessed,
      duration_ms: Date.now() - startedAt.getTime(),
    });
  } catch (error) {
    console.error("price-history-prod failed", error);
    return jsonResponse(
      {
        ok: false,
        error: error instanceof Error ? error.message : String(error),
      },
      500,
    );
  }
});

async function readJson(req: Request): Promise<Record<string, unknown>> {
  if (req.method === "GET") {
    const url = new URL(req.url);
    return {
      batch_size: url.searchParams.get("batch_size") ?? undefined,
      dry_run: url.searchParams.get("dry_run") === "true",
    };
  }

  try {
    return await req.json();
  } catch {
    return {};
  }
}

function normalizeBatchSize(value: unknown): number {
  const parsed = Number(value ?? DEFAULT_BATCH_SIZE);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return DEFAULT_BATCH_SIZE;
  }
  return Math.min(Math.trunc(parsed), MAX_BATCH_SIZE);
}

function supabaseConfig(): { url: string; key: string } {
  const url = Deno.env.get("SUPABASE_URL")?.replace(/\/$/, "");
  const key =
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ??
    Deno.env.get("SUPABASE_KEY");

  if (!url || !key) {
    throw new Error("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set.");
  }

  return { url, key };
}

function baseHeaders(extra?: HeadersInit): HeadersInit {
  const { key } = supabaseConfig();
  return {
    apikey: key,
    Authorization: `Bearer ${key}`,
    "Content-Type": "application/json",
    ...extra,
  };
}

async function claimPriceHistoryRows(batchSize: number): Promise<FlyerDealRow[]> {
  const { url } = supabaseConfig();
  const res = await retry(async () => {
    const response = await fetch(`${url}/rest/v1/rpc/claim_price_history_rows`, {
      method: "POST",
      headers: baseHeaders({
        Prefer: "return=representation",
      }),
      body: JSON.stringify({
        p_batch_size: batchSize,
      }),
    });

    if (!response.ok) {
      throw new Error(`claim rpc failed: ${response.status} ${await response.text()}`);
    }

    return response;
  }, `claim_price_history_rows batch_size=${batchSize}`);

  const data = await res.json();
  if (!Array.isArray(data)) {
    throw new Error(`claim rpc returned unexpected response: ${JSON.stringify(data)}`);
  }
  return data as FlyerDealRow[];
}

function buildPriceHistoryRows(rows: FlyerDealRow[], observedAt: string): PriceHistoryRow[] {
  const grouped = new Map<string, PriceHistoryRow>();

  for (const row of rows) {
    const record = makePriceHistoryRecord(row, observedAt);
    if (!record) continue;

    const key = `${record.match_key}\u0000${record.store_id}\u0000${record.observed_date}`;
    const existing = grouped.get(key);
    if (!existing || record.product_price < existing.product_price) {
      grouped.set(key, record);
    }
  }

  return [...grouped.values()];
}

function makePriceHistoryRecord(
  row: FlyerDealRow,
  observedAt: string,
): PriceHistoryRow | null {
  if (!row.match_key || row.store_id === null || row.store_id === undefined || !row.processed_at) {
    return null;
  }

  const price = Number(row.product_price);
  if (!Number.isFinite(price) || price <= 0 || price > MAX_PRICE) {
    return null;
  }

  return {
    match_key: row.match_key,
    store_id: row.store_id,
    brand: row.brand,
    canonical_product_name: row.canonical_product_name,
    size_oz: normalizeSizeOz(row.base_amount, row.base_unit, row.canonical_product_name ?? ""),
    product_price: price,
    observed_at: observedAt,
    observed_date: row.processed_at.slice(0, 10),
  };
}

function toOz(amount: number, unit: string): number | null {
  const u = unit.toLowerCase().replace(/\s+/g, "").replace(/\./g, "");
  if (u === "oz" || u === "floz") return round(amount);
  if (u === "g" || u === "gram") return round(amount * 0.035274);
  if (u === "lb" || u === "pound") return round(amount * 16);
  if (u === "ml") return round(amount * 0.033814);
  if (u === "l" || u === "liter") return round(amount * 33.814);
  return null;
}

function normalizeSizeOz(
  baseAmount: number | string | null,
  baseUnit: string | null,
  productName: string,
): number | null {
  let dbOz: number | null = null;
  const amount = Number(baseAmount);
  if (Number.isFinite(amount) && baseUnit) {
    dbOz = toOz(amount, baseUnit);
  }

  const nameCandidates: number[] = [];
  for (const match of productName.matchAll(SIZE_IN_NAME_RE)) {
    const oz = toOz(Number(match[1]), match[2]);
    if (oz !== null && oz >= MIN_REAL_SIZE_OZ) {
      nameCandidates.push(oz);
    }
  }

  const nameOz = nameCandidates.length ? Math.max(...nameCandidates) : null;
  if (dbOz !== null && dbOz >= MIN_REAL_SIZE_OZ) return dbOz;
  if (nameOz !== null) return nameOz;
  return dbOz;
}

function round(value: number): number {
  return Math.round(value * 100) / 100;
}

async function upsertPriceHistory(rows: PriceHistoryRow[]): Promise<UpsertResult> {
  if (!rows.length) {
    return { written: 0, batches: 0 };
  }

  const { url } = supabaseConfig();
  let written = 0;
  let batches = 0;

  for (let i = 0; i < rows.length; i += WRITE_BATCH) {
    const batch = rows.slice(i, i + WRITE_BATCH);
    await retry(async () => {
      const res = await fetch(
        `${url}/rest/v1/${TARGET_TABLE}?on_conflict=match_key,store_id,observed_date`,
        {
          method: "POST",
          headers: baseHeaders({
            Prefer: "resolution=merge-duplicates,return=minimal",
          }),
          body: JSON.stringify(batch),
        },
      );

      if (!res.ok) {
        throw new Error(`upsert failed: ${res.status} ${await res.text()}`);
      }
    }, `upsert rows ${i}-${i + batch.length - 1}`);

    written += batch.length;
    batches += 1;
    console.info(
      `upserted ${written}/${rows.length} price_history rows ` +
        `(${batches} batches, batch_size=${batch.length})`,
    );
  }

  return { written, batches };
}

async function markPriceHistoryRowsProcessed(ids: number[]): Promise<number> {
  const { url } = supabaseConfig();
  const res = await retry(async () => {
    const response = await fetch(`${url}/rest/v1/rpc/mark_price_history_rows_processed`, {
      method: "POST",
      headers: baseHeaders(),
      body: JSON.stringify({
        p_ids: ids,
      }),
    });

    if (!response.ok) {
      throw new Error(`mark processed rpc failed: ${response.status} ${await response.text()}`);
    }

    return response;
  }, `mark_price_history_rows_processed count=${ids.length}`);

  const data = await res.json();
  return Number(data ?? 0);
}

async function retry<T>(fn: () => Promise<T>, label: string): Promise<T> {
  let lastError: unknown;

  for (let attempt = 1; attempt <= MAX_RETRIES; attempt += 1) {
    try {
      return await fn();
    } catch (error) {
      lastError = error;
      if (attempt === MAX_RETRIES) {
        break;
      }

      const delayMs = 500 * 2 ** (attempt - 1);
      console.warn(`${label} failed on attempt ${attempt}; retrying in ${delayMs}ms`, error);
      await new Promise((resolve) => setTimeout(resolve, delayMs));
    }
  }

  throw lastError;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json",
    },
  });
}
