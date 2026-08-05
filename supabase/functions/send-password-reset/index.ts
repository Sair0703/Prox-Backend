import { serve } from "https://deno.land/std@0.190.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.57.2";
import { Resend } from "npm:resend@2.0.0";
import { checkRateLimit, createRateLimitResponse } from "../_shared/rate-limiter.ts";

const resend = new Resend(Deno.env.get("RESEND_API_KEY"));
const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
const supabaseServiceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

// Rate limit: 3 password resets per minute per IP (security sensitive)
const RATE_LIMIT_CONFIG = {
  maxRequests: 3,
  windowMs: 60 * 1000,
};

interface PasswordResetRequest {
  email: string;
  redirectTo: string;
}

// Generate a random token
function generateResetToken(): string {
  const array = new Uint8Array(32);
  crypto.getRandomValues(array);
  return Array.from(array, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

const handler = async (req: Request): Promise<Response> => {
  // Handle CORS preflight requests
  if (req.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders });
  }

  // Check rate limit
  const rateLimitResult = checkRateLimit(req, RATE_LIMIT_CONFIG);
  if (!rateLimitResult.allowed) {
    console.warn("Rate limit exceeded for send-password-reset");
    return createRateLimitResponse(rateLimitResult, corsHeaders);
  }

  try {
    const { email, redirectTo }: PasswordResetRequest = await req.json();

    console.log("Password reset requested for:", email);

    // Create Supabase admin client
    const supabaseAdmin = createClient(supabaseUrl, supabaseServiceKey, {
      auth: {
        autoRefreshToken: false,
        persistSession: false,
      },
    });

    // Find user by email with pagination
    console.log("Looking up user by email:", email);

    let user = null;
    let page = 1;
    const perPage = 1000;

    while (!user && page <= 10) {
      const { data, error: listError } = await supabaseAdmin.auth.admin.listUsers({
        page,
        perPage,
      });

      if (listError) {
        console.error("Error listing users:", listError);
        // Return success for security
        return new Response(
          JSON.stringify({
            success: true,
            message: "If an account exists, a reset email will be sent",
          }),
          {
            status: 200,
            headers: {
              "Content-Type": "application/json",
              ...corsHeaders,
            },
          },
        );
      }

      const users = data?.users || [];
      console.log(`Checking page ${page}, found ${users.length} users`);

      if (users.length === 0) break;

      user = users.find((u) => u.email?.toLowerCase() === email.toLowerCase());

      if (user) {
        console.log("✓ Found user:", user.id, user.email);
        break;
      }

      if (users.length < perPage) break;
      page++;
    }

    if (!user) {
      console.log("✗ User not found in auth.users, checking waitlist...");

      // Check if email exists in waitlist
      const { data: waitlistData, error: waitlistError } = await supabaseAdmin
        .from("waitlist")
        .select("email, user_id")
        .eq("email", email.toLowerCase())
        .maybeSingle();

      if (waitlistError) {
        console.error("Error checking waitlist:", waitlistError);
      }

      if (waitlistData && !waitlistData.user_id) {
        console.log("✓ Found in waitlist but no account yet");
        // User is in waitlist but hasn't completed signup
        const emailResponse = await resend.emails.send({
          from: "Join Prox <noreply@joinprox.com>",
          to: [email],
          subject: "Complete Your Prox Account Setup",
          html: `
            <!DOCTYPE html>
            <html>
              <head>
                <style>
                  body {
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Roboto', 'Oxygen', 'Ubuntu', 'Cantarell', 'Fira Sans', 'Droid Sans', 'Helvetica Neue', sans-serif;
                    line-height: 1.6;
                    color: #333;
                    max-width: 600px;
                    margin: 0 auto;
                    padding: 20px;
                  }
                  .container {
                    background: #ffffff;
                    border-radius: 8px;
                    padding: 32px;
                    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                  }
                  .header {
                    text-align: center;
                    margin-bottom: 32px;
                  }
                  h1 {
                    color: #1a1a1a;
                    font-size: 24px;
                    margin: 0 0 16px 0;
                  }
                  .button {
                    display: inline-block;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    color: white;
                    padding: 14px 32px;
                    text-decoration: none;
                    border-radius: 6px;
                    font-weight: 600;
                    margin: 24px 0;
                  }
                  .footer {
                    margin-top: 32px;
                    padding-top: 24px;
                    border-top: 1px solid #eee;
                    font-size: 14px;
                    color: #666;
                  }
                </style>
              </head>
              <body>
                <div class="container">
                  <div class="header">
                    <h1>📝 Complete Your Account Setup</h1>
                  </div>
                  <p>Hello,</p>
                  <p>You're on our waitlist, but you haven't completed your account setup yet!</p>
                  <p>To reset your password, you first need to complete your signup with your full information.</p>
                  <div style="text-align: center;">
                    <a href="${redirectTo.replace("/reset-password", "/auth")}" class="button">Complete Signup</a>
                  </div>
                  <p>Once you've completed signup, you'll be able to sign in and use all features.</p>
                  <div class="footer">
                    <p>Best regards,<br>The Prox Team</p>
                  </div>
                </div>
              </body>
            </html>
          `,
        });

        console.log("Waitlist reminder email sent:", emailResponse);

        return new Response(
          JSON.stringify({
            success: true,
            message: "If an account exists, a reset email will be sent",
          }),
          {
            status: 200,
            headers: {
              "Content-Type": "application/json",
              ...corsHeaders,
            },
          },
        );
      }

      console.log("✗ Email not found in auth or waitlist, sending generic response");
      return new Response(
        JSON.stringify({
          success: true,
          message: "If an account exists, a reset email will be sent",
        }),
        {
          status: 200,
          headers: {
            "Content-Type": "application/json",
            ...corsHeaders,
          },
        },
      );
    }

    console.log("User found:", user.id);

    // Generate custom reset token
    const resetToken = generateResetToken();
    const expiresAt = new Date(Date.now() + 3600000); // 1 hour from now

    // Store the reset token in user metadata
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

    console.log("Reset token generated and stored successfully");

    // Create reset link with custom token
    const resetLink = `${redirectTo}?token=${resetToken}`;

    // Send email via Resend
    const emailResponse = await resend.emails.send({
      from: "Prox <noreply@joinprox.com>",
      to: [email],
      subject: "Reset Your Password",
      html: `
        <!DOCTYPE html>
        <html>
          <head>
            <style>
              body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Roboto', 'Oxygen', 'Ubuntu', 'Cantarell', 'Fira Sans', 'Droid Sans', 'Helvetica Neue', sans-serif;
                line-height: 1.6;
                color: #333;
                max-width: 600px;
                margin: 0 auto;
                padding: 20px;
              }
              .container {
                background: #ffffff;
                border-radius: 8px;
                padding: 32px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
              }
              .header {
                text-align: center;
                margin-bottom: 32px;
              }
              h1 {
                color: #1a1a1a;
                font-size: 24px;
                margin: 0 0 16px 0;
              }
              .button {
                display: inline-block;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                padding: 14px 32px;
                text-decoration: none;
                border-radius: 6px;
                font-weight: 600;
                margin: 24px 0;
              }
              .footer {
                margin-top: 32px;
                padding-top: 24px;
                border-top: 1px solid #eee;
                font-size: 14px;
                color: #666;
              }
            </style>
          </head>
          <body>
            <div class="container">
              <div class="header">
                <h1>🔐 Reset Your Password</h1>
              </div>
              <p>Hello,</p>
              <p>We received a request to reset your password. Click the button below to set a new password:</p>
              <div style="text-align: center;">
                <a href="${resetLink}" class="button">Reset Password</a>
              </div>
              <p>This link will expire in 1 hour for security reasons.</p>
              <p>If you didn't request this password reset, you can safely ignore this email. Your password will remain unchanged.</p>
              <div class="footer">
                <p>Best regards,<br>The InnerDeals Team</p>
              </div>
            </div>
          </body>
        </html>
      `,
    });

    console.log("Email sent successfully:", emailResponse);

    return new Response(
      JSON.stringify({
        success: true,
        message: "Password reset email sent successfully",
      }),
      {
        status: 200,
        headers: {
          "Content-Type": "application/json",
          ...corsHeaders,
        },
      },
    );
  } catch (error: any) {
    console.error("Error in send-password-reset function:", error);
    return new Response(
      JSON.stringify({
        error: error.message,
        success: false,
      }),
      {
        status: 500,
        headers: { "Content-Type": "application/json", ...corsHeaders },
      },
    );
  }
};

serve(handler);
