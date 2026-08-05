import { serve } from "https://deno.land/std@0.190.0/http/server.ts";
import { Resend } from "npm:resend@2.0.0";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.57.2";
import { checkRateLimit, createRateLimitResponse } from "../_shared/rate-limiter.ts";

const resend = new Resend(Deno.env.get("RESEND_API_KEY"));

const supabase = createClient(Deno.env.get("SUPABASE_URL") ?? "", Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "");

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type, x-supabase-client-platform, x-supabase-client-platform-version, x-supabase-client-runtime, x-supabase-client-runtime-version",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

// Rate limit: 5 emails per minute per IP
const RATE_LIMIT_CONFIG = {
  maxRequests: 5,
  windowMs: 60 * 1000,
};

// Native deep-link scheme used by the mobile app. Must match:
// - ios/App/App/Info.plist → CFBundleURLSchemes
// - android/app/src/main/AndroidManifest.xml → intent-filter (TODO: not yet added)
// - Supabase Dashboard → Authentication → URL Configuration → Redirect URLs
const MOBILE_DEEP_LINK_SCHEME = "com.proxshopping.mobile";

interface WelcomeEmailRequest {
  name: string;
  email: string;
  platform?: "mobile" | "web"; // optional so old callers keep working; defaults to "web"
}

/**
 * Build the redirect URL that the "Confirm Your Email" CTA should point at.
 * Mobile signups get the custom-scheme deep link so they bounce back into the
 * app after confirmation. Web signups (and anything unspecified) get the site
 * URL as the safe default.
 */
function getPrimaryRedirect(platform: "mobile" | "web", siteUrl: string): string {
  if (platform === "mobile") {
    return `${MOBILE_DEEP_LINK_SCHEME}://auth?mode=signin`;
  }
  return `${siteUrl}/`;
}

const handler = async (req: Request): Promise<Response> => {
  console.log("send-welcome-email invoked", { method: req.method });

  // Defensive environment check
  console.log("Environment check:", {
    hasResendKey: !!Deno.env.get("RESEND_API_KEY"),
    hasServiceRole: !!Deno.env.get("SUPABASE_SERVICE_ROLE_KEY"),
    hasSiteUrl: !!Deno.env.get("SITE_URL"),
  });

  // Handle CORS preflight requests
  if (req.method === "OPTIONS") {
    return new Response("ok", { status: 200, headers: corsHeaders });
  }

  // Enforce POST only
  if (req.method !== "POST") {
    console.warn("Invalid method:", req.method);
    return new Response("Method not allowed", { status: 405, headers: corsHeaders });
  }

  // Check rate limit
  const rateLimitResult = checkRateLimit(req, RATE_LIMIT_CONFIG);
  if (!rateLimitResult.allowed) {
    console.warn("Rate limit exceeded for send-welcome-email");
    return createRateLimitResponse(rateLimitResult, corsHeaders);
  }

  try {
    const body = await req.json();
    const { name, email, platform } = body as WelcomeEmailRequest;

    // Resolve platform with a safe default so any caller that doesn't pass
    // `platform` (e.g. older client builds) still gets a working email.
    const resolvedPlatform: "mobile" | "web" =
      platform === "mobile" || platform === "web" ? platform : "web";

    console.log("Payload received:", { name, email, platform: resolvedPlatform });

    // Validate email
    if (!email || typeof email !== "string") {
      console.error("Missing or invalid email in request body:", body);
      return new Response(
        JSON.stringify({ error: "Missing or invalid email" }),
        { status: 400, headers: { "Content-Type": "application/json", ...corsHeaders } }
      );
    }

    // Resolve the redirect target based on platform. Mobile signups get the
    // deep-link scheme so confirmation bounces back into the app; everyone
    // else gets the web URL.
    const siteUrl = Deno.env.get("SITE_URL") || "https://www.joinprox.com";
    const encodedEmail = encodeURIComponent(email);
    const primaryRedirect = getPrimaryRedirect(resolvedPlatform, siteUrl);
    const webFallbackRedirect = `${siteUrl}/`;

    console.log("Redirect strategy resolved:", {
      platform: resolvedPlatform,
      primaryRedirect,
      webFallbackRedirect,
    });

    // Generate the confirmation link with magiclink-first strategy
    let linkData: any = null;
    let usedType = "";
    const attemptedTypes: string[] = [];

    // Attempt 1: magiclink (works for existing users)
    console.log("generateLink attempt #1 type=magiclink");
    attemptedTypes.push("magiclink");
    const { data: magicData, error: magicError } = await supabase.auth.admin.generateLink({
      type: "magiclink",
      email: email,
      options: {
        redirectTo: primaryRedirect,
      },
    });

    if (!magicError && magicData) {
      linkData = magicData;
      usedType = "magiclink";
      console.log("Link generated successfully; using type=magiclink");
    } else {
      console.warn("generateLink magiclink error:", JSON.stringify(magicError));

      // Attempt 2: signup fallback (for edge cases where user doesn't exist yet)
      console.log("generateLink fallback attempt #2 type=signup");
      attemptedTypes.push("signup");
      const { data: signupData, error: signupError } = await supabase.auth.admin.generateLink({
        type: "signup",
        email: email,
        options: {
          redirectTo: primaryRedirect,
        },
      });

      if (!signupError && signupData) {
        linkData = signupData;
        usedType = "signup";
        console.log("Link generated successfully; using type=signup");
      } else {
        console.error("generateLink signup fallback error:", JSON.stringify(signupError));
        return new Response(
          JSON.stringify({
            stage: "generateLink",
            attempted_types: attemptedTypes,
            error: signupError || magicError,
          }),
          { status: 500, headers: { "Content-Type": "application/json", ...corsHeaders } }
        );
      }
    }

    // Primary confirmation link (may be mobile deep link or web, based on platform)
    const confirmationUrl = linkData.properties.action_link;

    // Web fallback confirmation link — only used in the secondary "opening on a
    // computer?" CTA for mobile signups. We generate a second link pointing at
    // the web URL so desktop email readers have a working path.
    let webFallbackUrl: string | null = null;
    if (resolvedPlatform === "mobile") {
      console.log("Generating web fallback link for mobile signup");
      const { data: fallbackData, error: fallbackError } = await supabase.auth.admin.generateLink({
        type: usedType === "signup" ? "signup" : "magiclink",
        email: email,
        options: {
          redirectTo: webFallbackRedirect,
        },
      });

      if (!fallbackError && fallbackData) {
        webFallbackUrl = fallbackData.properties.action_link;
        console.log("Web fallback link generated successfully");
      } else {
        // Non-fatal: if we can't generate a fallback, we just omit the secondary
        // CTA from the email. The primary mobile deep link still works.
        console.warn("Web fallback link generation failed (non-fatal):", JSON.stringify(fallbackError));
      }
    }

    // Log confirmation URL hostname only (not the full token)
    try {
      const urlHost = new URL(confirmationUrl).hostname;
      console.log(`Confirmation link hostname: ${urlHost}, type used: ${usedType}`);
    } catch {
      console.log(`Confirmation link generated, type used: ${usedType}`);
    }

    // Build the optional secondary CTA for mobile signups. Styled to match the
    // editorial email body (cream paper, dark green text, monospace label).
    const secondaryCtaHtml =
      resolvedPlatform === "mobile" && webFallbackUrl
        ? `
            <p style="margin:18px 0 0 0;font-family:'Geist',Helvetica,Arial,sans-serif;font-size:13px;color:#3B4A40;line-height:1.55;">
              Opening this on a computer?
              <a href="${webFallbackUrl}" style="color:#0E3A22;text-decoration:underline;font-weight:600;">Confirm on the web instead</a>.
            </p>
          `
        : "";

    // Send email via Resend
    console.log("Sending email via Resend...");
    const emailResponse = await resend.emails.send({
      from: "Prox <alston@joinprox.com>",
      to: [email],
      subject: "Welcome to Prox Community",
      html: `<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta http-equiv="X-UA-Compatible" content="IE=edge" />
  <meta name="x-apple-disable-message-reformatting" />
  <meta name="color-scheme" content="light only" />
  <meta name="supported-color-schemes" content="light only" />
  <title>Welcome to Prox</title>
  <!--[if mso]>
  <noscript><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml></noscript>
  <![endif]-->
  <style>
    body, table, td, a { -webkit-text-size-adjust: 100%; -ms-text-size-adjust: 100%; }
    table, td { mso-table-lspace: 0pt; mso-table-rspace: 0pt; border-collapse: collapse; }
    img { -ms-interpolation-mode: bicubic; border: 0; line-height: 100%; outline: none; text-decoration: none; }
    body { margin: 0 !important; padding: 0 !important; width: 100% !important; background: #FAF5E6; }
    a { color: #0E3A22; }
    @media (max-width: 620px) {
      .container { width: 100% !important; }
      .px-40 { padding-left: 22px !important; padding-right: 22px !important; }
      .px-28 { padding-left: 18px !important; padding-right: 18px !important; }
      .hero  { font-size: 44px !important; line-height: 1.02 !important; }
      .story-h { font-size: 28px !important; }
    }
  </style>
  <!--[if !mso]><!-->
  <link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=Instrument+Serif:ital@0;1&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet" />
  <!--<![endif]-->
</head>
<body style="margin:0;padding:0;background:#FAF5E6;">
  <div style="display:none;max-height:0;overflow:hidden;mso-hide:all;font-size:1px;line-height:1px;color:#FAF5E6;opacity:0;">
    Groceries got quietly expensive. We're fixing that — confirm to start saving.
  </div>

  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#FAF5E6" style="background:#FAF5E6;">
    <tr>
      <td align="center" style="padding:0;">

        <table role="presentation" class="container" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:600px;background:#FAF5E6;">

          <!-- Top bar -->
          <tr>
            <td class="px-40" style="padding:20px 40px;border-bottom:1px solid #E5DCC2;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td align="left">
                    <span style="display:inline-block;background:#082517;color:#5CE26A;width:32px;height:32px;border-radius:16px;text-align:center;font-family:'Geist',Helvetica,Arial,sans-serif;font-size:11px;font-weight:800;letter-spacing:-0.02em;line-height:32px;">prox</span>
                  </td>
                  <td align="right" style="font-family:'JetBrains Mono',Menlo,Consolas,monospace;font-size:10px;color:#7A847C;letter-spacing:0.1em;text-transform:uppercase;">
                    Vol. 001 · The Welcome Issue
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Editorial hero -->
          <tr>
            <td class="px-40" style="padding:56px 40px 40px;">
              <p style="margin:0 0 18px 0;font-family:'JetBrains Mono',Menlo,Consolas,monospace;font-size:11px;color:#0E3A22;letter-spacing:0.16em;text-transform:uppercase;">
                Welcome to the Prox Community!
              </p>
              <h1 class="hero" style="margin:0;font-family:'Instrument Serif',Georgia,serif;font-size:62px;line-height:0.98;font-weight:400;letter-spacing:-0.02em;color:#082517;">
                Groceries got<br/>
                <span style="font-style:italic;">quietly</span> expensive.<br/>
                We're fixing that.
              </h1>
              <p style="margin:28px 0 0 0;font-family:'Geist',Helvetica,Arial,sans-serif;font-size:16px;line-height:1.6;color:#3B4A40;max-width:460px;">
                The same cart can cost wildly different amounts depending on which grocer you shop. Prox watches every store near you, every week, and tells you where to go.
              </p>

              <!-- Primary CTA -->
              <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin-top:32px;">
                <tr>
                  <td bgcolor="#082517" style="border-radius:999px;">
                    <!--[if mso]>
                    <v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" xmlns:w="urn:schemas-microsoft-com:office:word" href="${confirmationUrl}" style="height:50px;v-text-anchor:middle;width:200px;" arcsize="50%" stroke="f" fillcolor="#082517">
                      <w:anchorlock/>
                      <center style="color:#5CE26A;font-family:Helvetica,Arial,sans-serif;font-size:15px;font-weight:600;">Confirm email →</center>
                    </v:roundrect>
                    <![endif]-->
                    <!--[if !mso]><!-->
                    <a href="${confirmationUrl}" style="display:inline-block;background:#082517;color:#5CE26A;text-decoration:none;font-family:'Geist',Helvetica,Arial,sans-serif;font-weight:600;font-size:15px;padding:16px 26px;border-radius:999px;letter-spacing:-0.01em;">
                      Confirm email&nbsp;&nbsp;→
                    </a>
                    <!--<![endif]-->
                  </td>
                </tr>
              </table>
              ${secondaryCtaHtml}
            </td>
          </tr>

          <!-- Story card — sample cart -->
          <tr>
            <td style="padding:0 40px 8px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#082517" style="background:#082517;border-radius:22px;">
                <tr>
                  <td class="px-28" style="padding:28px 28px 24px;color:#ffffff;">
                    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                      <tr>
                        <td valign="top">
                          <p style="margin:0;font-family:'JetBrains Mono',Menlo,Consolas,monospace;font-size:10px;color:#5CE26A;letter-spacing:0.14em;text-transform:uppercase;">Last week, near you</p>
                          <p class="story-h" style="margin:8px 0 0 0;font-family:'Instrument Serif',Georgia,serif;font-size:36px;line-height:1;letter-spacing:-0.01em;color:#ffffff;">
                            The same cart,<br/>
                            <span style="font-style:italic;">three prices.</span>
                          </p>
                        </td>
                        <td valign="top" align="right">
                          <span style="display:inline-block;background:#F4C13E;color:#082517;font-family:'Geist',Helvetica,Arial,sans-serif;font-size:9px;font-weight:800;padding:5px 10px;border-radius:8px;letter-spacing:0.06em;text-transform:uppercase;">Saved&nbsp;$9.29</span>
                        </td>
                      </tr>
                    </table>

                    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:24px;border-top:1px solid #1B4A2F;">
                      <tr>
                        <td style="padding:14px 0;border-bottom:1px solid #1B4A2F;">
                          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                            <tr>
                              <td align="left" style="font-family:'Geist',Helvetica,Arial,sans-serif;font-size:14px;color:#A8C9B0;">● Target</td>
                              <td align="right" style="font-family:'JetBrains Mono',Menlo,Consolas,monospace;font-size:16px;font-weight:500;color:#A8C9B0;letter-spacing:-0.01em;">$13.03</td>
                            </tr>
                          </table>
                        </td>
                      </tr>
                      <tr>
                        <td style="padding:14px 0;border-bottom:1px solid #1B4A2F;">
                          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                            <tr>
                              <td align="left" style="font-family:'Geist',Helvetica,Arial,sans-serif;font-size:14px;color:#A8C9B0;">● Walmart</td>
                              <td align="right" style="font-family:'JetBrains Mono',Menlo,Consolas,monospace;font-size:16px;font-weight:500;color:#A8C9B0;letter-spacing:-0.01em;">$4.38</td>
                            </tr>
                          </table>
                        </td>
                      </tr>
                      <tr>
                        <td style="padding:14px 0;border-bottom:1px solid #1B4A2F;">
                          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                            <tr>
                              <td align="left" style="font-family:'Geist',Helvetica,Arial,sans-serif;font-size:14px;color:#ffffff;font-weight:600;"><span style="color:#5CE26A;">●</span>&nbsp;Aldi</td>
                              <td align="right" style="font-family:'JetBrains Mono',Menlo,Consolas,monospace;font-size:16px;font-weight:500;color:#5CE26A;letter-spacing:-0.01em;">$3.74</td>
                            </tr>
                          </table>
                        </td>
                      </tr>
                    </table>

                    <p style="margin:14px 0 0 0;font-family:'Geist',Helvetica,Arial,sans-serif;font-size:12px;color:#C9DAC9;line-height:1.5;">
                      2 items · chicken + ground beef · prices live as of last Sat.
                    </p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Three quiet promises -->
          <tr>
            <td class="px-40" style="padding:40px 40px 24px;">
              <p style="margin:0 0 18px 0;font-family:'JetBrains Mono',Menlo,Consolas,monospace;font-size:11px;color:#7A847C;letter-spacing:0.14em;text-transform:uppercase;">
                What you get — three things
              </p>

              <!-- 01 -->
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-top:1px solid #E5DCC2;">
                <tr>
                  <td width="52" valign="top" style="padding:20px 22px 20px 0;font-family:'Instrument Serif',Georgia,serif;font-size:28px;color:#0E3A22;line-height:1;">01</td>
                  <td valign="top" style="padding:20px 0;border-bottom:1px solid #E5DCC2;">
                    <div style="font-family:'Instrument Serif',Georgia,serif;font-size:22px;color:#082517;letter-spacing:-0.01em;line-height:1.15;">Real prices, every store.</div>
                    <div style="font-family:'Geist',Helvetica,Arial,sans-serif;font-size:14px;line-height:1.55;color:#3B4A40;margin-top:6px;">Not "estimated." Not "circular." The actual shelf prices, refreshed weekly.</div>
                  </td>
                </tr>
              </table>
              <!-- 02 -->
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td width="52" valign="top" style="padding:20px 22px 20px 0;font-family:'Instrument Serif',Georgia,serif;font-size:28px;color:#0E3A22;line-height:1;">02</td>
                  <td valign="top" style="padding:20px 0;border-bottom:1px solid #E5DCC2;">
                    <div style="font-family:'Instrument Serif',Georgia,serif;font-size:22px;color:#082517;letter-spacing:-0.01em;line-height:1.15;">Carts that save the most.</div>
                    <div style="font-family:'Geist',Helvetica,Arial,sans-serif;font-size:14px;line-height:1.55;color:#3B4A40;margin-top:6px;">Single-store and multi-store splits ranked by what you keep — not by what stores pay us.</div>
                  </td>
                </tr>
              </table>
              <!-- 03 -->
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td width="52" valign="top" style="padding:20px 22px 20px 0;font-family:'Instrument Serif',Georgia,serif;font-size:28px;color:#0E3A22;line-height:1;">03</td>
                  <td valign="top" style="padding:20px 0;border-bottom:1px solid #E5DCC2;">
                    <div style="font-family:'Instrument Serif',Georgia,serif;font-size:22px;color:#082517;letter-spacing:-0.01em;line-height:1.15;">A real human on the other end.</div>
                    <div style="font-family:'Geist',Helvetica,Arial,sans-serif;font-size:14px;line-height:1.55;color:#3B4A40;margin-top:6px;">
                      My personal email is <a href="mailto:alston@joinprox.com" style="color:#0E3A22;font-weight:600;">alston@joinprox.com</a> and my phone number is<br/><a href="sms:+17573537478" style="color:#0E3A22;font-weight:600;">(757)&nbsp;353-7478</a>. If anything isn't to your liking or you don't understand something, shoot me an email or text and I'll fix it ASAP.
                    </div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Pull quote / beta perk -->
          <tr>
            <td class="px-40" style="padding:24px 40px 8px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#D6F0AA" style="background:#D6F0AA;border-radius:18px;">
                <tr>
                  <td style="padding:24px 26px;">
                    <p style="margin:0;font-family:'JetBrains Mono',Menlo,Consolas,monospace;font-size:10px;color:#0E3A22;letter-spacing:0.14em;text-transform:uppercase;">Beta perk</p>
                    <p style="margin:8px 0 0 0;font-family:'Instrument Serif',Georgia,serif;font-size:26px;line-height:1.15;color:#082517;letter-spacing:-0.01em;">
                      Unlimited <span style="font-style:italic;">free access</span> while we're in beta.
                    </p>
                    <p style="margin:10px 0 0 0;font-family:'Geist',Helvetica,Arial,sans-serif;font-size:13px;color:#3B4A40;line-height:1.55;">
                      No card, no caps. Check out the full capabilities of the app, as a thank you for trusting us with finding them for you.
                    </p>
                    <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin-top:16px;">
                      <tr>
                        <td bgcolor="#082517" style="border-radius:10px;">
                          <!--[if mso]>
                          <v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" href="https://joinprox.com/deals" style="height:42px;v-text-anchor:middle;width:180px;" arcsize="24%" stroke="f" fillcolor="#082517">
                            <w:anchorlock/>
                            <center style="color:#5CE26A;font-family:Helvetica,Arial,sans-serif;font-size:13px;font-weight:700;">View this week's deals</center>
                          </v:roundrect>
                          <![endif]-->
                          <!--[if !mso]><!-->
                          <a href="https://joinprox.com/deals" style="display:inline-block;background:#082517;color:#5CE26A;text-decoration:none;font-family:'Geist',Helvetica,Arial,sans-serif;font-weight:700;font-size:13px;padding:11px 18px;border-radius:10px;letter-spacing:-0.01em;">
                            View this week's deals
                          </a>
                          <!--<![endif]-->
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Sign-off -->
          <tr>
            <td class="px-40" style="padding:32px 40px 12px;">
              <p style="margin:0;font-family:'Instrument Serif',Georgia,serif;font-style:italic;font-size:22px;color:#082517;">— Alston</p>
              <p style="margin:2px 0 0 0;font-family:'Geist',Helvetica,Arial,sans-serif;font-size:12px;color:#7A847C;">founder, Prox · alston@joinprox.com</p>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td class="px-40" style="border-top:1px solid #E5DCC2;padding:20px 40px 30px;margin-top:24px;text-align:center;font-family:'Geist',Helvetica,Arial,sans-serif;font-size:11px;color:#7A847C;line-height:1.7;">
              <div>Prox, LLC · 2903 Lincoln Blvd, Santa Monica, CA 90405</div>
              <div>
                <a href="${siteUrl}/unsubscribe?email=${encodedEmail}" style="color:#7A847C;text-decoration:underline;">unsubscribe</a>
                &nbsp;·&nbsp;
                <a href="https://joinprox.com" style="color:#7A847C;text-decoration:underline;">joinprox.com</a>
              </div>
            </td>
          </tr>

        </table>

        <div style="height:18px;line-height:18px;font-size:1px;">&nbsp;</div>

      </td>
    </tr>
  </table>
</body>
</html>`,
    });

    // Check for Resend errors
    if ((emailResponse as any)?.error) {
      console.error("Resend error:", JSON.stringify((emailResponse as any).error));
      return new Response(
        JSON.stringify({ error: "Resend send failed", details: (emailResponse as any).error }),
        { status: 502, headers: { "Content-Type": "application/json", ...corsHeaders } }
      );
    }

    console.log("Email sent successfully:", JSON.stringify(emailResponse));

    return new Response(JSON.stringify(emailResponse), {
      status: 200,
      headers: {
        "Content-Type": "application/json",
        ...corsHeaders,
      },
    });
  } catch (error: any) {
    console.error("Error in send-welcome-email function:", error?.message || error);
    return new Response(JSON.stringify({ error: error.message }), {
      status: 500,
      headers: { "Content-Type": "application/json", ...corsHeaders },
    });
  }
};

serve(handler);
