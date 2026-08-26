import { serve } from "https://deno.land/std@0.168.0/http/server.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { status: 200, headers: corsHeaders });
  if (req.method !== "POST") {
    return new Response(JSON.stringify({ message: "Method not allowed." }), {
      status: 405,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  try {
    const payload = await req.json() as { tid?: unknown; status?: unknown; merchant?: unknown };
    if (typeof payload.tid !== "string" || typeof payload.status !== "string") {
      return new Response(JSON.stringify({ message: "Cart AI webhook payload is invalid." }), {
        status: 400,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }

    // The Cart AI portal is configured without authentication for this test
    // endpoint. Log only non-sensitive routing fields; the payload includes
    // customer data and must not be written to logs.
    console.log("[cartai-webhook] received", {
      taskId: payload.tid,
      status: payload.status,
      merchant: typeof payload.merchant === "string" ? payload.merchant : null,
    });

    return new Response(JSON.stringify({ received: true }), {
      status: 200,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch {
    return new Response(JSON.stringify({ message: "Cart AI webhook body must be valid JSON." }), {
      status: 400,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
