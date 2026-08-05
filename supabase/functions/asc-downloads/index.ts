import { createClient } from "npm:@supabase/supabase-js@2";
import { SignJWT, importPKCS8 } from "npm:jose@5";

const APPLE_BASE = "https://api.appstoreconnect.apple.com/v1/salesReports";

function json(obj: unknown, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json" } });
}

function pacificDateMinus(days: number): string {
  const now = new Date();
  const fmt = new Intl.DateTimeFormat("en-CA", { timeZone: "America/Los_Angeles", year: "numeric", month: "2-digit", day: "2-digit" });
  const pacificNowStr = fmt.format(now);
  const base = new Date(pacificNowStr + "T12:00:00Z");
  base.setUTCDate(base.getUTCDate() - days);
  return base.toISOString().slice(0, 10);
}

function parseTsv(text: string, reportDate: string) {
  const lines = text.split("\n").filter((l) => l.trim().length);
  if (!lines.length) return { report_date: reportDate, status: 200, units: 0, downloads: 0, updates: 0, proceeds: 0, byType: {} as Record<string, number> };
  const header = lines[0].split("\t");
  const idx = (name: string) => header.findIndex((h) => h.trim().toLowerCase() === name.toLowerCase());
  const iUnits = idx("Units");
  const iType = idx("Product Type Identifier");
  const iProceeds = idx("Developer Proceeds");
  let units = 0, downloads = 0, updates = 0, proceeds = 0;
  const byType: Record<string, number> = {};
  for (let i = 1; i < lines.length; i++) {
    const c = lines[i].split("\t");
    const u = parseInt((c[iUnits] || "0").trim(), 10) || 0;
    const pt = (c[iType] || "").trim();
    const pr = parseFloat((c[iProceeds] || "0").trim()) || 0;
    units += u;
    byType[pt] = (byType[pt] || 0) + u;
    if (pt.startsWith("7")) updates += u; else downloads += u;
    proceeds += pr * u;
  }
  return { report_date: reportDate, status: 200, units, downloads, updates, proceeds: Number(proceeds.toFixed(2)), byType };
}

async function fetchReport(token: string, vendor: string, reportDate: string) {
  const params = new URLSearchParams({
    "filter[frequency]": "DAILY",
    "filter[reportType]": "SALES",
    "filter[reportSubType]": "SUMMARY",
    "filter[vendorNumber]": vendor,
    "filter[reportDate]": reportDate,
    "filter[version]": "1_1",
  });
  const resp = await fetch(`${APPLE_BASE}?${params.toString()}`, {
    headers: { Authorization: `Bearer ${token}`, Accept: "application/a-gzip" },
  });
  if (resp.status === 404) {
    return { report_date: reportDate, status: 404, units: 0, downloads: 0, updates: 0, proceeds: 0, byType: {}, note: "no sales for date" };
  }
  if (!resp.ok) {
    const t = await resp.text();
    return { report_date: reportDate, status: resp.status, error: t.slice(0, 400), units: null, downloads: null, updates: null, proceeds: null, byType: {} };
  }
  const ds = new DecompressionStream("gzip");
  const stream = (resp.body as ReadableStream<Uint8Array>).pipeThrough(ds);
  const text = await new Response(stream).text();
  return parseTsv(text, reportDate);
}

Deno.serve(async (req) => {
  try {
    const supabase = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);
    const { data: cfg, error: cfgErr } = await supabase.from("asc_config").select("*").eq("id", 1).single();
    if (cfgErr || !cfg) return json({ ok: false, step: "config", error: cfgErr?.message || "no config" }, 500);
    if (!cfg.vendor_number) return json({ ok: false, step: "config", error: "vendor_number missing" }, 500);

    const pk = await importPKCS8(cfg.private_key, "ES256");
    const now = Math.floor(Date.now() / 1000);
    const token = await new SignJWT({ aud: "appstoreconnect-v1" })
      .setProtectedHeader({ alg: "ES256", kid: cfg.key_id, typ: "JWT" })
      .setIssuer(cfg.issuer_id)
      .setIssuedAt(now)
      .setExpirationTime(now + 1200)
      .sign(pk);

    const url = new URL(req.url);
    const days = Math.min(parseInt(url.searchParams.get("days") || "3", 10) || 3, 14);
    const results = [];
    for (let i = 1; i <= days; i++) {
      const d = pacificDateMinus(i);
      const r = await fetchReport(token, cfg.vendor_number, d);
      results.push(r);
      if (r.units !== null) {
        await supabase.from("app_store_metrics").upsert({
          report_date: d,
          units: r.units,
          downloads: r.downloads,
          updates: r.updates,
          proceeds: r.proceeds,
          raw: r.byType,
          fetched_at: new Date().toISOString(),
        }, { onConflict: "report_date" });
      }
    }
    return json({ ok: true, results });
  } catch (e) {
    return json({ ok: false, error: String((e as Error)?.message || e) }, 500);
  }
});
