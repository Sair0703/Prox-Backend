import { serve } from "https://deno.land/std@0.190.0/http/server.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const handler = async (req: Request): Promise<Response> => {
  // Handle CORS preflight requests
  if (req.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders });
  }

  try {
    const payload = await req.json();
    
    console.log("Email hook triggered:", payload);
    
    // Block Supabase's automatic confirmation emails
    // Your custom welcome email function handles this instead
    if (payload.email_action_type === "signup") {
      console.log("Blocking automatic signup confirmation email");
      return new Response(
        JSON.stringify({
          decision: "reject",
          message: "Custom email will be sent via edge function"
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json", ...corsHeaders },
        }
      );
    }
    
    // Allow all other email types (password reset, magic link, etc.)
    return new Response(
      JSON.stringify({
        decision: "continue"
      }),
      {
        status: 200,
        headers: { "Content-Type": "application/json", ...corsHeaders },
      }
    );
  } catch (error: any) {
    console.error("Error in send-email hook:", error);
    // If hook fails, allow email to be sent (fail-safe)
    return new Response(
      JSON.stringify({
        decision: "continue"
      }),
      {
        status: 200,
        headers: { "Content-Type": "application/json", ...corsHeaders },
      }
    );
  }
};

serve(handler);
