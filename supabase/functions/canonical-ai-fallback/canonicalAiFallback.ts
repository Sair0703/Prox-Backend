export interface CanonicalAIRow {
  rowIndex: number;
  rowId: string;
  productName: string;
  brand: string;
  displaySize: string | null;
}

export interface CanonicalAIResult {
  canonical_product_name: string | null;
  confidence: number;
}

export interface CanonicalAIStats {
  totalRowsNeeding: number;
  resolved: number;
  lowConfidence: number;
  invalidResults: number;
  totalApiCalls: number;
  cacheHits: number;
  totalTimeMs: number;
  errors: number;
  examples: Array<{ original: string; canonical: string; confidence: number }>;
}

const BATCH_SIZE = 20;
const CONCURRENCY = 5;
const STAGGER_MS = 200;
const OPENAI_BASE_URL = "https://api.openai.com/v1";
const resultCache = new Map<string, CanonicalAIResult>();

function createEmptyStats(): CanonicalAIStats {
  return {
    totalRowsNeeding: 0,
    resolved: 0,
    lowConfidence: 0,
    invalidResults: 0,
    totalApiCalls: 0,
    cacheHits: 0,
    totalTimeMs: 0,
    errors: 0,
    examples: [],
  };
}

function cacheKey(row: CanonicalAIRow): string {
  return [
    row.productName.toLowerCase().trim(),
    row.brand.toLowerCase().trim(),
    (row.displaySize ?? "").toLowerCase().trim(),
  ].join(" | ");
}

function getSystemPrompt(): string {
  return `You generate clean canonical grocery product names.

Rules:
- Return only the canonical product name and a confidence score.
- Remove the brand name. Brand is provided separately and must not appear in the canonical name.
- Remove size, weight, volume, count, unit, multipack, and package quantity information.
- Remove packaging words unless they define the product itself: bag, box, can, bottle, pouch, tub, carton, jar, container, clamshell, package, each.
- Remove marketing or retail fluff: fresh, premium, quality, select, choice, best, great, value, sale, deal, limited time, family size.
- Remove punctuation artifacts and duplicated words.
- Keep meaningful product descriptors: flavor, variety, product type, prep style, cut, diet/allergen terms, and "organic" when it changes the product.
- Keep the name concise. It should be what a person would naturally compare across retailers.
- Use Title Case.
- If the product is too ambiguous to name confidently, return null.

Return a JSON array in the same order as the input. Each item must have exactly:
  canonical_product_name: string or null
  confidence: number from 0.0 to 1.0

Examples:
Input: product_name="Cap'n Crunch Crunch Berries Cereal 11.7 oz", brand="Cap'n Crunch", display_size="11.7 oz"
Output: {"canonical_product_name":"Crunch Berries Cereal","confidence":0.96}

Input: product_name="Heinz 57 Steak Sauce 20 oz", brand="Heinz", display_size="20 oz"
Output: {"canonical_product_name":"57 Steak Sauce","confidence":0.98}

Input: product_name="Fresh Organic Lemons, 2 lb Bag", brand="Fresh", display_size="2 lb"
Output: {"canonical_product_name":"Organic Lemons","confidence":0.92}

Return ONLY the JSON array. No markdown fences, no commentary, no extra text.`;
}

function buildUserMessage(rows: CanonicalAIRow[]): string {
  const items = rows.map((row, index) => {
    const size = row.displaySize ? `, display_size="${row.displaySize}"` : "";
    return `${index + 1}. product_name="${row.productName}", brand="${row.brand}"${size}`;
  });
  return `Generate canonical names for these ${rows.length} products:\n${items.join("\n")}`;
}

function clampConfidence(value: unknown): number {
  if (typeof value !== "number" || Number.isNaN(value)) return 0;
  return Math.max(0, Math.min(1, value));
}

function normalizeCanonicalName(value: string): string {
  return value
    .replace(/\s+/g, " ")
    .replace(/^[\s,;:.-]+|[\s,;:.-]+$/g, "")
    .trim();
}

export function isUsableCanonicalName(
  canonicalName: string | null,
  row: CanonicalAIRow,
  minConfidence: number,
  confidence: number,
): canonicalName is string {
  if (!canonicalName || confidence < minConfidence) return false;
  const clean = normalizeCanonicalName(canonicalName);
  if (clean.length < 3) return false;

  const lower = clean.toLowerCase();
  const brandLower = row.brand.toLowerCase().trim();
  if (lower === brandLower) return false;

  const sizeText = row.displaySize?.toLowerCase().replace(/-/g, " ").trim();
  if (sizeText && lower.includes(sizeText)) return false;

  if (/\b\d+(?:\.\d+)?\s*(?:oz|fl\s*oz|lb|lbs|g|kg|ml|l|ct|count|pack|pk|qt|pt|gal)\b/i.test(clean)) {
    return false;
  }

  return true;
}

async function sendBatch(
  rows: CanonicalAIRow[],
  stats: CanonicalAIStats,
): Promise<(CanonicalAIResult | null)[]> {
  const apiKey = Deno.env.get("OPENAI_API_KEY");
  if (!apiKey) {
    console.warn("[canonical-ai] missing OPENAI_API_KEY; skipping batch");
    stats.errors++;
    return rows.map(() => null);
  }

  const model = Deno.env.get("OPENAI_MODEL") ?? "gpt-4o";
  const body = JSON.stringify({
    model,
    messages: [
      { role: "system", content: getSystemPrompt() },
      { role: "user", content: buildUserMessage(rows) },
    ],
    max_tokens: 2000,
    temperature: 0,
    top_p: 0,
  });

  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      stats.totalApiCalls++;
      const response = await fetch(`${OPENAI_BASE_URL}/chat/completions`, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${apiKey}`,
          "Content-Type": "application/json",
        },
        body,
      });

      if (!response.ok) {
        const text = await response.text();
        console.warn(`[canonical-ai] OpenAI returned ${response.status}: ${text}`);
        if (attempt === 1) {
          stats.errors++;
          return rows.map(() => null);
        }
        continue;
      }

      const json = await response.json();
      const content = json?.choices?.[0]?.message?.content;
      if (!content) {
        console.warn(`[canonical-ai] empty response, attempt ${attempt + 1}`);
        if (attempt === 1) {
          stats.errors++;
          return rows.map(() => null);
        }
        continue;
      }

      const results = parseAIResponse(content, rows.length);
      if (!results) {
        console.warn(`[canonical-ai] JSON parse failed, attempt ${attempt + 1}`);
        if (attempt === 1) {
          stats.errors++;
          return rows.map(() => null);
        }
        continue;
      }

      const padded: (CanonicalAIResult | null)[] = [...results];
      while (padded.length < rows.length) padded.push(null);
      return padded.slice(0, rows.length);
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : String(error);
      console.warn(`[canonical-ai] request error, attempt ${attempt + 1}: ${message}`);
      if (attempt === 1) {
        stats.errors++;
        return rows.map(() => null);
      }
    }
  }

  return rows.map(() => null);
}

function parseAIResponse(raw: string, expectedCount: number): CanonicalAIResult[] | null {
  try {
    let cleaned = raw.trim();
    if (cleaned.startsWith("```")) {
      cleaned = cleaned.replace(/^```(?:json)?\s*/, "").replace(/\s*```$/, "");
    }

    const parsed = JSON.parse(cleaned);
    if (!Array.isArray(parsed)) return null;
    if (parsed.length !== expectedCount) {
      console.warn(`[canonical-ai] expected ${expectedCount} results, got ${parsed.length}`);
    }

    return parsed.map((item: Record<string, unknown>) => ({
      canonical_product_name: typeof item.canonical_product_name === "string"
        ? normalizeCanonicalName(item.canonical_product_name)
        : null,
      confidence: clampConfidence(item.confidence),
    }));
  } catch {
    return null;
  }
}

async function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function runCanonicalAIBatches(
  rows: CanonicalAIRow[],
): Promise<{ results: Map<number, CanonicalAIResult>; stats: CanonicalAIStats }> {
  const startTime = Date.now();
  const stats = createEmptyStats();
  const resultsByIndex = new Map<number, CanonicalAIResult>();
  const uncachedRows: CanonicalAIRow[] = [];

  stats.totalRowsNeeding = rows.length;

  for (const row of rows) {
    const key = cacheKey(row);
    const cached = resultCache.get(key);
    if (cached) {
      stats.cacheHits++;
      resultsByIndex.set(row.rowIndex, cached);
    } else {
      uncachedRows.push(row);
    }
  }

  const batches: CanonicalAIRow[][] = [];
  for (let i = 0; i < uncachedRows.length; i += BATCH_SIZE) {
    batches.push(uncachedRows.slice(i, i + BATCH_SIZE));
  }

  for (let i = 0; i < batches.length; i += CONCURRENCY) {
    const concurrentBatches = batches.slice(i, i + CONCURRENCY);
    const promises = concurrentBatches.map((batch, batchIndex) =>
      (async () => {
        if (batchIndex > 0) {
          await sleep(batchIndex * STAGGER_MS);
        }
        const results = await sendBatch(batch, stats);
        return { batch, results };
      })()
    );

    const settled = await Promise.allSettled(promises);
    for (const outcome of settled) {
      if (outcome.status === "fulfilled") {
        const { batch, results } = outcome.value;
        for (let j = 0; j < batch.length; j++) {
          const result = results[j];
          if (!result) continue;
          const key = cacheKey(batch[j]);
          resultsByIndex.set(batch[j].rowIndex, result);
          resultCache.set(key, result);
        }
      } else {
        stats.errors++;
      }
    }
  }

  stats.totalTimeMs = Date.now() - startTime;
  return { results: resultsByIndex, stats };
}
