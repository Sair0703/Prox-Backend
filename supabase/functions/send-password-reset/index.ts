import { serve } from "https://deno.land/std@0.190.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.57.2";
import { Resend } from "npm:resend@2.0.0";
import { checkRateLimit, createRateLimitResponse } from "./rate-limiter.ts";

const resend = new Resend(Deno.env.get("RESEND_API_KEY"));
const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
const supabaseServiceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

// Rate limit: 3 password resets per minute per IP (security sensitive)
const RATE_LIMIT_CONFIG = { maxRequests: 3, windowMs: 60 * 1000 };

interface PasswordResetRequest {
  email: string;
  redirectTo: string;
}

function generateResetToken(): string {
  const array = new Uint8Array(32);
  crypto.getRandomValues(array);
  return Array.from(array, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

const handler = async (req: Request): Promise<Response> => {
  if (req.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders });
  }

  const rateLimitResult = checkRateLimit(req, RATE_LIMIT_CONFIG);
  if (!rateLimitResult.allowed) {
    console.warn("Rate limit exceeded for send-password-reset");
    return createRateLimitResponse(rateLimitResult, corsHeaders);
  }

  try {
    const { email, redirectTo }: PasswordResetRequest = await req.json();
    console.log("Password reset requested for:", email);

    const supabaseAdmin = createClient(supabaseUrl, supabaseServiceKey, {
      auth: { autoRefreshToken: false, persistSession: false },
    });

    let user = null;
    let page = 1;
    const perPage = 1000;
    while (!user && page <= 10) {
      const { data, error: listError } = await supabaseAdmin.auth.admin.listUsers({ page, perPage });
      if (listError) {
        console.error("Error listing users:", listError);
        return new Response(
          JSON.stringify({ success: true, message: "If an account exists, a reset email will be sent" }),
          { status: 200, headers: { "Content-Type": "application/json", ...corsHeaders } },
        );
      }
      const users = data?.users || [];
      if (users.length === 0) break;
      user = users.find((u) => u.email?.toLowerCase() === email.toLowerCase());
      if (user) break;
      if (users.length < perPage) break;
      page++;
    }

    if (!user) {
      const { data: waitlistData } = await supabaseAdmin
        .from("waitlist").select("email, user_id").eq("email", email.toLowerCase()).maybeSingle();
      if (waitlistData && !waitlistData.user_id) {
        await resend.emails.send({
          from: "Join Prox <noreply@joinprox.com>",
          to: [email],
          subject: "Complete Your Prox Account Setup",
          html: `<p>You're on our waitlist, but you haven't completed your account setup yet. <a href="${redirectTo.replace("/reset-password", "/auth")}">Complete Signup</a> to continue.</p>`,
        });
      }
      return new Response(
        JSON.stringify({ success: true, message: "If an account exists, a reset email will be sent" }),
        { status: 200, headers: { "Content-Type": "application/json", ...corsHeaders } },
      );
    }

    // Generate custom reset token, valid for 24 hours (was 1 hour).
    const resetToken = generateResetToken();
    const expiresAt = new Date(Date.now() + 86400000); // 24 hours from now

    const { error: updateError } = await supabaseAdmin.auth.admin.updateUserById(user.id, {
      user_metadata: {
        reset_token: resetToken,
        reset_token_expires: expiresAt.toISOString(),
      },
    });
    if (updateError) {
      console.error("Error storing reset token:", updateError);
      throw updateError;
    }

    const resetLink = `${redirectTo}?token=${resetToken}`;

    await resend.emails.send({
      from: "Prox <noreply@joinprox.com>",
      to: [email],
      subject: "Reset Your Password",
      html: `
        <!DOCTYPE html>
        <html>
          <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px;">
            <div style="background: #ffffff; border-radius: 8px; padding: 32px; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
              <h1 style="color: #1a1a1a; font-size: 24px;">🔐 Reset Your Password</h1>
              <p>Hello,</p>
              <p>We received a request to reset your password. Click the button below to set a new password:</p>
              <div style="text-align: center;">
                <a href="${resetLink}" style="display: inline-block; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 14px 32px; text-decoration: none; border-radius: 6px; font-weight: 600; margin: 24px 0;">Reset Password</a>
              </div>
              <p>This link will expire in 24 hours for security reasons. You can click it more than once during that time if needed.</p>
              <p>If you didn't request this password reset, you can safely ignore this email. Your password will remain unchanged.</p>
              <p style="margin-top: 32px; padding-top: 24px; border-top: 1px solid #eee; font-size: 14px; color: #666;">Best regards,<br>The Prox Team</p>
            </div>
          </body>
        </html>
      `,
    });

    return new Response(
      JSON.stringify({ success: true, message: "Password reset email sent successfully" }),
      { status: 200, headers: { "Content-Type": "application/json", ...corsHeaders } },
    );
  } catch (error: any) {
    console.error("Error in send-password-reset function:", error);
    return new Response(
      JSON.stringify({ error: error.message, success: false }),
      { status: 500, headers: { "Content-Type": "application/json", ...corsHeaders } },
    );
  }
};

serve(handler);
