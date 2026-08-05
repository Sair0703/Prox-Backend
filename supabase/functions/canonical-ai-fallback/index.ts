import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import { buildNormalizedSignature } from "./canonicalCore.ts";
import {
  isUsableCanonicalName,
  runCanonicalAIBatches,
  type CanonicalAIRow,
} from "./canonicalAiFallback.ts";

const DEFAULT_BATCH_SIZE = 50;
const MAX_BATCH_SIZE = 100;
const UPDATE_RPC_CHUNK_SIZE = 50;
const DEFAULT_MIN_CONFIDENCE = 0.75;

const TARGET_TABLE = Deno.env.get("CANONICAL_TARGET_TABLE") || "flyer_deals";
const CANONICAL_TABLE = Deno.env.get("CANONICAL_LOOKUP_TABLE") || "canonical_name_test";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function envNumber(name: string, fallback: number): number {
  const value = Number(Deno.env.get(name));
  return Number.isFinite(value) ? value : fallback;
}

interface ClaimedRow {
  id: unknown;
  product_name: string | null;
  brand: string | null;
  display_size: string | null;
}

interface UpdateRow {
  id: string;
  canonical_product_name: string | null;
  canonical_match_stage: "ai_fallback" | "ai_no_match";
}

interface LookupRow {
  raw_product_name: string;
  brand: string;
  canonical_name: string;
  normalized_signature: string;
  source: string;
  confidence: "high" | "medium" | "low";
  confidence_score: number;
  updated_at: string;
}

function confidenceLabel(confidence: number): "high" | "medium" | "low" {
  if (confidence >= 0.9) return "high";
  if (confidence >= 0.75) return "medium";
  return "low";
}

serve(async (req) => {
  const startedAt = Date.now();
  const workerId = `canonical_ai_${crypto.randomUUID()}`;
  // deno-lint-ignore no-explicit-any
  let supabase: any = null;

  try {
    const supabaseUrl = Deno.env.get("SUPABASE_URL");
    const serviceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");

    if (!supabaseUrl) throw new Error("Missing SUPABASE_URL");
    if (!serviceRoleKey) throw new Error("Missing SUPABASE_SERVICE_ROLE_KEY");

    supabase = createClient(supabaseUrl, serviceRoleKey);

    let batchSize = DEFAULT_BATCH_SIZE;
    try {
      const body = await req.json();
      batchSize = Number(body?.batch_size || DEFAULT_BATCH_SIZE);
    } catch {
      batchSize = DEFAULT_BATCH_SIZE;
    }
    batchSize = Math.max(1, Math.min(batchSize, MAX_BATCH_SIZE));

    const minConfidence = Math.max(
      0,
      Math.min(1, envNumber("CANONICAL_AI_MIN_CONFIDENCE", DEFAULT_MIN_CONFIDENCE)),
    );

    console.log(
      `[canonical-ai][${workerId}] claiming up to ${batchSize} rows from ${TARGET_TABLE}`,
    );

    const { data: rows, error: claimError } = await supabase.rpc("claim_canonical_ai_fallback_rows", {
      p_worker_id: workerId,
      p_batch_size: batchSize,
    });

    if (claimError) {
      throw new Error(`Claim error: ${claimError.message}`);
    }

    console.log(`[canonical-ai][${workerId}] claimed ${rows?.length || 0} rows`);

    if (!rows || rows.length === 0) {
      return jsonResponse({
        status: "no_rows",
        worker: workerId,
        table: TARGET_TABLE,
      });
    }

    const claimedRows = rows as ClaimedRow[];
    const aiRows: CanonicalAIRow[] = [];
    const updates: UpdateRow[] = [];

    for (let index = 0; index < claimedRows.length; index++) {
      const row = claimedRows[index];
      const productName = String(row.product_name || "").trim();
      const brand = String(row.brand || "").trim();
      const displaySize = row.display_size ? String(row.display_size) : null;

      if (!productName || !brand) {
        updates.push({
          id: String(row.id),
          canonical_product_name: null,
          canonical_match_stage: "ai_no_match",
        });
        continue;
      }

      aiRows.push({
        rowIndex: index,
        rowId: String(row.id),
        productName,
        brand,
        displaySize,
      });
    }

    const { results, stats } = await runCanonicalAIBatches(aiRows);
    const lookupRows: LookupRow[] = [];

    for (const aiRow of aiRows) {
      const result = results.get(aiRow.rowIndex);
      if (!result) {
        updates.push({
          id: aiRow.rowId,
          canonical_product_name: null,
          canonical_match_stage: "ai_no_match",
        });
        stats.invalidResults++;
        continue;
      }

      const candidateName = result.canonical_product_name;
      if (!isUsableCanonicalName(candidateName, aiRow, minConfidence, result.confidence)) {
        updates.push({
          id: aiRow.rowId,
          canonical_product_name: null,
          canonical_match_stage: "ai_no_match",
        });
        if (result.confidence < minConfidence) {
          stats.lowConfidence++;
        } else {
          stats.invalidResults++;
        }
        continue;
      }

      const canonicalName = candidateName;
      const signature = buildNormalizedSignature(aiRow.productName, aiRow.brand, aiRow.displaySize);

      updates.push({
        id: aiRow.rowId,
        canonical_product_name: canonicalName,
        canonical_match_stage: "ai_fallback",
      });

      lookupRows.push({
        raw_product_name: aiRow.productName,
        brand: aiRow.brand,
        canonical_name: canonicalName,
        normalized_signature: signature,
        source: "ai_fallback",
        confidence: confidenceLabel(result.confidence),
        confidence_score: result.confidence,
        updated_at: new Date().toISOString(),
      });

      stats.resolved++;
      if (stats.examples.length < 10) {
        stats.examples.push({
          original: aiRow.productName,
          canonical: canonicalName,
          confidence: result.confidence,
        });
      }
    }

    if (lookupRows.length > 0) {
      const { error: lookupError } = await supabase
        .from(CANONICAL_TABLE)
        .insert(lookupRows);

      if (lookupError) {
        throw new Error(`Lookup insert error: ${lookupError.message}`);
      }
    }

    let updated = 0;
    for (let i = 0; i < updates.length; i += UPDATE_RPC_CHUNK_SIZE) {
      const chunk = updates.slice(i, i + UPDATE_RPC_CHUNK_SIZE);
      const { data, error } = await supabase.rpc("apply_canonical_ai_fallback_batch", {
        updates: chunk,
        p_worker_id: workerId,
      });

      if (error) {
        throw new Error(
          `apply_canonical_ai_fallback_batch failed for rows ${i + 1}-${i + chunk.length}: ${error.message}`,
        );
      }

      updated += Number(data || 0);
    }

    const durationMs = Date.now() - startedAt;
    console.log(
      `[canonical-ai][${workerId}] done: claimed=${claimedRows.length}, updated=${updated}, resolved=${stats.resolved}, duration=${durationMs}ms`,
    );

    return jsonResponse({
      status: "ok",
      worker: workerId,
      table: TARGET_TABLE,
      claimed: claimedRows.length,
      ai_candidates: aiRows.length,
      updated,
      lookup_inserts: lookupRows.length,
      ai_resolved: stats.resolved,
      ai_no_match: updates.filter((row) => row.canonical_match_stage === "ai_no_match").length,
      low_confidence: stats.lowConfidence,
      invalid_results: stats.invalidResults,
      api_calls: stats.totalApiCalls,
      cache_hits: stats.cacheHits,
      ai_errors: stats.errors,
      ai_duration_ms: stats.totalTimeMs,
      duration_ms: durationMs,
      examples: stats.examples,
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error(`[canonical-ai][${workerId}] fatal error:`, message);

    try {
      if (supabase) {
        await supabase
          .from(TARGET_TABLE)
          .update({ canonical_ai_worker: null, canonical_ai_started_at: null })
          .eq("canonical_ai_worker", workerId);
        console.log(`[canonical-ai][${workerId}] released claims`);
      }
    } catch {
      console.error(`[canonical-ai][${workerId}] failed to release claims`);
    }

    return jsonResponse({ status: "error", worker: workerId, error: message }, 500);
  }
});
