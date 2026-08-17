interface RateLimitEntry { count: number; resetTime: number; }
const rateLimitStore = new Map<string, RateLimitEntry>();

export interface RateLimitConfig { maxRequests: number; windowMs: number; identifier?: string; }
export interface RateLimitResult { allowed: boolean; remaining: number; resetIn: number; }

export function checkRateLimit(req: Request, config: RateLimitConfig): RateLimitResult {
  const now = Date.now();
  const identifier = config.identifier || getClientIP(req);
  if (Math.random() < 0.1) cleanupExpiredEntries(now);
  const entry = rateLimitStore.get(identifier);
  if (!entry || now > entry.resetTime) {
    rateLimitStore.set(identifier, { count: 1, resetTime: now + config.windowMs });
    return { allowed: true, remaining: config.maxRequests - 1, resetIn: Math.ceil(config.windowMs / 1000) };
  }
  entry.count++;
  if (entry.count > config.maxRequests) {
    return { allowed: false, remaining: 0, resetIn: Math.ceil((entry.resetTime - now) / 1000) };
  }
  return { allowed: true, remaining: config.maxRequests - entry.count, resetIn: Math.ceil((entry.resetTime - now) / 1000) };
}

function getClientIP(req: Request): string {
  const f = req.headers.get("x-forwarded-for");
  if (f) return f.split(",")[0].trim();
  const r = req.headers.get("x-real-ip");
  if (r) return r;
  const c = req.headers.get("cf-connecting-ip");
  if (c) return c;
  return "unknown";
}

function cleanupExpiredEntries(now: number): void {
  for (const [key, entry] of rateLimitStore.entries()) {
    if (now > entry.resetTime) rateLimitStore.delete(key);
  }
}

export function createRateLimitResponse(result: RateLimitResult, corsHeaders: Record<string, string>): Response {
  return new Response(
    JSON.stringify({ error: "Too many requests. Please try again later.", retryAfter: result.resetIn }),
    { status: 429, headers: { ...corsHeaders, "Content-Type": "application/json", "Retry-After": result.resetIn.toString() } }
  );
}
