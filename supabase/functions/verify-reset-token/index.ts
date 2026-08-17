import { serve } from "https://deno.land/std@0.190.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.57.2";

const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
const supabaseServiceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type",
};

interface ResetPasswordRequest {
  token: string;
  newPassword?: string;
}

// Find a user by their custom reset_token, scanning ALL users (paginated).
// The previous version only checked the first 50 users, so resets failed for
// most of the user base once it grew past that.
async function findUserByResetToken(supabaseAdmin: any, token: string) {
  const perPage = 1000;
  let page = 1;
  while (page <= 50) {
    const { data, error } = await supabaseAdmin.auth.admin.listUsers({ page, perPage });
    if (error) throw error;
    const users = data?.users || [];
    if (users.length === 0) break;
    const match = users.find((u: any) => u.user_metadata?.reset_token === token);
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

  try {
    const { token, newPassword }: ResetPasswordRequest = await req.json();

    if (!token) {
      return new Response(
        JSON.stringify({ valid: false, error: "Reset token is required" }),
        { status: 400, headers: { "Content-Type": "application/json", ...corsHeaders } }
      );
    }

    const supabaseAdmin = createClient(supabaseUrl, supabaseServiceKey, {
      auth: { autoRefreshToken: false, persistSession: false },
    });

    const user = await findUserByResetToken(supabaseAdmin, token);

    // Token must exist and not be expired.
    const expiresAt = user?.user_metadata?.reset_token_expires;
    const isNotExpired = expiresAt ? new Date(expiresAt) > new Date() : false;
    const isValid = !!user && isNotExpired;

    if (!isValid) {
      return new Response(
        JSON.stringify({ valid: false, error: "This reset link is invalid or has expired." }),
        { status: 400, headers: { "Content-Type": "application/json", ...corsHeaders } }
      );
    }

    // Validate-only mode: the "Verifying your reset link..." screen can call this
    // with just the token to check the link before showing the new-password form.
    // The token stays usable so the user can submit their password next.
    if (!newPassword) {
      return new Response(
        JSON.stringify({ valid: true, message: "Reset link is valid" }),
        { status: 200, headers: { "Content-Type": "application/json", ...corsHeaders } }
      );
    }

    // Update the password. We intentionally do NOT clear the reset token here, so
    // the same link keeps working on repeated clicks until it naturally expires (24h).
    const { error: updateError } = await supabaseAdmin.auth.admin.updateUserById(
      user.id,
      { password: newPassword }
    );
    if (updateError) throw updateError;

    console.log("Password updated successfully for user:", user.email);

    // Migrate waitlist -> profiles if applicable (guarded so repeat calls are safe).
    try {
      const { data: waitlistUser, error: waitlistError } = await supabaseAdmin
        .from('waitlist')
        .select('*')
        .eq('email', user.email)
        .maybeSingle();

      if (!waitlistError && waitlistUser) {
        const nameParts = waitlistUser.name?.trim().split(' ') || [];
        const firstName = nameParts[0] || '';
        const lastName = nameParts.slice(1).join(' ') || '';

        const { data: existingProfile } = await supabaseAdmin
          .from('profiles')
          .select('id')
          .eq('user_id', user.id)
          .maybeSingle();

        if (!existingProfile) {
          const { error: profileError } = await supabaseAdmin
            .from('profiles')
            .insert({
              user_id: user.id,
              first_name: firstName,
              last_name: lastName,
              display_name: waitlistUser.name,
              zip_code: waitlistUser.zip_code,
              preferred_retailers: waitlistUser.preferred_retailers,
              app_preference: waitlistUser.device_preference || 'web',
            });
          if (profileError) {
            console.error("Error creating profile from waitlist:", profileError);
          } else {
            await supabaseAdmin
              .from('waitlist')
              .update({ user_id: user.id })
              .eq('email', user.email);
          }
        }
      }
    } catch (migrationError) {
      console.error("Error during waitlist migration:", migrationError);
    }

    return new Response(
      JSON.stringify({ success: true, valid: true, message: "Password updated successfully" }),
      { status: 200, headers: { "Content-Type": "application/json", ...corsHeaders } }
    );
  } catch (error: any) {
    console.error("Error in verify-reset-token function:", error);
    return new Response(
      JSON.stringify({ error: error.message, success: false }),
      { status: 500, headers: { "Content-Type": "application/json", ...corsHeaders } }
    );
  }
};

serve(handler);
