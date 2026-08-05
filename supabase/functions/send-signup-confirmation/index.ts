import { serve } from "https://deno.land/std@0.190.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.57.2";
import { Resend } from "npm:resend@2.0.0";
import { checkRateLimit, createRateLimitResponse } from "../_shared/rate-limiter.ts";

const resend = new Resend(Deno.env.get("RESEND_API_KEY"));

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

// Rate limit: 5 confirmation emails per minute per IP
const RATE_LIMIT_CONFIG = {
  maxRequests: 5,
  windowMs: 60 * 1000,
};

interface SignupConfirmationRequest {
  email: string;
  userName?: string;
}

const handler = async (req: Request): Promise<Response> => {
  // Handle CORS preflight requests
  if (req.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders });
  }

  // Check rate limit
  const rateLimitResult = checkRateLimit(req, RATE_LIMIT_CONFIG);
  if (!rateLimitResult.allowed) {
    console.warn("Rate limit exceeded for send-signup-confirmation");
    return createRateLimitResponse(rateLimitResult, corsHeaders);
  }

  try {
    const { email, userName }: SignupConfirmationRequest = await req.json();
    
    console.log("📧 Sending signup confirmation email");
    console.log("User email:", email);

    const displayName = userName || email.split('@')[0];

    // Create Supabase admin client to generate confirmation link
    const supabase = createClient(
      Deno.env.get("SUPABASE_URL") ?? "",
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? ""
    );

    // Get the user by email
    const { data: { users }, error: listError } = await supabase.auth.admin.listUsers();
    if (listError) throw listError;

    const user = users?.find(u => u.email === email);
    if (!user) {
      throw new Error("User not found");
    }

    // Generate email verification link
    const siteUrl = Deno.env.get('SITE_URL') || 'https://joinprox.com';
    const { data: linkData, error: linkError } = await supabase.auth.admin.generateLink({
      type: 'signup',
      email: email,
      options: {
        redirectTo: `${siteUrl}/confirm-email`
      }
    });

    if (linkError) throw linkError;
    
    // Extract the token from the generated link and build our custom URL
    const generatedUrl = new URL(linkData.properties.action_link);
    const token = generatedUrl.searchParams.get('token');
    const confirmationUrl = `${siteUrl}/confirm-email?confirmation_token=${token}&type=signup`;

    console.log("Sending custom confirmation email with URL:", confirmationUrl);

    const emailResponse = await resend.emails.send({
      from: "Prox <alston@joinprox.com>",
      to: [email],
      subject: "Welcome to Prox - Confirm Your Email",
      html: `
        <!DOCTYPE html>
        <html>
        <head>
          <link href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700&display=swap" rel="stylesheet">
          <style>
            body {
              font-family: 'Roboto', Arial, sans-serif;
              background-color: #FFFFFF;
              margin: 0;
              padding: 0;
            }
            .container {
              max-width: 600px;
              margin: 30px auto;
              background-color: #082517;
              color: #FFFFFF;
              padding: 40px;
              border-radius: 12px;
            }
            h2, h3 {
              color: #60FF6F;
            }
            h2 {
              font-size: 28px;
              font-weight: 700;
            }
            h3 {
              font-size: 22px;
              margin-top: 40px;
              font-weight: 500;
            }
            p {
              font-size: 16px;
              line-height: 1.6;
              font-weight: 400;
            }
            ul {
              margin-top: 10px;
              margin-bottom: 20px;
            }
            li {
              margin-bottom: 8px;
            }
            a.button {
              display: inline-block;
              margin-top: 20px;
              padding: 12px 24px;
              background-color: #60FF6F;
              color: #082517;
              text-decoration: none;
              font-weight: 700;
              border-radius: 8px;
            }
            .footer-wrapper {
              margin: 40px -40px -40px -40px;
              background-color: #F0F0F0;
              padding: 20px;
              border-radius: 0 0 12px 12px;
            }
            .footer {
              text-align: center;
              font-size: 12px;
              color: #666666;
            }
            .footer a {
              color: #666666;
              text-decoration: underline;
            }
          </style>
        </head>
        <body>

          <div class="container">
            <h2>Welcome to Prox 👋</h2>
            <p>Hi ${displayName}!</p>
            <p>We're thrilled to have you join us on this journey to make grocery shopping smarter, easier, and more affordable.</p>
            
            <p>As part of the Prox community, you'll get:</p>
            <ul>
              <li>🔎 Real-time price comparisons across stores near you</li>
              <li>💡 Data-driven insights to help you save more</li>
              <li>📊 Early access to exclusive features and tools</li>
              <li>📚 Access to weekly blog posts on how to strategically find savings</li>
            </ul>

            <p>To get started, please confirm your email by clicking the button below:</p>

            <a href="${confirmationUrl}" class="button">Confirm Your Email</a>

            <h3>As an added bonus for joining the waitlist:</h3>
            <p>🎉 You now have <strong>unlimited free access</strong> to our services while we're in beta!</p>

            <p>We'll personally review your grocery list and get back to you with <strong>10%+ savings</strong> in less than <strong>24 hours</strong>. Just reply directly to this email with:</p>
            <ul>
              <li>Your zip code</li>
              <li>Your grocery list (in any format)
                <ul>
                  <li>A recent receipt</li>
                  <li>A screenshot of your online shopping cart</li>
                  <li>A hand-written grocery list</li>
                  <li>OR even an ancient scroll with hieroglyphics</li>
                </ul>
              </li>
            </ul>

            <p>Thanks again for joining Prox. We're so excited to have you with us!</p>

            <div class="footer-wrapper">
              <div class="footer">
                <p>© 2025 Prox, LLC</p>
                <p>2903 Lincoln Blvd, Santa Monica, CA 90405</p>
                <p><a href="https://joinprox.com/unsubscribe?email=${encodeURIComponent(email)}">Update your email preferences or unsubscribe here</a></p>
              </div>
            </div>

          </div>

        </body>
        </html>
      `,
    });

    console.log("✅ Custom confirmation email sent successfully:", emailResponse);

    return new Response(JSON.stringify(emailResponse), {
      status: 200,
      headers: {
        "Content-Type": "application/json",
        ...corsHeaders,
      },
    });
  } catch (error: any) {
    console.error("❌ Error in send-signup-confirmation function:", error);
    return new Response(JSON.stringify({ error: error.message }), {
      status: 500,
      headers: { "Content-Type": "application/json", ...corsHeaders },
    });
  }
};

serve(handler);
