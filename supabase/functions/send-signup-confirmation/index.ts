import { serve } from "https://deno.land/std@0.190.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.57.2";
import { Resend } from "npm:resend@2.0.0";
import { checkRateLimit, createRateLimitResponse } from "./rate-limiter.ts";

const resend = new Resend(Deno.env.get("RESEND_API_KEY"));
const supabaseUrl = Deno.env.get("SUPABASE_URL") ?? "";
const supabaseServiceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const SITE_URL = Deno.env.get("SITE_URL") || "https://joinprox.com";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const RATE_LIMIT_CONFIG = { maxRequests: 5, windowMs: 60 * 1000 };

interface SignupConfirmationRequest {
  email: string;
  userName?: string;
}

function generateToken(): string {
  const array = new Uint8Array(32);
  crypto.getRandomValues(array);
  return Array.from(array, (b) => b.toString(16).padStart(2, "0")).join("");
}

// Find a user by email, scanning ALL users (paginated). The old version only
// checked the first 50 users, which could miss brand-new signups.
async function findUserByEmail(admin: any, email: string) {
  const perPage = 1000;
  let page = 1;
  while (page <= 10) {
    const { data, error } = await admin.auth.admin.listUsers({ page, perPage });
    if (error) throw error;
    const users = data?.users || [];
    if (users.length === 0) break;
    const match = users.find((u: any) => u.email?.toLowerCase() === email.toLowerCase());
    if (match) return match;
    if (users.length < perPage) break;
    page++;
  }
  return null;
}

const handler = async (req: Request): Promise<Response> => {
  if (req.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders });
  }

  const rateLimitResult = checkRateLimit(req, RATE_LIMIT_CONFIG);
  if (!rateLimitResult.allowed) {
    return createRateLimitResponse(rateLimitResult, corsHeaders);
  }

  try {
    const { email, userName }: SignupConfirmationRequest = await req.json();
    const displayName = userName || email.split("@")[0];

    const admin = createClient(supabaseUrl, supabaseServiceKey, {
      auth: { autoRefreshToken: false, persistSession: false },
    });

    const user = await findUserByEmail(admin, email);
    if (!user) throw new Error("User not found");

    // Mint a custom confirmation token, valid for 24 hours and reusable.
    const confirmToken = generateToken();
    const expiresAt = new Date(Date.now() + 86400000); // 24 hours
    const { error: updateError } = await admin.auth.admin.updateUserById(user.id, {
      user_metadata: {
        confirm_token: confirmToken,
        confirm_token_expires: expiresAt.toISOString(),
      },
    });
    if (updateError) throw updateError;

    // Link points to our own server-side confirm handler (works on unlimited
    // clicks within 24h; immune to email link-scanners consuming a one-time token).
    const confirmationUrl = `${supabaseUrl}/functions/v1/confirm-email-link?token=${confirmToken}`;

    await resend.emails.send({
      from: "Prox <alston@joinprox.com>",
      to: [email],
      subject: "Welcome to Prox - Confirm Your Email",
      html: `
        <!DOCTYPE html>
        <html>
        <head><meta charset="utf-8"></head>
        <body style="font-family: 'Roboto', Arial, sans-serif; background-color:#FFFFFF; margin:0; padding:0;">
          <div style="max-width:600px; margin:30px auto; background-color:#082517; color:#FFFFFF; padding:40px; border-radius:12px;">
            <h2 style="color:#60FF6F; font-size:28px; font-weight:700;">Welcome to Prox 👋</h2>
            <p style="font-size:16px; line-height:1.6;">Hi ${displayName}!</p>
            <p style="font-size:16px; line-height:1.6;">We're thrilled to have you join us on this journey to make grocery shopping smarter, easier, and more affordable.</p>
            <p style="font-size:16px; line-height:1.6;">To get started, please confirm your email by clicking the button below:</p>
            <div style="text-align:left;">
              <a href="${confirmationUrl}" style="display:inline-block; margin-top:20px; padding:12px 24px; background-color:#60FF6F; color:#082517; text-decoration:none; font-weight:700; border-radius:8px;">Confirm Your Email</a>
            </div>
            <p style="font-size:14px; line-height:1.6; margin-top:24px; color:#cfe9d3;">This link stays valid for 24 hours and you can click it more than once if needed.</p>
            <h3 style="color:#60FF6F; font-size:22px; margin-top:40px; font-weight:500;">As a bonus for joining:</h3>
            <p style="font-size:16px; line-height:1.6;">🎉 You now have <strong>unlimited free access</strong> to our services while we're in beta! Reply to this email with your zip code and grocery list (a receipt, a screenshot, or a photo) and we'll get back to you with <strong>10%+ savings</strong> in under <strong>24 hours</strong>.</p>
            <div style="margin:40px -40px -40px -40px; background-color:#F0F0F0; padding:20px; border-radius:0 0 12px 12px; text-align:center; font-size:12px; color:#666666;">
              <p>© 2025 Prox, LLC</p>
              <p>2903 Lincoln Blvd, Santa Monica, CA 90405</p>
              <p><a href="${SITE_URL}/unsubscribe?email=${encodeURIComponent(email)}" style="color:#666666; text-decoration:underline;">Update your email preferences or unsubscribe here</a></p>
            </div>
          </div>
        </body>
        </html>
      `,
    });

    return new Response(JSON.stringify({ success: true }), {
      status: 200,
      headers: { "Content-Type": "application/json", ...corsHeaders },
    });
  } catch (error: any) {
    console.error("Error in send-signup-confirmation function:", error);
    return new Response(JSON.stringify({ error: error.message }), {
      status: 500,
      headers: { "Content-Type": "application/json", ...corsHeaders },
    });
  }
};

serve(handler);
