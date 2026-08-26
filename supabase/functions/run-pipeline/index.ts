import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import { enrichRow, loadEnrichmentContext, toDbRow, EnrichmentContext, RawRow } from "./enrichmentCore.ts";

// Module-level context cache — persists for the lifetime of the Edge Function worker
let cachedContext: EnrichmentContext | null = null;
let contextCachedAt = 0;
const CONTEXT_TTL_MS = 5 * 60 * 1000; // 5 minutes

// deno-lint-ignore no-explicit-any
async function getContext(supabase: any): Promise<EnrichmentContext> {
  const now = Date.now();
  if (cachedContext && (now - contextCachedAt) < CONTEXT_TTL_MS) {
    console.log(`[pipeline] Using cached context (${Math.round((now - contextCachedAt) / 1000)}s old)`);
    return cachedContext;
  }
  console.log(`[pipeline] Loading fresh context...`);
  cachedContext = await loadEnrichmentContext(supabase);
  contextCachedAt = now;
  return cachedContext;
}

serve(async (req) => {
  const startTime = Date.now();

  // Declared before try so the catch block can access it for claim release
  let workerID = "uninitialized";
  // deno-lint-ignore no-explicit-any
  let supabase: any = null;

  try {
    const supabaseUrl = Deno.env.get("SUPABASE_URL");
    const serviceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");

    if (!supabaseUrl) throw new Error("Missing SUPABASE_URL");
    if (!serviceRoleKey) throw new Error("Missing SUPABASE_SERVICE_ROLE_KEY");

    supabase = createClient(supabaseUrl, serviceRoleKey);
    workerID = `worker_${crypto.randomUUID()}`;

    let batch_size = 500;
    try {
      const body = await req.json();
      batch_size = body?.batch_size ?? 500;
    } catch {
      batch_size = 500;
    }
    console.log(`[pipeline][${workerID}] Triggered. Processing up to ${batch_size} rows...`);

    // Phase 1: Load context (cached)
    const contextStart = Date.now();
    const context = await getContext(supabase);
    console.log(`[pipeline][${workerID}] Context loaded in ${Date.now() - contextStart}ms`);

    // Phase 2: Claim rows atomically. The SQL RPC enforces the current flyer week.
    const fetchStart = Date.now();
    const { data: rows, error: claimError } = await supabase.rpc("claim_pipeline_rows_v2", {
      p_worker_id: workerID,
      p_batch_size: batch_size,
    });

    if (claimError) throw new Error(`Claim error: ${claimError.message}`);

    console.log(`[pipeline][${workerID}] Claimed ${rows?.length || 0} rows in ${Date.now() - fetchStart}ms`);

    if (!rows || rows.length === 0) {
      console.log(`[pipeline][${workerID}] No rows available in the current flyer week.`);
      return new Response(
        JSON.stringify({ status: "no_rows", worker: workerID }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      );
    }

    console.log(`[pipeline][${workerID}] First claimed row id:`, rows[0]?.id);

    // Phase 3: Deterministic enrichment — parallel chunks of 50
    const enrichStart = Date.now();
    // deno-lint-ignore no-explicit-any
    const enrichedRows: any[] = [];
    const failures: Array<{ id: unknown; error: string }> = [];
    const CHUNK_SIZE = 50;

    for (let i = 0; i < rows.length; i += CHUNK_SIZE) {
      const chunk = rows.slice(i, i + CHUNK_SIZE);

      const results = await Promise.allSettled(
        chunk.map((row: unknown) => enrichRow(row as RawRow, context))
      );

      for (let j = 0; j < results.length; j++) {
        const result = results[j];
        if (result.status === "fulfilled") {
          enrichedRows.push(result.value);
        } else {
          const message = result.reason instanceof Error ? result.reason.message : String(result.reason);
          // deno-lint-ignore no-explicit-any
          failures.push({ id: (chunk[j] as any).id, error: message });
          console.error(`[pipeline][${workerID}] Enrich failure:`, {
            // deno-lint-ignore no-explicit-any
            id: (chunk[j] as any).id,
            error: message,
          });
        }
      }
    }
    console.log(`[pipeline][${workerID}] Deterministic enrichment: ${Date.now() - enrichStart}ms`);

    // Size AI fallback disabled — high failure rate not worth the API calls
    const deterministicOnly = enrichedRows.filter(r =>
      !r._needsAIBrand && !r._needsAICategory
    );
    const needsAI = enrichedRows.filter(r =>
      r._needsAIBrand || r._needsAICategory
    );

    console.log(`[pipeline][${workerID}] ${deterministicOnly.length} rows fully resolved deterministically`);
    console.log(`[pipeline][${workerID}] ${needsAI.length} rows need AI fallback`);
    console.log(`[pipeline][${workerID}] AI fallback candidates: ${needsAI.length} / ${enrichedRows.length} enriched rows`);

    // Phase 4: Defer unresolved brand/category rows to the dedicated AI queue.
    // The AI queue can claim these rows after processed_at is populated below.
    console.log(
      `[pipeline][${workerID}] Deferred ${needsAI.length} unresolved rows to the brand/category AI queue.`,
    );

    console.log(`[pipeline][${workerID}] Enrichment complete. ${enrichedRows.length} succeeded, ${failures.length} failed.`);

    if (enrichedRows.length === 0) {
      // Release claims so another worker can retry
      await supabase
        .from("flyer_deals")
        .update({ processing_worker: null, processing_started_at: null })
        .eq("processing_worker", workerID);

      return new Response(
        JSON.stringify({
          status: "ok",
          worker: workerID,
          processed: 0,
          enrich_failures: failures.length,
          upsert_failures: 0,
          duration_ms: Date.now() - startTime,
          note: "No rows reached the upsert stage.",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      );
    }

    // Phase 5: Upsert in batches of 250
    const upsertStart = Date.now();
    const UPSERT_BATCH = 250;
    const successfulIds: unknown[] = [];
    const upsertFailures: Array<{ batchStart: number; error: string }> = [];

    for (let i = 0; i < enrichedRows.length; i += UPSERT_BATCH) {
      const batchRows = enrichedRows.slice(i, i + UPSERT_BATCH);

      const payload = batchRows.map((row: unknown, idx: number) => {
        // deno-lint-ignore no-explicit-any
        const dbRow = toDbRow(row as any);

        if (idx === 0 && i === 0) {
          console.log(`[pipeline][${workerID}] toDbRow payload preview:`, {
            id: dbRow?.id,
            title: dbRow?.title,
            brand: dbRow?.brand,
            normalized_title: dbRow?.normalized_title,
            category: dbRow?.category,
            processed_at: dbRow?.processed_at,
            keys: Object.keys(dbRow ?? {}),
          });
        }

        return dbRow;
      });

      const { data, error } = await supabase
        .from("flyer_deals")
        .upsert(payload, { onConflict: "id" })
        .select("id");

      if (error) {
        console.error(`[pipeline][${workerID}] Upsert error at batch ${i}:`, {
          message: error.message,
          details: error.details,
          hint: error.hint,
          code: error.code,
        });
        upsertFailures.push({ batchStart: i, error: error.message });
      } else {
        console.log(`[pipeline][${workerID}] Upsert success at batch ${i}. Returned rows:`, data?.length);
        for (const r of data ?? []) {
          successfulIds.push(r.id);
        }
      }
    }
    console.log(`[pipeline][${workerID}] Upsert: ${Date.now() - upsertStart}ms`);

    // Phase 6: Mark processed and release worker claims
    const markStart = Date.now();
    const processedIds = successfulIds;
    const ID_BATCH = 250;
    let processedAtUpdatedCount = 0;
    const processedAtFailures: Array<{ batchStart: number; error: string }> = [];

    for (let i = 0; i < processedIds.length; i += ID_BATCH) {
      const idBatch = processedIds.slice(i, i + ID_BATCH);

      const { data, error } = await supabase
        .from("flyer_deals")
        .update({
          processed_at: new Date().toISOString(),
          processing_worker: null,
        })
        .in("id", idBatch)
        .select("id, processed_at");

      if (error) {
        console.error(`[pipeline][${workerID}] processed_at update error:`, {
          message: error.message,
          details: error.details,
          hint: error.hint,
          code: error.code,
        });
        processedAtFailures.push({ batchStart: i, error: error.message });
      } else {
        processedAtUpdatedCount += data?.length ?? 0;
      }
    }
    console.log(`[pipeline][${workerID}] Mark processed: ${Date.now() - markStart}ms`);

    if (successfulIds.length > 0) {
      const firstId = successfulIds[0];
      const { data: verifyRow, error: verifyError } = await supabase
        .from("flyer_deals")
        .select("*")
        .eq("id", firstId)
        .single();

      if (verifyError) {
        console.error(`[pipeline][${workerID}] Post-write verification error:`, {
          message: verifyError.message,
          details: verifyError.details,
          hint: verifyError.hint,
          code: verifyError.code,
        });
      } else {
        console.log(`[pipeline][${workerID}] Post-write verification row:`, verifyRow);
      }
    }

    await supabase.rpc("reset_pipeline_processing");

    const totalTime = Date.now() - startTime;

    console.log(`[pipeline][${workerID}] Done. ${successfulIds.length} upserted, ${upsertFailures.length} upsert batch failures, ${failures.length} enrich failures, ${processedAtUpdatedCount} processed_at updates, ${totalTime}ms`);
    console.log(`[pipeline][${workerID}] Total: ${totalTime}ms | ${rows.length} rows | ${Math.round(rows.length / (totalTime / 1000))}/sec`);

    return new Response(
      JSON.stringify({
        status: "ok",
        worker: workerID,
        fetched: rows.length,
        enriched: enrichedRows.length,
        processed: successfulIds.length,
        processed_at_updated: processedAtUpdatedCount,
        deterministic_resolved: deterministicOnly.length,
        ai_queue_candidates: needsAI.length,
        enrich_failures: failures.length,
        upsert_batch_failures: upsertFailures.length,
        processed_at_failures: processedAtFailures.length,
        duration_ms: totalTime,
        enrich_failure_rows: failures,
        upsert_failures: upsertFailures,
        processed_at_failure_batches: processedAtFailures,
      }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    );
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error(`[pipeline][${workerID}] Fatal error:`, message);

    // Release claimed rows so another worker can retry them
    try {
      if (workerID !== "uninitialized" && supabase) {
        await supabase
          .from("flyer_deals")
          .update({ processing_worker: null, processing_started_at: null })
          .eq("processing_worker", workerID);
        console.log(`[pipeline][${workerID}] Released claims`);
      }
    } catch (_) {
      console.error(`[pipeline][${workerID}] Failed to release claims`);
    }

    try {
      if (supabase) await supabase.rpc("reset_pipeline_processing");
    } catch (_) {
      // best-effort, ignore
    }

    return new Response(
      JSON.stringify({ status: "error", error: message, worker: workerID }),
      { status: 500, headers: { "Content-Type": "application/json" } }
    );
  }
});
