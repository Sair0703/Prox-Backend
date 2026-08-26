/**
 * Canonical name matching core — Stage1 exact + Stage2 Jaccard fallback.
 * Ported from Python: test/Canonical_name/run_canonical_matching_eval.py
 */

import type { SupabaseClient } from "https://esm.sh/@supabase/supabase-js@2";

// ============================================================================
// TYPES
// ============================================================================

export interface RawRow {
  id: unknown;
  product_name: string | null;
  brand: string | null;
  display_size?: string | null;
  canonical_product_name?: string | null;
}

export interface CanonicalEntry {
  id: unknown;
  canonical_name: string;
  brand: string;
  normalized_signature: string;
}

export interface CanonicalLookup {
  entries: CanonicalEntry[];
  byBrandAndSignature: Map<string, CanonicalEntry>;
}

export interface MatchResult {
  status: "matched" | "no_match" | "ambiguous";
  matchedId?: unknown;
  matchedCanonical?: string;
  matchStage?: "stage1_exact" | "stage2_jaccard";
  stage2Score?: number;
  stage2SecondScore?: number;
  stage2SharedTokens?: number;
}

// ============================================================================
// TEXT UTILITIES
// ============================================================================

function normalizeText(s: string): string {
  let t = s || "";
  // Normalize Unicode
  t = Array.from(t)
    .map((c) => {
      const normalized = c.normalize("NFKC");
      return normalized;
    })
    .join("");
  // Strip trademark marks
  t = t.replace(/™/g, " ").replace(/®/g, " ");
  t = t.replace(/'/g, "'").replace(/`/g, "'");
  // Remove trademark textual artifacts
  t = t.replace(/\b(?:tm|r)\b/gi, " ");
  t = t.replace(/([a-z0-9])tm\b/gi, "$1");
  t = t.replace(/\s+/g, " ").trim().toLowerCase();
  return t;
}

const wordDigitRe = /\b([a-z]{2,})\s+(\d)\b(?!\s*(?:oz|fl(?:\s*oz)?|lb|lbs|g|kg|ml|l|ct|count|pack|pk|qt|pt|gal|each|pkg)\b)/gi;

function collapseWordDigitSpacing(s: string): string {
  let prev: string | null = null;
  let out = s;
  while (prev !== out) {
    prev = out;
    out = out.replace(wordDigitRe, "$1$2");
  }
  return out;
}

const connectorRe = /\b(?:and|&|\+)\b/gi;

function collapseConnectorTokens(s: string): string {
  let t = s.replace(connectorRe, " ");
  t = t.replace(/\s+/g, " ").trim();
  return t;
}

function normalizeSignatureToken(tok: string): string {
  return tok.replace(/'/g, "");
}

function removeBrand(name: string, brand: string | null): string {
  if (!brand) return name;
  let b = normalizeText(brand);
  if (!b) return name;
  b = b.replace(/&/g, " and ");
  b = b.replace(/\s+/g, " ").trim();
  if (!b) return name;

  const phraseVariants: string[] = [b];
  const bNoApos = b.replace(/'/g, "");
  if (bNoApos !== b) phraseVariants.push(bNoApos);

  let out = name;
  for (const phrase of phraseVariants.sort((a, b) => b.length - a.length)) {
    if (!phrase) continue;
    const tokenPattern = phrase
      .split(/\s+/)
      .map((tok) => tok.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
      .join("\\s+");
    out = out.replace(new RegExp(`\\b${tokenPattern}\\b`, "gi"), " ");
    out = out.replace(new RegExp(`\\b${phrase.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`, "gi"), " ");
  }
  return out;
}

function stripDisplaySize(name: string, displaySize: string | null): string {
  if (!displaySize) return name;
  const ds = normalizeText(displaySize);
  if (!ds) return name;

  const variants = new Set([ds]);
  variants.add(ds.replace(/-/g, " "));
  variants.add(ds.replace(/\s+/g, ""));

  let out = name;
  for (const v of Array.from(variants).sort((a, b) => b.length - a.length)) {
    out = out.replace(new RegExp(`\\b${v.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`, "gi"), " ");
  }

  const m = ds.match(/^\s*(\d+(?:\.\d+)?)\s*([a-z][a-z-]*)\s*$/i);
  if (m) {
    const amount = m[1].replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const unit = m[2].replace(/-/g, "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const flexible = `\\b${amount}\\s*[-_/]?\\s*${unit}\\b`;
    out = out.replace(new RegExp(flexible, "gi"), " ");
  }
  return out;
}

const multipackRe = /\b\d+\s*[xX]\s*\d+(?:\.\d+)?\s*(?:oz|fl\s*oz|lb|lbs|g|kg|ml|l|ct|count)\b/gi;
const sizeInlineRe = /\b\d+(?:\.\d+)?\s*(?:oz|fl\s*oz|lb|lbs|g|kg|ml|l|ct|count|pack|pk|qt|pt|gal)\b/gi;

const minimalPromoPhrases = ["value pack", "family size", "limited time"];
const minimalPromoTokens = new Set(["sale", "deal"]);

export function buildNormalizedSignature(
  productName: string,
  brand: string | null,
  displaySize?: string | null,
  isBrand?: boolean | null,
): string {
  let s = normalizeText(productName);
  s = s.replace(/&/g, " and ");
  s = s.replace(/\s+/g, " ").trim();
  s = collapseWordDigitSpacing(s);

  if (isBrand !== false) {
    s = removeBrand(s, brand);
  }

  s = stripDisplaySize(s, displaySize || null);
  s = s.replace(multipackRe, " ");
  s = s.replace(sizeInlineRe, " ");

  for (const phrase of minimalPromoPhrases) {
    s = s.replace(new RegExp(`\\b${phrase.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`, "gi"), " ");
  }

  s = s.replace(/[^a-z0-9\s%'-]/g, " ");
  s = s.replace(/(?:\s*-\s*){2,}/g, " ");
  s = s.replace(/^\s*-\s*|\s*-\s*$/g, " ");
  s = s.replace(/\s+/g, " ").trim();

  s = collapseConnectorTokens(s);

  const tokens: string[] = [];
  for (const tok of s.split(/\s+/)) {
    if (minimalPromoTokens.has(tok)) continue;
    const nt = normalizeSignatureToken(tok);
    if (!nt) continue;
    tokens.push(nt);
  }

  return tokens.join(" ");
}

// ============================================================================
// MATCHING LOGIC
// ============================================================================

function tokenizeSignature(signature: string): Set<string> {
  const tokens = new Set<string>();
  for (const t of (signature || "").toLowerCase().split(/[^a-z0-9]+/)) {
    if (t && t.length > 1) {
      tokens.add(t);
    }
  }
  return tokens;
}

function jaccardAndOverlap(a: Set<string>, b: Set<string>): [number, number] {
  if (a.size === 0 && b.size === 0) return [1.0, 0];
  if (a.size === 0 || b.size === 0) return [0.0, 0];

  const overlap = Array.from(a).filter((x) => b.has(x)).length;
  const union = new Set([...a, ...b]).size;
  const score = union > 0 ? overlap / union : 0.0;
  return [score, overlap];
}

const typeGuardPairs: Array<[string, string]> = [
  ["shampoo", "conditioner"],
  ["conditioner", "shampoo"],
  ["bacon", "sausage"],
  ["sausage", "bacon"],
];

const heatTokens = new Set(["hot", "spicy"]);
const mildTokens = new Set(["mild"]);

function violatesTypeGuard(queryTokens: Set<string>, candidateTokens: Set<string>): boolean {
  // Organic symmetry
  const qOrg = queryTokens.has("organic");
  const cOrg = candidateTokens.has("organic");
  if (qOrg !== cOrg) return true;

  // Flavor / heat
  const qHot = Array.from(heatTokens).some((t) => queryTokens.has(t));
  const cHot = Array.from(heatTokens).some((t) => candidateTokens.has(t));
  const qMild = Array.from(mildTokens).some((t) => queryTokens.has(t));
  const cMild = Array.from(mildTokens).some((t) => candidateTokens.has(t));

  if (qHot && cMild && !cHot) return true;
  if (qMild && cHot && !qHot) return true;

  // Type guard pairs
  for (const [mustHave, mustNotHave] of typeGuardPairs) {
    if (queryTokens.has(mustHave) && !queryTokens.has(mustNotHave)) {
      if (candidateTokens.has(mustNotHave) && !candidateTokens.has(mustHave)) {
        return true;
      }
    }
  }

  return false;
}

export interface Stage2Params {
  minJaccard: number;
  minSharedTokens: number;
  minMargin: number;
}

export function stage2BestCandidate(
  querySignature: string,
  candidates: CanonicalEntry[],
  params: Stage2Params,
): [CanonicalEntry | null, number, number, number] {
  const queryTokens = tokenizeSignature(querySignature);
  if (queryTokens.size === 0) return [null, 0.0, 0.0, 0];

  const scored: Array<[number, number, CanonicalEntry]> = [];
  for (const c of candidates) {
    const cSig = c.normalized_signature || "";
    const cTokens = tokenizeSignature(cSig);
    if (cTokens.size === 0) continue;
    if (violatesTypeGuard(queryTokens, cTokens)) continue;

    const [score, overlap] = jaccardAndOverlap(queryTokens, cTokens);
    scored.push([score, overlap, c]);
  }

  if (scored.length === 0) return [null, 0.0, 0.0, 0];

  scored.sort((a, b) => {
    if (a[0] !== b[0]) return b[0] - a[0];
    return b[1] - a[1];
  });

  const [bestScore, bestOverlap, best] = scored[0];
  const secondScore = scored.length > 1 ? scored[1][0] : 0.0;

  if (bestScore < params.minJaccard) return [null, bestScore, secondScore, bestOverlap];
  if (bestOverlap < params.minSharedTokens) return [null, bestScore, secondScore, bestOverlap];
  if (bestScore - secondScore < params.minMargin) return [null, bestScore, secondScore, bestOverlap];

  return [best, bestScore, secondScore, bestOverlap];
}

// ============================================================================
// DATABASE OPERATIONS
// ============================================================================

export async function loadCanonicalLookup(supabase: SupabaseClient, tableName: string): Promise<CanonicalLookup> {
  const entries: CanonicalEntry[] = [];
  const byBrandAndSignature = new Map<string, CanonicalEntry>();
  let offset = 0;
  const PAGE = 1000;

  while (true) {
    const { data, error } = await supabase
      .from(tableName)
      .select("id, canonical_name, brand, normalized_signature")
      .range(offset, offset + PAGE - 1);

    if (error) {
      console.warn(`Could not load canonical data from ${tableName}:`, error.message);
      break;
    }

    if (!data || data.length === 0) break;

    for (const row of data) {
      const entry: CanonicalEntry = {
        id: row.id,
        canonical_name: row.canonical_name,
        brand: row.brand,
        normalized_signature: row.normalized_signature,
      };
      entries.push(entry);

      const key = `${row.brand}|${row.normalized_signature}`;
      byBrandAndSignature.set(key, entry);
    }

    if (data.length < PAGE) break;
    offset += PAGE;
  }

  return { entries, byBrandAndSignature };
}

export function lookupExactMatch(
  lookup: CanonicalLookup,
  brand: string,
  signature: string,
): CanonicalEntry | null {
  const key = `${brand}|${signature}`;
  return lookup.byBrandAndSignature.get(key) || null;
}

export function lookupBrandCandidates(lookup: CanonicalLookup, brand: string): CanonicalEntry[] {
  return lookup.entries.filter((e) => e.brand === brand);
}
