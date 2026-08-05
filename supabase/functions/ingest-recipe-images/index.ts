import "jsr:@supabase/functions-js/edge-runtime.d.ts";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const BUCKET = "recipes";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
};

const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36";
const BOT_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)";

const hdr = { apikey: SERVICE_KEY, Authorization: `Bearer ${SERVICE_KEY}` };

async function ogFrom(pageUrl: string, ua: string): Promise<string | null> {
  try {
    const r = await fetch(pageUrl, { headers: { "User-Agent": ua, Accept: "text/html" } });
    if (!r.ok) return null;
    const html = await r.text();
    const m =
      html.match(/<meta[^>]+property=["']og:image["'][^>]+content=["']([^"']+)["']/i) ||
      html.match(/<meta[^>]+content=["']([^"']+)["'][^>]+property=["']og:image["']/i);
    return m ? m[1].replace(/&amp;/g, "&") : null;
  } catch (_) {
    return null;
  }
}

async function resolveTikTok(sourceUrl: string): Promise<string | null> {
  let full = sourceUrl;
  if (/tiktok\.com\/t\//.test(sourceUrl)) {
    try {
      const r = await fetch(sourceUrl, { redirect: "follow", headers: { "User-Agent": UA } });
      full = r.url || sourceUrl;
    } catch (_) { /* keep */ }
  }
  const m = full.match(/\/(video|photo)\/(\d+)/);
  const canonical = m ? `https://www.tiktok.com/@tiktok/video/${m[2]}` : full;
  try {
    const r = await fetch(`https://www.tiktok.com/oembed?url=${encodeURIComponent(canonical)}`, {
      headers: { "User-Agent": UA },
    });
    if (r.ok) {
      const j = await r.json();
      if (j.thumbnail_url) return j.thumbnail_url;
    }
  } catch (_) { /* fall through */ }
  // fallback: scrape og:image off the video page (works for posts oEmbed rejects)
  return await ogFrom(full, BOT_UA);
}

async function resolveInstagram(sourceUrl: string): Promise<string | null> {
  const m = sourceUrl.match(/instagram\.com\/(?:reel|reels|p|tv)\/([A-Za-z0-9_-]+)/);
  if (m) {
    // public, no-auth image endpoint -> redirects straight to the full-size media
    const direct = `https://www.instagram.com/p/${m[1]}/media/?size=l`;
    try {
      const r = await fetch(direct, { redirect: "follow", headers: { "User-Agent": BOT_UA } });
      const ct = r.headers.get("content-type") || "";
      if (r.ok && ct.startsWith("image/")) return r.url || direct;
    } catch (_) { /* fall through */ }
  }
  // og:image works when Instagram serves the crawler view
  return (await ogFrom(sourceUrl, BOT_UA)) ?? (await ogFrom(sourceUrl, UA));
}

async function resolveFacebook(sourceUrl: string): Promise<string | null> {
  return (await ogFrom(sourceUrl, BOT_UA)) ?? (await ogFrom(sourceUrl, UA));
}

async function resolveImage(sourceUrl: string): Promise<string | null> {
  if (/tiktok\.com/.test(sourceUrl)) return await resolveTikTok(sourceUrl);
  if (/instagram\.com/.test(sourceUrl)) return await resolveInstagram(sourceUrl);
  if (/facebook\.com/.test(sourceUrl)) return await resolveFacebook(sourceUrl);
  return await ogFrom(sourceUrl, BOT_UA);
}

async function host(id: number, src: string): Promise<Record<string, unknown>> {
  let bytes: ArrayBuffer;
  let contentType = "image/jpeg";
  try {
    const imgRes = await fetch(src, { headers: { "User-Agent": UA, Referer: "https://www.tiktok.com/" } });
    if (!imgRes.ok) return { id, ok: false, step: "download", status: imgRes.status };
    contentType = imgRes.headers.get("content-type")?.split(";")[0] || "image/jpeg";
    if (!/^image\//.test(contentType)) contentType = "image/jpeg";
    bytes = await imgRes.arrayBuffer();
  } catch (_) {
    return { id, ok: false, step: "download" };
  }
  if (bytes.byteLength < 1000) return { id, ok: false, step: "too_small" };

  const ext = contentType.includes("png") ? "png" : contentType.includes("webp") ? "webp" : "jpg";
  const path = `${id}.${ext}`;

  const up = await fetch(`${SUPABASE_URL}/storage/v1/object/${BUCKET}/${path}`, {
    method: "POST",
    headers: { ...hdr, "Content-Type": contentType, "x-upsert": "true" },
    body: bytes,
  });
  if (!up.ok) return { id, ok: false, step: "upload", status: up.status };

  const publicUrl = `${SUPABASE_URL}/storage/v1/object/public/${BUCKET}/${path}`;
  const patch = await fetch(`${SUPABASE_URL}/rest/v1/recipes?id=eq.${id}`, {
    method: "PATCH",
    headers: { ...hdr, "Content-Type": "application/json", Prefer: "return=minimal" },
    body: JSON.stringify({ image_url: publicUrl }),
  });
  if (!patch.ok) return { id, ok: false, step: "db", status: patch.status };

  return { id, ok: true, bytes: bytes.byteLength };
}

async function remainingCount(): Promise<string> {
  const r = await fetch(
    `${SUPABASE_URL}/rest/v1/recipes?image_url=like.https://api.openverse.org*&select=id`,
    { headers: { ...hdr, Prefer: "count=exact", Range: "0-0" } },
  );
  return r.headers.get("content-range")?.split("/")[1] ?? "?";
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });

  let body: any = {};
  try { body = await req.json(); } catch (_) { /* defaults */ }

  // MODE 1: caller supplies resolved image URLs or base64 -> [{id, src}] / [{id, b64, content_type}]
  if (Array.isArray(body?.items)) {
    const results: any[] = [];
    for (const it of body.items) {
      if (it?.id && it?.b64) {
        const ct = String(it.content_type || "image/jpeg");
        const bin = Uint8Array.from(atob(String(it.b64)), (c) => c.charCodeAt(0));
        const ext = ct.includes("png") ? "png" : ct.includes("webp") ? "webp" : "jpg";
        const path = `${it.id}.${ext}`;
        const up = await fetch(`${SUPABASE_URL}/storage/v1/object/${BUCKET}/${path}`, {
          method: "POST",
          headers: { ...hdr, "Content-Type": ct, "x-upsert": "true" },
          body: bin,
        });
        if (!up.ok) { results.push({ id: it.id, ok: false, step: "upload", status: up.status }); continue; }
        const publicUrl = `${SUPABASE_URL}/storage/v1/object/public/${BUCKET}/${path}`;
        await fetch(`${SUPABASE_URL}/rest/v1/recipes?id=eq.${it.id}`, {
          method: "PATCH",
          headers: { ...hdr, "Content-Type": "application/json", Prefer: "return=minimal" },
          body: JSON.stringify({ image_url: publicUrl }),
        });
        results.push({ id: it.id, ok: true, bytes: bin.byteLength });
        continue;
      }
      if (!it?.id || !it?.src) { results.push({ id: it?.id, ok: false, step: "input" }); continue; }
      results.push(await host(Number(it.id), String(it.src)));
    }
    return new Response(
      JSON.stringify({ mode: "items", hosted: results.filter((r) => r.ok).length, remaining: await remainingCount(), results }),
      { headers: { ...CORS, "Content-Type": "application/json" } },
    );
  }

  // MODE 2: resolve server-side
  const limit = Math.min(Number(body?.limit ?? 10), 40);
  const dryRun = !!body?.dry_run;
  const onlyIds: number[] | null = Array.isArray(body?.ids) ? body.ids.map(Number) : null;

  const filter = onlyIds
    ? `id=in.(${onlyIds.join(",")})`
    : `image_url=like.https://api.openverse.org*`;

  const listRes = await fetch(
    `${SUPABASE_URL}/rest/v1/recipes?${filter}&select=id,source_url&order=id.asc&limit=${limit}`,
    { headers: hdr },
  );
  const rows: Array<{ id: number; source_url: string }> = await listRes.json();

  const results: Array<Record<string, unknown>> = [];
  for (const row of rows) {
    const src = await resolveImage(row.source_url);
    if (!src) { results.push({ id: row.id, ok: false, step: "resolve" }); continue; }
    if (dryRun) { results.push({ id: row.id, ok: true, step: "resolved_only" }); continue; }
    results.push(await host(row.id, src));
  }

  return new Response(
    JSON.stringify({ mode: "resolve", processed: rows.length, hosted: results.filter((r) => r.ok).length, remaining: await remainingCount(), results }),
    { headers: { ...CORS, "Content-Type": "application/json" } },
  );
});
