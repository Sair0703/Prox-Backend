import { createClient } from "npm:@supabase/supabase-js@2";

const GH = "https://api.github.com";

function json(obj: unknown, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json" } });
}

async function gh(path: string, token: string) {
  const resp = await fetch(`${GH}${path}`, {
    headers: {
      Authorization: `Bearer ${token}`,
      "User-Agent": "prox-digest-relay",
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
    },
  });
  if (!resp.ok) {
    const t = await resp.text();
    return { ok: false, status: resp.status, error: t.slice(0, 200), data: null as unknown };
  }
  return { ok: true, status: resp.status, data: await resp.json() };
}

Deno.serve(async (req) => {
  try {
    const supabase = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);
    const { data: cfg, error: cfgErr } = await supabase.from("github_config").select("*").eq("id", 1).single();
    if (cfgErr || !cfg) return json({ ok: false, step: "config", error: cfgErr?.message || "no config" }, 500);
    const token = cfg.token as string;

    const url = new URL(req.url);
    const hours = Math.min(parseInt(url.searchParams.get("hours") || "48", 10) || 48, 336);
    const sinceMs = Date.now() - hours * 3600 * 1000;
    const sinceISO = new Date(sinceMs).toISOString();

    // List repos accessible to the token, most-recently-pushed first.
    const reposResp = await gh(`/user/repos?per_page=100&sort=pushed&affiliation=owner,collaborator,organization_member`, token);
    if (!reposResp.ok) return json({ ok: false, step: "list_repos", status: reposResp.status, error: reposResp.error }, 502);
    const repos = (reposResp.data as any[]).filter((r) => new Date(r.pushed_at).getTime() >= sinceMs).slice(0, 25);

    const rows: any[] = [];
    const summary: any[] = [];
    for (const r of repos) {
      const full = r.full_name as string;
      let commitCount = 0, prCount = 0;
      const c = await gh(`/repos/${full}/commits?since=${encodeURIComponent(sinceISO)}&per_page=30`, token);
      if (c.ok && Array.isArray(c.data)) {
        for (const cm of c.data as any[]) {
          const when = cm.commit?.author?.date || cm.commit?.committer?.date;
          if (!when || new Date(when).getTime() < sinceMs) continue;
          rows.push({
            id: `commit:${full}:${cm.sha}`,
            repo: full,
            kind: "commit",
            ref: (cm.sha as string).slice(0, 7),
            title: (cm.commit?.message || "").split("\n")[0].slice(0, 300),
            author: cm.author?.login || cm.commit?.author?.name || "unknown",
            state: null,
            url: cm.html_url,
            event_time: when,
          });
          commitCount++;
        }
      }
      const p = await gh(`/repos/${full}/pulls?state=all&sort=updated&direction=desc&per_page=20`, token);
      if (p.ok && Array.isArray(p.data)) {
        for (const pr of p.data as any[]) {
          if (!pr.updated_at || new Date(pr.updated_at).getTime() < sinceMs) continue;
          rows.push({
            id: `pr:${full}:${pr.number}`,
            repo: full,
            kind: "pr",
            ref: String(pr.number),
            title: (pr.title || "").slice(0, 300),
            author: pr.user?.login || "unknown",
            state: pr.merged_at ? "merged" : pr.state,
            url: pr.html_url,
            event_time: pr.updated_at,
          });
          prCount++;
        }
      }
      summary.push({ repo: full, commits: commitCount, prs: prCount });
    }

    if (rows.length) {
      await supabase.from("github_activity").upsert(rows, { onConflict: "id" });
    }
    await supabase.from("github_activity").delete().lt("event_time", new Date(Date.now() - 14 * 24 * 3600 * 1000).toISOString());

    return json({ ok: true, since: sinceISO, repos_scanned: repos.length, rows_upserted: rows.length, summary });
  } catch (e) {
    return json({ ok: false, error: String((e as Error)?.message || e) }, 500);
  }
});
