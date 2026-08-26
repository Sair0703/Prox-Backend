import { serve } from "https://deno.land/std@0.190.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.57.2";
import { Resend } from "npm:resend@2.0.0";
import { checkRateLimit, createRateLimitResponse } from "../_shared/rate-limiter.ts";

const resend = new Resend(Deno.env.get("RESEND_API_KEY"));

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

// Rate limit: 5 unsubscribes per minute per IP
const RATE_LIMIT_CONFIG = {
  maxRequests: 5,
  windowMs: 60 * 1000,
};

const handler = async (req: Request): Promise<Response> => {
  // Handle CORS preflight requests
  if (req.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders });
  }

  // Check rate limit
  const rateLimitResult = checkRateLimit(req, RATE_LIMIT_CONFIG);
  if (!rateLimitResult.allowed) {
    console.warn("Rate limit exceeded for unsubscribe");
    return createRateLimitResponse(rateLimitResult, corsHeaders);
  }

  try {
    const { email } = await req.json();

    if (!email) {
      return new Response(
        JSON.stringify({ error: "Email is required" }),
        {
          status: 400,
          headers: { "Content-Type": "application/json", ...corsHeaders },
        }
      );
    }

    const supabase = createClient(
      Deno.env.get("SUPABASE_URL") ?? "",
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? ""
    );

    console.log("Unsubscribing and deleting account for:", email);

    // Find user by email
    const { data: { users }, error: listError } = await supabase.auth.admin.listUsers();
    
    if (listError) {
      console.error("Error listing users:", listError);
      throw listError;
    }

    const user = users?.find(u => u.email === email);

    if (user) {
      console.log("Found user account, deleting from all tables...");
      
      // Delete from profiles table
      const { error: profileError } = await supabase
        .from("profiles")
        .delete()
        .eq("user_id", user.id);
      
      if (profileError && profileError.code !== "PGRST116") {
        console.error("Error deleting profile:", profileError);
      } else {
        console.log("Deleted from profiles table");
      }
      
      // Delete from waitlist table
      const { error: waitlistError } = await supabase
        .from("waitlist")
        .delete()
        .eq("email", email);
      
      if (waitlistError && waitlistError.code !== "PGRST116") {
        console.error("Error deleting from waitlist:", waitlistError);
      } else {
        console.log("Deleted from waitlist table");
      }
      
      // Delete from email_preferences table if exists
      const { error: emailPrefError } = await supabase
        .from("email_preferences")
        .delete()
        .eq("user_id", user.id);
      
      if (emailPrefError && emailPrefError.code !== "PGRST116") {
        console.error("Error deleting email preferences:", emailPrefError);
      } else {
        console.log("Deleted from email_preferences table");
      }
      
      // Finally, delete user account from authentication
      const { error: deleteError } = await supabase.auth.admin.deleteUser(user.id);
      
      if (deleteError) {
        console.error("Error deleting user:", deleteError);
        throw deleteError;
      }
      
      console.log("Successfully deleted user account from all tables:", email);
    } else {
      // If no user account exists, just delete from waitlist
      const { error: waitlistError } = await supabase
        .from("waitlist")
        .delete()
        .eq("email", email);

      if (waitlistError && waitlistError.code !== "PGRST116") {
        console.error("Error deleting from waitlist:", waitlistError);
        throw waitlistError;
      }
      
      console.log("Successfully removed from waitlist:", email);
    }

    // Send confirmation email
    try {
      const emailResult = await resend.emails.send({
        from: "Prox <noreply@joinprox.com>",
        to: [email],
        subject: "You've been unsubscribed from Prox",
        html: `
          <!DOCTYPE html>
          <html>
            <head>
              <meta charset="utf-8">
              <meta name="viewport" content="width=device-width, initial-scale=1.0">
            </head>
            <body style="margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; background-color: #0F1419;">
              <div style="max-width: 600px; margin: 0 auto; padding: 40px 20px;">
                <div style="background: linear-gradient(135deg, #1a1f2e 0%, #2d1b3d 100%); border-radius: 16px; padding: 40px; box-shadow: 0 4px 6px rgba(0, 0, 0, 0.3);">
                  <h2 style="color: #FFFFFF; font-size: 24px; margin: 0 0 20px 0; font-weight: 600;">You've been unsubscribed</h2>
                  
                  <p style="color: #FFFFFF; font-size: 16px; line-height: 1.6; margin: 0 0 20px 0;">
                    Your email address has been successfully removed from our system. You will no longer receive any emails from Prox.
                  </p>
                  
                  <p style="color: #FFFFFF; font-size: 16px; line-height: 1.6; margin: 0 0 20px 0;">
                    ${user ? 'Your account has been completely deleted.' : 'You have been removed from our waitlist.'}
                  </p>
                  
                  <p style="color: #FFFFFF; font-size: 16px; line-height: 1.6; margin: 0 0 30px 0;">
                    If this was a mistake, you can always sign up again at <a href="https://joinprox.com/auth" style="color: #9b87f5; text-decoration: none;">joinprox.com</a>.
                  </p>
                  
                  <div style="border-top: 1px solid rgba(255, 255, 255, 0.1); padding-top: 20px; margin-top: 30px;">
                    <p style="color: rgba(255, 255, 255, 0.6); font-size: 14px; margin: 0; text-align: center;">
                      Prox - Smart Grocery Shopping
                    </p>
                  </div>
                </div>
              </div>
            </body>
          </html>
        `,
      });
      
      if (emailResult.error) {
        console.error("Resend API error:", JSON.stringify(emailResult.error));
      } else {
        console.log("Unsubscribe confirmation email sent successfully:", JSON.stringify(emailResult.data));
      }
    } catch (emailError) {
      console.error("Failed to send confirmation email:", emailError);
      // Don't fail the request if email fails - user is already unsubscribed
    }

    return new Response(
      JSON.stringify({ success: true, message: "Successfully unsubscribed" }),
      {
        status: 200,
        headers: { "Content-Type": "application/json", ...corsHeaders },
      }
    );
  } catch (error: any) {
    console.error("Error in unsubscribe function:", error);
    return new Response(
      JSON.stringify({ error: error.message || "Failed to unsubscribe" }),
      {
        status: 500,
        headers: { "Content-Type": "application/json", ...corsHeaders },
      }
    );
  }
};

serve(handler);
