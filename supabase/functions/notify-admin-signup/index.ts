import { serve } from "https://deno.land/std@0.190.0/http/server.ts";
import { Resend } from "npm:resend@2.0.0";
import { checkRateLimit, createRateLimitResponse } from "../_shared/rate-limiter.ts";

const resend = new Resend(Deno.env.get("RESEND_API_KEY"));

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type",
};

// Rate limit: 10 notifications per minute per IP
const RATE_LIMIT_CONFIG = {
  maxRequests: 10,
  windowMs: 60 * 1000,
};

interface AdminNotificationRequest {
  firstName: string;
  lastName: string;
  email: string;
  dateOfBirth?: string;
  genderIdentity?: string;
  zipCode?: string;
  preferredRetailers?: string[];
  appPreference?: string;
}

const handler = async (req: Request): Promise<Response> => {
  // Handle CORS preflight requests
  if (req.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders });
  }

  // Check rate limit
  const rateLimitResult = checkRateLimit(req, RATE_LIMIT_CONFIG);
  if (!rateLimitResult.allowed) {
    console.warn("Rate limit exceeded for notify-admin-signup");
    return createRateLimitResponse(rateLimitResult, corsHeaders);
  }

  try {
    const {
      firstName,
      lastName,
      email,
      dateOfBirth,
      genderIdentity,
      zipCode,
      preferredRetailers,
      appPreference,
    }: AdminNotificationRequest = await req.json();

    console.log("Sending admin notification for new signup:", email);

    const emailResponse = await resend.emails.send({
      from: "Prox Notifications <onboarding@resend.dev>",
      to: ["alston@joinprox.com"],
      subject: `New User Signup: ${firstName} ${lastName}`,
      html: `
        <!DOCTYPE html>
        <html>
          <head>
            <style>
              body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Roboto', 'Oxygen', 'Ubuntu', 'Cantarell', 'Fira Sans', 'Droid Sans', 'Helvetica Neue', sans-serif;
                line-height: 1.6;
                color: #333;
                max-width: 700px;
                margin: 0 auto;
                padding: 20px;
              }
              .header {
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                padding: 30px 20px;
                text-align: center;
                border-radius: 8px 8px 0 0;
              }
              .header h1 {
                color: white;
                margin: 0;
                font-size: 28px;
              }
              .content {
                background: white;
                padding: 30px;
                border: 1px solid #e0e0e0;
                border-top: none;
                border-radius: 0 0 8px 8px;
              }
              .info-section {
                background: #f8f9fa;
                padding: 20px;
                border-radius: 8px;
                margin: 20px 0;
              }
              .info-row {
                display: flex;
                padding: 8px 0;
                border-bottom: 1px solid #e0e0e0;
              }
              .info-row:last-child {
                border-bottom: none;
              }
              .info-label {
                font-weight: 600;
                width: 180px;
                color: #555;
              }
              .info-value {
                color: #333;
                flex: 1;
              }
              .retailers-list {
                display: flex;
                flex-wrap: wrap;
                gap: 8px;
                margin-top: 5px;
              }
              .retailer-badge {
                background: #667eea;
                color: white;
                padding: 4px 12px;
                border-radius: 16px;
                font-size: 13px;
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
              <h1>🎉 New User Signup</h1>
            </div>
            <div class="content">
              <h2>User Details</h2>
              
              <div class="info-section">
                <div class="info-row">
                  <div class="info-label">Name:</div>
                  <div class="info-value">${firstName} ${lastName}</div>
                </div>
                <div class="info-row">
                  <div class="info-label">Email:</div>
                  <div class="info-value"><strong>${email}</strong></div>
                </div>
                ${dateOfBirth ? `
                <div class="info-row">
                  <div class="info-label">Date of Birth:</div>
                  <div class="info-value">${dateOfBirth}</div>
                </div>
                ` : ''}
                ${genderIdentity ? `
                <div class="info-row">
                  <div class="info-label">Gender Identity:</div>
                  <div class="info-value">${genderIdentity}</div>
                </div>
                ` : ''}
                ${zipCode ? `
                <div class="info-row">
                  <div class="info-label">ZIP Code:</div>
                  <div class="info-value">${zipCode}</div>
                </div>
                ` : ''}
                ${appPreference ? `
                <div class="info-row">
                  <div class="info-label">App Preference:</div>
                  <div class="info-value">${appPreference}</div>
                </div>
                ` : ''}
                ${preferredRetailers && preferredRetailers.length > 0 ? `
                <div class="info-row">
                  <div class="info-label">Preferred Retailers:</div>
                  <div class="info-value">
                    <div class="retailers-list">
                      ${preferredRetailers.map(retailer => `<span class="retailer-badge">${retailer}</span>`).join('')}
                    </div>
                  </div>
                </div>
                ` : ''}
              </div>
              
              <p style="color: #666; font-size: 14px; margin-top: 20px;">
                This user just created an account on Prox. Timestamp: ${new Date().toLocaleString('en-US', { timeZone: 'America/New_York' })} EST
              </p>
              
              <div class="footer">
                <p>Automated notification from Prox user signup system</p>
              </div>
            </div>
          </body>
        </html>
      `,
    });

    console.log("Admin notification email sent successfully:", emailResponse);

    return new Response(JSON.stringify(emailResponse), {
      status: 200,
      headers: {
        "Content-Type": "application/json",
        ...corsHeaders,
      },
    });
  } catch (error: any) {
    console.error("Error in notify-admin-signup function:", error);
    return new Response(
      JSON.stringify({ error: error.message }),
      {
        status: 500,
        headers: { "Content-Type": "application/json", ...corsHeaders },
      }
    );
  }
};

serve(handler);
