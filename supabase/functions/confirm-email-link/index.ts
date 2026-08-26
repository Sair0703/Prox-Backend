import { serve } from "https://deno.land/std@0.190.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.57.2";

const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
const supabaseServiceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const SITE_URL = Deno.env.get("SITE_URL") || "https://joinprox.com";

// Find a user by their custom confirm_token, scanning ALL users (paginated).
async function findUserByConfirmToken(admin: any, token: string) {
  const perPage = 1000;
  let page = 1;
  while (page <= 50) {
    const { data, error } = await admin.auth.admin.listUsers({ page, perPage });
    if (error) throw error;
    const users = data?.users || [];
    if (users.length === 0) break;
    const match = users.find((u: any) => u.user_metadata?.confirm_token === token);
    if (match) return match;
    if (users.length < perPage) break;
    page++;
  }
  return null;
}

function redirect(to: string): Response {
  return new Response(null, { status: 302, headers: { Location: to } });
}

serve(async (req: Request): Promise<Response> => {
  try {
    const url = new URL(req.url);
    const token = url.searchParams.get("token") || url.searchParams.get("confirmation_token");
    if (!token) return redirect(`${SITE_URL}/auth?email_confirmed=invalid`);

    const admin = createClient(supabaseUrl, supabaseServiceKey, {
      auth: { autoRefreshToken: false, persistSession: false },
    });

    const user = await findUserByConfirmToken(admin, token);
    const expiresAt = user?.user_metadata?.confirm_token_expires;
    const isNotExpired = expiresAt ? new Date(expiresAt) > new Date() : false;

    if (!user || !isNotExpired) {
      return redirect(`${SITE_URL}/auth?email_confirmed=expired`);
    }

    // Idempotent: only flip the flag if not already confirmed.
    if (!user.email_confirmed_at) {
      const { error } = await admin.auth.admin.updateUserById(user.id, { email_confirm: true });
      if (error) throw error;
      console.log("Email confirmed for", user.email);
    }

    // Token is intentionally NOT cleared, so the link keeps working on repeated
    // clicks (and email link-scanners) until it naturally expires after 24h.
    return redirect(`${SITE_URL}/auth?email_confirmed=1`);
  } catch (e) {
    console.error("confirm-email-link error:", e);
    return redirect(`${SITE_URL}/auth?email_confirmed=error`);
  }
});
