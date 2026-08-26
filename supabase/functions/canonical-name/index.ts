import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import {
  buildNormalizedSignature,
  loadCanonicalLookup,
  lookupExactMatch,
  lookupBrandCandidates,
  stage2BestCandidate,
  type CanonicalLookup,
  type RawRow,
  type MatchResult,
} from "./canonicalCore.ts";

// Module-level context cache — persists for the lifetime of the Edge Function worker
let cachedLookup: CanonicalLookup | null = null;
let lookupCachedAt = 0;
const LOOKUP_TTL_MS = 5 * 60 * 1000; // 5 minutes

// Stage 2 matching configuration
const STAGE2_ENABLED = true;
const STAGE2_JACCARD_MIN = 0.8;
const STAGE2_MIN_SHARED_TOKENS = 2;
const STAGE2_MARGIN_MIN = 0.08;

// deno-lint-ignore no-explicit-any
async function getLookup(supabase: any): Promise<CanonicalLookup> {
  const now = Date.now();
  if (cachedLookup && now - lookupCachedAt < LOOKUP_TTL_MS) {
    console.log(
      `[canonical][lookup] Using cached lookup (${Math.round((now - lookupCachedAt) / 1000)}s old)`
    );
    return cachedLookup;
  }
  console.log(`[canonical][lookup] Loading fresh canonical data...`);
  cachedLookup = await loadCanonicalLookup(supabase, "canonical_name_test");
  lookupCachedAt = now;
  console.log(`[canonical][lookup] Loaded ${cachedLookup.entries.length} canonical entries`);
  return cachedLookup;
}

function matchCanonical(
  productName: string,
  brand: string | null,
  displaySize: string | null,
  lookup: CanonicalLookup
): MatchResult {
  if (!brand) {
    return { status: "no_match" };
  }

  // Build signature
  const signature = buildNormalizedSignature(productName, brand, displaySize);

  // Stage 1: Exact match
  const exactMatch = lookupExactMatch(lookup, brand, signature);
  if (exactMatch) {
    return {
      status: "matched",
      matchedId: exactMatch.id,
      matchedCanonical: exactMatch.canonical_name,
      matchStage: "stage1_exact",
    };
  }

  // Stage 2: Token overlap fallback
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
  const startTime = Date.now();

  let workerID = "uninitialized";
  // deno-lint-ignore no-explicit-any
  let supabase: any = null;

  try {
    const supabaseUrl = Deno.env.get("SUPABASE_URL");
    const serviceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");

    if (!supabaseUrl) throw new Error("Missing SUPABASE_URL");
    if (!serviceRoleKey) throw new Error("Missing SUPABASE_SERVICE_ROLE_KEY");

    supabase = createClient(supabaseUrl, serviceRoleKey);
    workerID = `canonical_${crypto.randomUUID()}`;

    let batchSize = 500;
    try {
      const body = await req.json();
      batchSize = body?.batch_size ?? 500;
    } catch {
      batchSize = 500;
    }

    console.log(
      `[canonical][${workerID}] Triggered. Processing up to ${batchSize} rows...`
    );

    // Phase 1: Load canonical lookup (cached)
    const lookupStart = Date.now();
    const lookup = await getLookup(supabase);
    console.log(`[canonical][${workerID}] Lookup loaded in ${Date.now() - lookupStart}ms`);

    // Phase 2: Fetch rows with canonical_product_name = null
    const fetchStart = Date.now();
    const { data: rows, error: fetchError } = await supabase
      .from("test_flyer_deals_duplicate")
      .select("id, product_name, brand, display_size")
      .is("canonical_match_stage", null)
      .limit(batchSize)
      .order("created_at", { ascending: true });

    if (fetchError) {
      throw new Error(`Fetch error: ${fetchError.message}`);
    }

    console.log(
      `[canonical][${workerID}] Fetched ${rows?.length || 0} rows in ${
        Date.now() - fetchStart
      }ms`
    );

    if (!rows || rows.length === 0) {
      console.log(`[canonical][${workerID}] No rows with null canonical_product_name.`);
      return new Response(
        JSON.stringify({ status: "no_rows", worker: workerID }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      );
    }

    // Phase 3: Match rows in parallel chunks of 50
    const matchStart = Date.now();
    // deno-lint-ignore no-explicit-any
    const matchedRows: any[] = [];
    const failures: Array<{ id: unknown; error: string }> = [];
    const CHUNK_SIZE = 50;

    for (let i = 0; i < rows.length; i += CHUNK_SIZE) {
      const chunk = rows.slice(i, i + CHUNK_SIZE);

      const results = await Promise.allSettled(
        chunk.map((row: RawRow) => {
          try {
            const productName = String(row.product_name || "");
            const brand = row.brand ? String(row.brand) : null;
            const displaySize = row.display_size ? String(row.display_size) : null;

            const match = matchCanonical(productName, brand, displaySize, lookup);

            return {
              id: row.id,
              canonical_product_name:
                match.status === "matched" ? match.matchedCanonical : null,
              match_stage: match.matchStage || "no_match",
              match_status: match.status,
              stage2_score: match.stage2Score || null,
              stage2_second_score: match.stage2SecondScore || null,
              stage2_shared_tokens: match.stage2SharedTokens || null,
            };
          } catch (err) {
            const message = err instanceof Error ? err.message : String(err);
            throw new Error(`Match failed: ${message}`);
          }
        })
      );

      for (let j = 0; j < results.length; j++) {
        const result = results[j];
        if (result.status === "fulfilled") {
          matchedRows.push(result.value);
        } else {
          const message =
            result.reason instanceof Error ? result.reason.message : String(result.reason);
          // deno-lint-ignore no-explicit-any
          failures.push({ id: (chunk[j] as any).id, error: message });
          console.error(`[canonical][${workerID}] Match failure:`, {
            // deno-lint-ignore no-explicit-any
            id: (chunk[j] as any).id,
            error: message,
          });
        }
      }
    }

    console.log(`[canonical][${workerID}] Matching: ${Date.now() - matchStart}ms`);

    // Phase 4: Upsert matched canonical names in batches of 250
    const upsertStart = Date.now();
    const UPSERT_BATCH = 250;
    let successfulIds = 0;
    const upsertFailures: Array<{ batchStart: number; error: string }> = [];

    for (let i = 0; i < matchedRows.length; i += UPSERT_BATCH) {
      const batchRows = matchedRows.slice(i, i + UPSERT_BATCH);

      const payload = batchRows.map((row: unknown, idx: number) => {
        // deno-lint-ignore no-explicit-any
        const r = row as any;
        if (idx === 0 && i === 0) {
          console.log(`[canonical][${workerID}] Upsert payload preview:`, {
            id: r.id,
            canonical_product_name: r.canonical_product_name,
            match_stage: r.match_stage,
            match_status: r.match_status,
          });
        }
        return {
          id: r.id,
          canonical_product_name: r.canonical_product_name,
          match_stage: r.match_stage,
        };
      });

      const { data, error } = await supabase
        .from("test_flyer_deals_duplicate")
        .update({ canonical_product_name: null }) // Placeholder; will be updated per-row below
        .in(
          "id",
          payload.map((p) => p.id)
        );

      // Update each row individually to handle null canonical names
      for (const p of payload) {
        const { error: updateError } = await supabase
          .from("test_flyer_deals_duplicate")
          .update({
            canonical_product_name: p.canonical_product_name,
            canonical_product_name_updated_at: p.canonical_product_name ? new Date().toISOString() : null,
            canonical_match_stage: p.match_stage,
          })
          .eq("id", p.id);

        if (updateError) {
          console.error(`[canonical][${workerID}] Update error at id ${p.id}:`, {
            message: updateError.message,
          });
          upsertFailures.push({ batchStart: i, error: updateError.message });
        } else {
          successfulIds++;
        }
      }
    }

    console.log(`[canonical][${workerID}] Upsert: ${Date.now() - upsertStart}ms`);

    const totalTime = Date.now() - startTime;

    console.log(
      `[canonical][${workerID}] Done. ${successfulIds} updated, ${failures.length} match failures, ${upsertFailures.length} upsert failures, ${totalTime}ms`
    );
    console.log(
      `[canonical][${workerID}] Total: ${totalTime}ms | ${rows.length} rows | ${Math.round(
        (rows.length / (totalTime / 1000)) * 1000
      )}/sec`
    );

    const stage1Count   = matchedRows.filter((r: any) => r.match_stage === "stage1_exact").length;
    const stage2Count   = matchedRows.filter((r: any) => r.match_stage === "stage2_jaccard").length;
    const noMatchCount  = matchedRows.filter((r: any) => r.match_status === "no_match").length;

    return new Response(
      JSON.stringify({
        status: "ok",
        worker: workerID,
        fetched: rows.length,
        stage1_matches: stage1Count,
        stage2_matches: stage2Count,
        no_matches:     noMatchCount,
        updated: successfulIds,
        match_failures: failures.length,
        upsert_failures: upsertFailures.length,
        duration_ms: totalTime,
      }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    );
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error(`[canonical][${workerID}] Fatal error:`, message);

    return new Response(
      JSON.stringify({ status: "error", error: message, worker: workerID }),
      { status: 500, headers: { "Content-Type": "application/json" } }
    );
  }finally {
    if (supabase) {
      try {
        await supabase
          .from("canonical_trigger_state")
          .update({ is_processing: false })
          .eq("id", 1);
        console.log(`[canonical][${workerID}] Reset is_processing = false`);
      } catch (resetErr) {
        const m = resetErr instanceof Error ? resetErr.message : String(resetErr);
        console.error(`[canonical][${workerID}] Failed to reset is_processing:`, m);
      }
    }
  }
});
