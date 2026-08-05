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
  newPassword: string;
}

const handler = async (req: Request): Promise<Response> => {
  if (req.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders });
  }

  try {
    const { token, newPassword }: ResetPasswordRequest = await req.json();

    if (!token || !newPassword) {
      return new Response(
        JSON.stringify({ error: "Token and new password are required" }),
        {
          status: 400,
          headers: { "Content-Type": "application/json", ...corsHeaders },
        }
      );
    }

    console.log("Password reset verification requested");

    const supabaseAdmin = createClient(supabaseUrl, supabaseServiceKey, {
      auth: {
        autoRefreshToken: false,
        persistSession: false,
      },
    });

    // Find user with matching reset token
    const { data: { users }, error: listError } = await supabaseAdmin.auth.admin.listUsers();
    
    if (listError) {
      console.error("Error listing users:", listError);
      throw listError;
    }

    const user = users?.find(u => {
      const resetToken = u.user_metadata?.reset_token;
      const expiresAt = u.user_metadata?.reset_token_expires;
      
      if (!resetToken || !expiresAt) return false;
      
      const isTokenMatch = resetToken === token;
      const isNotExpired = new Date(expiresAt) > new Date();
      
      return isTokenMatch && isNotExpired;
    });

    if (!user) {
      console.log("Invalid or expired token");
      return new Response(
        JSON.stringify({ error: "Invalid or expired reset token" }),
        {
          status: 400,
          headers: { "Content-Type": "application/json", ...corsHeaders },
        }
      );
    }

    // Update the password
    const { error: updateError } = await supabaseAdmin.auth.admin.updateUserById(
      user.id,
      {
        password: newPassword,
        user_metadata: {
          reset_token: null,
          reset_token_expires: null,
        }
      }
    );

    if (updateError) {
      console.error("Error updating password:", updateError);
      throw updateError;
    }

    console.log("Password updated successfully for user:", user.email);

    // Check if user is in waitlist and migrate to profiles
    try {
      const { data: waitlistUser, error: waitlistError } = await supabaseAdmin
        .from('waitlist')
        .select('*')
        .eq('email', user.email)
        .maybeSingle();

      if (!waitlistError && waitlistUser) {
        console.log("Found waitlist user, migrating to profiles");

        // Split name into first and last name (if possible)
        const nameParts = waitlistUser.name?.trim().split(' ') || [];
        const firstName = nameParts[0] || '';
        const lastName = nameParts.slice(1).join(' ') || '';

        // Check if profile already exists
        const { data: existingProfile } = await supabaseAdmin
          .from('profiles')
          .select('id')
          .eq('user_id', user.id)
          .maybeSingle();

        if (!existingProfile) {
          // Create profile with waitlist data
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
            console.log("Successfully migrated waitlist user to profiles");

            // Mark waitlist entry as converted by adding user_id
            await supabaseAdmin
              .from('waitlist')
              .update({ user_id: user.id })
              .eq('email', user.email);
          }
        } else {
          console.log("Profile already exists, skipping migration");
        }
      }
    } catch (migrationError) {
      console.error("Error during waitlist migration:", migrationError);
      // Don't fail the password reset if migration fails
    }

    return new Response(
      JSON.stringify({ 
        success: true,
        message: "Password updated successfully" 
      }),
      {
        status: 200,
        headers: {
          "Content-Type": "application/json",
          ...corsHeaders,
        },
      }
    );
  } catch (error: any) {
    console.error("Error in verify-reset-token function:", error);
    return new Response(
      JSON.stringify({ 
        error: error.message,
        success: false 
      }),
      {
        status: 500,
        headers: { "Content-Type": "application/json", ...corsHeaders },
      }
    );
  }
};

serve(handler);
