// Internal admin utility: send a one-off email via Resend.
// Requires a valid service-role JWT (verify_jwt enabled) — not callable by the public.
import { serve } from "https://deno.land/std@0.190.0/http/server.ts";
import { Resend } from "npm:resend@2.0.0";

const resend = new Resend(Deno.env.get("RESEND_API_KEY"));

serve(async (req: Request): Promise<Response> => {
  try {
    const { to, subject, html } = await req.json();
    if (!to || !subject || !html) {
      return new Response(JSON.stringify({ error: "to, subject, html required" }), { status: 400 });
    }
    const result = await resend.emails.send({
      from: "Alston at Prox <alston@joinprox.com>",
      to: [to],
      reply_to: "alston@joinprox.com",
      subject,
      html,
    });
    return new Response(JSON.stringify(result), { status: 200, headers: { "Content-Type": "application/json" } });
  } catch (e: any) {
    return new Response(JSON.stringify({ error: e.message }), { status: 500 });
  }
});
