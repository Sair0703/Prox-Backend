// supabase/functions/resolve-pantry-images/index.ts
import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

type ResolveBody = {
  itemIds: string[];
};

type PantryItem = {
  id: string;
  user_id: string | null;
  name: string;
  brand: string | null;
  quantity: number | null;
  unit: string | null;
};

type WaitlistRow = {
  id: string; // == user_id
  zip_code: string | null;
  preferred_retailers: any; // could be text[] or jsonb array
};

type FlyerDeal = {
  id: number;
  retailer: string | null;
  zip_code: string | null;
  created_at: string | null;
  product_name: string | null;
  product_size: string | null;
  image_link: string | null;
};

function normalizeText(s: string) {
  return s
    .toLowerCase()
    .replace(/[^a-z0-9\s]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function tokenize(s: string) {
  const t = normalizeText(s);
  if (!t) return [];
  return t.split(" ").filter(Boolean);
}

function unique(arr: string[]) {
  return Array.from(new Set(arr));
}

// Parse flyer "8 oz", "1 gal", "16 count", etc.
function parseSize(str: string | null): { value: number; unit: string } | null {
  if (!str) return null;
  const s = normalizeText(str);

  // try patterns: "8 oz", "1 gal", "16 count", "12 fl oz"
  const m = s.match(/(\d+(\.\d+)?)\s*(fl\s*oz|oz|lb|g|kg|ml|l|gal|count|ct|pack)/);
  if (!m) return null;

  const value = Number(m[1]);
  if (!Number.isFinite(value)) return null;

  let unit = m[3].replace(/\s+/g, " ");
  if (unit === "ct") unit = "count";
  if (unit === "fl oz") unit = "floz";

  return { value, unit };
}

function pantryToSize(quantity: number | null, unit: string | null): { value: number; unit: string } | null {
  if (quantity == null || !unit) return null;
  const u = normalizeText(unit);
  const map: Record<string, string> = {
    "count": "count",
    "ct": "count",
    "oz": "oz",
    "lb": "lb",
    "g": "g",
    "kg": "kg",
    "ml": "ml",
    "l": "l",
    "gal": "gal",
    "gallon": "gal",
    "pack": "pack",
  };
  const unitNorm = map[u] ?? u;
  return { value: Number(quantity), unit: unitNorm };
}

// Convert to comparable base units for size comparison
function toBase(size: { value: number; unit: string }): { value: number; unit: "count" | "weight_oz" | "vol_floz" | "other" } {
  const { value, unit } = size;

  if (unit === "count" || unit === "pack") return { value, unit: "count" };

  // weight
  if (unit === "oz") return { value, unit: "weight_oz" };
  if (unit === "lb") return { value: value * 16, unit: "weight_oz" };
  if (unit === "g") return { value: value / 28.3495, unit: "weight_oz" };
  if (unit === "kg") return { value: (value * 1000) / 28.3495, unit: "weight_oz" };

  // volume
  if (unit === "floz") return { value, unit: "vol_floz" };
  if (unit === "ml") return { value: value / 29.5735, unit: "vol_floz" };
  if (unit === "l") return { value: (value * 1000) / 29.5735, unit: "vol_floz" };
  if (unit === "gal") return { value: value * 128, unit: "vol_floz" }; // 1 gal = 128 fl oz

  return { value, unit: "other" };
}

function scoreCandidate(pantry: PantryItem, deal: FlyerDeal): number {
  let score = 0;

  const pn = normalizeText(pantry.name || "");
  const pb = normalizeText(pantry.brand || "");
  const dn = normalizeText(deal.product_name || "");
  const ds = normalizeText(deal.product_size || "");

  // Brand match (0-40)
  if (pb) {
    if (dn.includes(pb)) score += 40;
    else {
      const bTokens = tokenize(pb);
      const hits = bTokens.filter(t => dn.includes(t)).length;
      if (hits >= 2) score += 25;
      else if (hits === 1) score += 15;
    }
  }

  // Size match (0-30)
  const pSize = pantryToSize(pantry.quantity, pantry.unit);
  const dSize = parseSize(ds);
  if (pSize && dSize) {
    const pbv = toBase(pSize);
    const dbv = toBase(dSize);

    if (pbv.unit === dbv.unit && pbv.unit !== "other") {
      const diff = Math.abs(pbv.value - dbv.value);
      if (diff < 0.01) score += 30;
      else if (diff <= Math.max(1, pbv.value * 0.05)) score += 20; // within 5% or 1 unit
      else if (diff <= Math.max(2, pbv.value * 0.15)) score += 10;
    } else {
      // if product_size exists but not parseable or mismatched family, no points
    }
  } else if (pSize && !dSize) {
    // deal has no/odd size; don't penalize, just no points
    score += 0;
  }

  // Name similarity / token overlap (0-25)
  const pTokens = unique(tokenize(pn).filter(t => t.length >= 3));
  const dTokens = new Set(unique(tokenize(dn)));

  if (pTokens.length > 0) {
    const overlap = pTokens.filter(t => dTokens.has(t)).length;
    const ratio = overlap / pTokens.length;
    if (ratio >= 0.6) score += 25;
    else if (ratio >= 0.35) score += 15;
    else if (ratio >= 0.2) score += 8;
    else if (overlap >= 1) score += 3;
  }

  // Recency bonus (0-5)
  if (deal.created_at) {
    const daysAgo = (Date.now() - new Date(deal.created_at).getTime()) / (1000 * 60 * 60 * 24);
    if (daysAgo <= 7) score += 5;
    else if (daysAgo <= 14) score += 3;
    else if (daysAgo <= 30) score += 1;
  }

  return Math.max(0, Math.min(100, score));
}

function parsePreferredRetailers(val: any): string[] {
  if (!val) return [];
  if (Array.isArray(val)) return val.map(String);
  // JSON string or comma separated
  if (typeof val === "string") {
    try {
      const parsed = JSON.parse(val);
      if (Array.isArray(parsed)) return parsed.map(String);
    } catch {
      // fallthrough
    }
    return val.split(",").map(s => s.trim()).filter(Boolean);
  }
  return [];
}

async function fetchCandidates(
  admin: ReturnType<typeof createClient>,
  tier: 1 | 2 | 3,
  pantry: PantryItem,
  zip: string | null,
  preferredRetailers: string[],
): Promise<FlyerDeal[]> {
  const tokens = unique(
    [
      ...tokenize(pantry.name || "").filter(t => t.length >= 4),
      ...tokenize(pantry.brand || "").filter(t => t.length >= 4),
    ].slice(0, 5)
  );

  // Build OR filter for product_name
  const orParts = tokens.map(t => `product_name.ilike.%${t}%`);
  const or = orParts.length ? orParts.join(",") : "";

  let q = admin
    .from("flyer_deals")
    .select("id, retailer, zip_code, created_at, product_name, product_size, image_link")
    .not("image_link", "is", null)
    .gte("created_at", new Date(Date.now() - 60 * 24 * 60 * 60 * 1000).toISOString()) // last 60 days
    .limit(60);

  if (or) q = q.or(or);

  if (tier === 1 || tier === 2) {
    if (zip) q = q.eq("zip_code", zip);
  }

  if (tier === 1) {
    if (preferredRetailers.length > 0) q = q.in("retailer", preferredRetailers);
  }

  const { data, error } = await q;
  if (error) return [];
  return (data || []) as any;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders, status: 200 });

  try {
    if (req.method !== "POST") {
      return new Response(JSON.stringify({ error: "Method not allowed" }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
        status: 405,
      });
    }

    const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
    const anonKey = Deno.env.get("SUPABASE_ANON_KEY")!;
    const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

    const authHeader = req.headers.get("Authorization") ?? "";

    // User client (to identify requester)
    const userClient = createClient(supabaseUrl, anonKey, {
      global: { headers: { Authorization: authHeader } },
    });

    const { data: authData, error: authErr } = await userClient.auth.getUser();
    if (authErr || !authData?.user) {
      return new Response(JSON.stringify({ error: "Unauthorized" }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
        status: 401,
      });
    }

    const body = (await req.json()) as ResolveBody;
    const itemIds = Array.isArray(body?.itemIds) ? body.itemIds.filter(Boolean) : [];

    if (itemIds.length === 0) {
      return new Response(JSON.stringify({ results: {} }), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
        status: 200,
      });
    }

    // Admin client for DB reads/writes
    const admin = createClient(supabaseUrl, serviceKey);

    // Fetch pantry items
    const { data: pantryItemsRaw, error: pantryErr } = await admin
      .from("pantry_tracker")
      .select("id, user_id, name, brand, quantity, unit")
      .in("id", itemIds);

    if (pantryErr) throw pantryErr;

    const pantryItems = (pantryItemsRaw || []) as unknown as PantryItem[];

    // Fetch existing cache to avoid re-resolving too often (TTL)
    const { data: existingCacheRaw } = await admin
      .from("pantry_item_images")
      .select("pantry_item_id, image_link, status, updated_at, match_score")
      .in("pantry_item_id", itemIds);

    const existingMap = new Map<string, { image_link: string | null; status: string; updated_at: string; match_score: number }>();
    (existingCacheRaw || []).forEach((r: any) => {
      existingMap.set(r.pantry_item_id, {
        image_link: r.image_link ?? null,
        status: r.status ?? "matched",
        updated_at: r.updated_at,
        match_score: r.match_score ?? 0,
      });
    });

    // Gather needed user context (zip + preferred)
    const userIds = unique(pantryItems.map(p => p.user_id).filter(Boolean) as string[]);
    const waitlistMap = new Map<string, { zip: string | null; preferred: string[] }>();

    if (userIds.length > 0) {
      const { data: waitlistRows, error: waitlistErr } = await admin
        .from("waitlist")
        .select("id, zip_code, preferred_retailers")
        .in("id", userIds);

      if (waitlistErr) throw waitlistErr;

      (waitlistRows || []).forEach((r: WaitlistRow) => {
        waitlistMap.set(r.id, {
          zip: r.zip_code ?? null,
          preferred: parsePreferredRetailers((r as any).preferred_retailers),
        });
      });
    }

    // Progressive thresholds
    const ACCEPT_SCORE = 75;
    const MIN_SCORE = 60;

    const upserts: any[] = [];
    const results: Record<string, string | null> = {};

    for (const item of pantryItems) {
      // TTL check (don’t re-run if updated recently and matched/not_found)
      const cached = existingMap.get(item.id);
      if (cached) {
        const ageDays = (Date.now() - new Date(cached.updated_at).getTime()) / (1000 * 60 * 60 * 24);
        // matched cache valid 14 days, not_found valid 7 days
        const ttl = cached.status === "not_found" ? 7 : 14;
        if (ageDays <= ttl) {
          results[item.id] = cached.image_link;
          continue;
        }
      }

      const ctx = item.user_id ? waitlistMap.get(item.user_id) : undefined;
      const zip = ctx?.zip ?? null;
      const preferred = ctx?.preferred ?? [];

      let best: { deal: FlyerDeal | null; score: number; tier: 1 | 2 | 3 } = { deal: null, score: 0, tier: 3 };

      for (const tier of [1, 2, 3] as const) {
        const candidates = await fetchCandidates(admin, tier, item, zip, preferred);
        let localBestDeal: FlyerDeal | null = null;
        let localBestScore = 0;

        for (const c of candidates) {
          const s = scoreCandidate(item, c);
          if (s > localBestScore) {
            localBestScore = s;
            localBestDeal = c;
          }
        }

        if (localBestScore > best.score) {
          best = { deal: localBestDeal, score: localBestScore, tier };
        }

        // stop early if very good
        if (best.score >= ACCEPT_SCORE) break;

        // if score is weak, expand
        // tiers continue automatically
      }

      // If best still below MIN_SCORE, treat as not found
      if (!best.deal || best.score < MIN_SCORE) {
        results[item.id] = null;
        upserts.push({
          pantry_item_id: item.id,
          matched_flyer_deal_id: null,
          image_link: null,
          match_score: best.score,
          match_tier: best.tier,
          matched_product_name: best.deal?.product_name ?? null,
          matched_product_size: best.deal?.product_size ?? null,
          matched_retailer: best.deal?.retailer ?? null,
          status: "not_found",
          updated_at: new Date().toISOString(),
        });
        continue;
      }

      results[item.id] = best.deal.image_link ?? null;

      upserts.push({
        pantry_item_id: item.id,
        matched_flyer_deal_id: best.deal.id,
        image_link: best.deal.image_link ?? null,
        match_score: best.score,
        match_tier: best.tier,
        matched_product_name: best.deal.product_name ?? null,
        matched_product_size: best.deal.product_size ?? null,
        matched_retailer: best.deal.retailer ?? null,
        status: "matched",
        updated_at: new Date().toISOString(),
      });
    }

    if (upserts.length > 0) {
      await admin
        .from("pantry_item_images")
        .upsert(upserts, { onConflict: "pantry_item_id" });
    }

    return new Response(JSON.stringify({ results }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
      status: 200,
    });
  } catch (e) {
    return new Response(JSON.stringify({ error: "Internal error", details: String(e) }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
      status: 500,
    });
  }
});
