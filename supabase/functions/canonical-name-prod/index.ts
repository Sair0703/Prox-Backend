import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import {
  buildNormalizedSignature,
  loadCanonicalLookup,
  lookupExactMatch,
  lookupBrandCandidates,
  stage2BestCandidate,
  type CanonicalLookup,
  type MatchResult,
  type RawRow,
} from "./canonicalCore.ts";

let cachedLookup: CanonicalLookup | null = null;
let lookupCachedAt = 0;

const LOOKUP_TTL_MS = 5 * 60 * 1000;
const DEFAULT_BATCH_SIZE = 100;
const MAX_BATCH_SIZE = 500;
const UPDATE_RPC_CHUNK_SIZE = 250;

const STAGE2_ENABLED = true;
const STAGE2_JACCARD_MIN = 0.8;
const STAGE2_MIN_SHARED_TOKENS = 2;
const STAGE2_MARGIN_MIN = 0.08;

const TARGET_TABLE = Deno.env.get("CANONICAL_TARGET_TABLE") || "flyer_deals";
const CANONICAL_TABLE = Deno.env.get("CANONICAL_LOOKUP_TABLE") || "canonical_name_test";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

// deno-lint-ignore no-explicit-any
async function getLookup(supabase: any): Promise<CanonicalLookup> {
  const now = Date.now();
  if (cachedLookup && now - lookupCachedAt < LOOKUP_TTL_MS) {
    console.log(
      `[canonical-prod][lookup] using cached lookup (${Math.round((now - lookupCachedAt) / 1000)}s old)`,
    );
    return cachedLookup;
  }

  console.log(`[canonical-prod][lookup] loading ${CANONICAL_TABLE}...`);
  cachedLookup = await loadCanonicalLookup(supabase, CANONICAL_TABLE);
  lookupCachedAt = now;
  console.log(`[canonical-prod][lookup] loaded ${cachedLookup.entries.length} canonical entries`);
  return cachedLookup;
}

function matchCanonical(
  productName: string,
  brand: string | null,
  displaySize: string | null,
  lookup: CanonicalLookup,
): MatchResult {
  if (!brand) {
    return { status: "no_match" };
  }

  const signature = buildNormalizedSignature(productName, brand, displaySize);
  const exactMatch = lookupExactMatch(lookup, brand, signature);

  if (exactMatch) {
    return {
      status: "matched",
      matchedId: exactMatch.id,
      matchedCanonical: exactMatch.canonical_name,
      matchStage: "stage1_exact",
    };
  }

  if (STAGE2_ENABLED) {
    const candidates = lookupBrandCandidates(lookup, brand);
    const [best, bestScore, secondScore, overlap] = stage2BestCandidate(signature, candidates, {
      minJaccard: STAGE2_JACCARD_MIN,
      minSharedTokens: STAGE2_MIN_SHARED_TOKENS,
      minMargin: STAGE2_MARGIN_MIN,
    });

    if (best) {
      return {
        status: "matched",
        matchedId: best.id,
        matchedCanonical: best.canonical_name,
        matchStage: "stage2_jaccard",
        stage2Score: bestScore,
        stage2SecondScore: secondScore,
        stage2SharedTokens: overlap,
      };
    }
  }

  return { status: "no_match" };
}

serve(async (req) => {
  const startedAt = Date.now();
  const workerId = `canonical_prod_${crypto.randomUUID()}`;

  try {
    const supabaseUrl = Deno.env.get("SUPABASE_URL");
    const serviceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");

    if (!supabaseUrl) throw new Error("Missing SUPABASE_URL");
    if (!serviceRoleKey) throw new Error("Missing SUPABASE_SERVICE_ROLE_KEY");

    const supabase = createClient(supabaseUrl, serviceRoleKey);

    let batchSize = DEFAULT_BATCH_SIZE;
    try {
      const body = await req.json();
      batchSize = Number(body?.batch_size || DEFAULT_BATCH_SIZE);
    } catch {
      batchSize = DEFAULT_BATCH_SIZE;
    }
    batchSize = Math.max(1, Math.min(batchSize, MAX_BATCH_SIZE));

    console.log(
      `[canonical-prod][${workerId}] processing up to ${batchSize} rows from ${TARGET_TABLE}`,
    );

    const lookup = await getLookup(supabase);

    const fetchStartedAt = Date.now();
    const { data: rows, error: fetchError } = await supabase
      .from(TARGET_TABLE)
      .select("id, product_name, brand, display_size")
      .is("canonical_product_name", null)
      .is("canonical_match_stage", null)
      .not("processed_at", "is", null)
      .not("brand", "is", null)
      .order("created_at", { ascending: true })
      .limit(batchSize);

    if (fetchError) {
      throw new Error(`Fetch error: ${fetchError.message}`);
    }

    console.log(
      `[canonical-prod][${workerId}] fetched ${rows?.length || 0} rows in ${
        Date.now() - fetchStartedAt
      }ms`,
    );

    if (!rows || rows.length === 0) {
      return jsonResponse({
        status: "no_rows",
        worker: workerId,
        table: TARGET_TABLE,
      });
    }

    // deno-lint-ignore no-explicit-any
    const matchedRows: any[] = [];
    const matchFailures: Array<{ id: unknown; error: string }> = [];

    for (const row of rows as RawRow[]) {
      try {
        const productName = String(row.product_name || "");
        const brand = row.brand ? String(row.brand) : null;
        const displaySize = row.display_size ? String(row.display_size) : null;
        const match = matchCanonical(productName, brand, displaySize, lookup);

        matchedRows.push({
          id: row.id,
          canonical_product_name: match.status === "matched" ? match.matchedCanonical : null,
          canonical_match_stage: match.matchStage || "no_match",
        });
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        matchFailures.push({ id: row.id, error: message });
      }
    }

    let updated = 0;
    const updateFailures: Array<{ batch: string; error: string }> = [];

    for (let i = 0; i < matchedRows.length; i += UPDATE_RPC_CHUNK_SIZE) {
      const chunk = matchedRows.slice(i, i + UPDATE_RPC_CHUNK_SIZE);
      const updates = chunk.map((row) => ({
        id: String(row.id),
        canonical_product_name: row.canonical_product_name,
        canonical_match_stage: row.canonical_match_stage,
      }));

      const { data, error } = await supabase.rpc("apply_canonical_backfill_batch", {
        updates,
      });

      if (error) {
        throw new Error(
          `apply_canonical_backfill_batch failed for rows ${i + 1}-${i + chunk.length}: ${error.message}`,
        );
      }

      updated += Number(data || 0);
    }

    const stage1Matches = matchedRows.filter((r) => r.canonical_match_stage === "stage1_exact").length;
    const stage2Matches = matchedRows.filter((r) => r.canonical_match_stage === "stage2_jaccard").length;
    const noMatches = matchedRows.filter((r) => r.canonical_match_stage === "no_match").length;
    const durationMs = Date.now() - startedAt;

    console.log(
      `[canonical-prod][${workerId}] done: fetched=${rows.length}, updated=${updated}, duration=${durationMs}ms`,
    );

    return jsonResponse({
      status: "ok",
      worker: workerId,
      table: TARGET_TABLE,
      fetched: rows.length,
      updated,
      stage1_matches: stage1Matches,
      stage2_matches: stage2Matches,
      no_matches: noMatches,
      match_failures: matchFailures.length,
      upsert_failures: updateFailures.length,
      duration_ms: durationMs,
      failure_rows: matchFailures,
      upsert_failure_rows: updateFailures,
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error(`[canonical-prod][${workerId}] fatal error:`, message);
    return jsonResponse({ status: "error", worker: workerId, error: message }, 500);
  }
});
