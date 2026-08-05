import { serve } from "https://deno.land/std@0.190.0/http/server.ts";
import { Resend } from "npm:resend@2.0.0";
import { checkRateLimit, createRateLimitResponse } from "../_shared/rate-limiter.ts";

const resend = new Resend(Deno.env.get("RESEND_API_KEY"));

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

// Rate limit: 5 emails per minute per IP (prevent spam)
const RATE_LIMIT_CONFIG = {
  maxRequests: 5,
  windowMs: 60 * 1000, // 1 minute
};

interface SignupEmailRequest {
  firstName: string;
  lastName: string;
  email: string;
}

const handler = async (req: Request): Promise<Response> => {
  // Handle CORS preflight requests
  if (req.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders });
  }

  // Check rate limit
  const rateLimitResult = checkRateLimit(req, RATE_LIMIT_CONFIG);
  if (!rateLimitResult.allowed) {
    console.warn("Rate limit exceeded for send-signup-email");
    return createRateLimitResponse(rateLimitResult, corsHeaders);
  }

  try {
    const { firstName, lastName, email }: SignupEmailRequest = await req.json();
    const name = `${firstName} ${lastName}`;

    console.log("Sending signup email to:", email);

    const emailResponse = await resend.emails.send({
      from: "Prox <alston@joinprox.com>",
      to: [email],
      subject: "Please confirm your email - Prox Waitlist",
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
              .header {
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                padding: 40px 20px;
                text-align: center;
                border-radius: 8px 8px 0 0;
              }
              .header h1 {
                color: white;
                margin: 0;
                font-size: 32px;
              }
              .content {
                background: white;
                padding: 40px 30px;
                border: 1px solid #e0e0e0;
                border-top: none;
                border-radius: 0 0 8px 8px;
              }
              .button {
                display: inline-block;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                padding: 14px 40px;
                text-decoration: none;
                border-radius: 6px;
                margin: 20px 0;
                font-weight: 600;
                font-size: 16px;
              }
              .button:hover {
                opacity: 0.9;
              }
              .info-box {
                background: #f8f9fa;
                border-left: 4px solid #667eea;
                padding: 15px 20px;
                margin: 20px 0;
                border-radius: 4px;
              }
              .footer {
                text-align: center;
                color: #666;
                font-size: 14px;
                margin-top: 30px;
                padding-top: 20px;
                border-top: 1px solid #e0e0e0;
              }
            </style>
          </head>
          <body>
            <div class="header">
              <h1>Confirm Your Email</h1>
            </div>
            <div class="content">
              <h2>Hi ${name}! 👋</h2>
              <p>Thank you for joining the Prox waitlist! To complete your signup, please confirm your email address by clicking the button below:</p>
              
              <div style="text-align: center; margin: 30px 0;">
                <a href="https://yhyaslxqzwqptknmybqa.supabase.co/auth/v1/verify?token_hash=PLACEHOLDER&type=signup&redirect_to=https://prox.lovable.app/confirm-email" class="button">Confirm Email Address</a>
              </div>
              
              <div class="info-box">
                <strong>What happens after confirmation?</strong>
                <ul style="margin: 10px 0; padding-left: 20px;">
                  <li>You'll be on the waitlist for exclusive early access</li>
                  <li>Get notified when we launch new features</li>
                  <li>Receive personalized grocery deals from your favorite retailers</li>
                  <li>Be among the first to try our platform</li>
                </ul>
              </div>
              
              <p style="color: #666; font-size: 14px; margin-top: 30px;">
                <strong>Note:</strong> If you didn't sign up for Prox, you can safely ignore this email.
              </p>
              
              <div class="footer">
                <p>Have questions? Just reply to this email - we'd love to hear from you!</p>
                <p style="margin-top: 15px;">Best regards,<br><strong>The Prox Team</strong></p>
                
                <div style="text-align: center; margin-top: 30px;">
                  <a href="https://prox.lovable.app/unsubscribe?email=${encodeURIComponent(email)}" style="display: inline-block; background: #dc3545; color: white; padding: 10px 30px; text-decoration: none; border-radius: 6px; font-weight: 600; font-size: 14px;">Unsubscribe</a>
                </div>
              </div>
            </div>
          </body>
        </html>
      `,
    });

    console.log("Email sent successfully:", emailResponse);

    return new Response(JSON.stringify(emailResponse), {
      status: 200,
      headers: {
        "Content-Type": "application/json",
        ...corsHeaders,
      },
    });
  } catch (error: any) {
    console.error("Error in send-signup-email function:", error);
    return new Response(JSON.stringify({ error: error.message }), {
      status: 500,
      headers: { "Content-Type": "application/json", ...corsHeaders },
    });
  }
};

serve(handler);
