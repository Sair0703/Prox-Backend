import { serve } from "https://deno.land/std@0.190.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.57.2";
import { checkRateLimit, createRateLimitResponse } from "../_shared/rate-limiter.ts";

const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
const supabaseServiceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type",
};

// Rate limit: 10 signup attempts per minute per IP
const RATE_LIMIT_CONFIG = {
  maxRequests: 10,
  windowMs: 60 * 1000, // 1 minute
};

interface CompleteSignupRequest {
  email: string;
  password: string;
  firstName: string;
  lastName: string;
  dateOfBirth: string;
  genderIdentity: string;
  zipCode: string;
  preferredRetailers: string[];
  appPreference: string;
}

const handler = async (req: Request): Promise<Response> => {
  if (req.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders });
  }

  // Check rate limit
  const rateLimitResult = checkRateLimit(req, RATE_LIMIT_CONFIG);
  if (!rateLimitResult.allowed) {
    console.warn("Rate limit exceeded for complete-waitlist-signup");
    return createRateLimitResponse(rateLimitResult, corsHeaders);
  }

  try {
    const body: CompleteSignupRequest = await req.json();
    const { email, password, firstName, lastName, dateOfBirth, genderIdentity, zipCode, preferredRetailers, appPreference } = body;
    
    console.log("Attempting to complete signup for:", email);

    const supabaseAdmin = createClient(supabaseUrl, supabaseServiceKey, {
      auth: {
        autoRefreshToken: false,
        persistSession: false,
      },
    });

    // Find the user by email with pagination
    let user = null;
    let page = 1;
    const perPage = 1000;
    
    console.log("Searching for user by email...");
    
    while (!user && page <= 10) {
      const { data, error: listError } = await supabaseAdmin.auth.admin.listUsers({
        page,
        perPage
      });
      
      if (listError) {
        console.error("Error listing users:", listError);
        throw listError;
      }
      
      const users = data?.users || [];
      console.log(`Checking page ${page}, found ${users.length} users`);
      
      if (users.length === 0) break;
      
      user = users.find(u => u.email?.toLowerCase() === email.toLowerCase());
      
      if (user) {
        console.log("Found user:", user.id, user.email);
        break;
      }
      
      if (users.length < perPage) break;
      page++;
    }
    
    if (!user) {
      console.log("User not found in auth.users, checking if we need to create one");
      
      // User doesn't exist in auth yet, create them
      const { data: newUser, error: createError } = await supabaseAdmin.auth.admin.createUser({
        email: email,
        password: password,
        email_confirm: true,
        user_metadata: {
          first_name: firstName,
          last_name: lastName,
          date_of_birth: dateOfBirth,
          gender_identity: genderIdentity,
          zip_code: zipCode,
          preferred_retailers: preferredRetailers,
          app_preference: appPreference,
        }
      });
      
      if (createError) {
        console.error("Error creating user:", createError);
        throw createError;
      }
      
      user = newUser.user;
      console.log("Created new user:", user.id);
    } else {
      // User exists, update their password and metadata
      const { error: updateError } = await supabaseAdmin.auth.admin.updateUserById(
        user.id,
        {
          password: password,
          email_confirm: true,
          user_metadata: {
            first_name: firstName,
            last_name: lastName,
            date_of_birth: dateOfBirth,
            gender_identity: genderIdentity,
            zip_code: zipCode,
            preferred_retailers: preferredRetailers,
            app_preference: appPreference,
          }
        }
      );

      if (updateError) {
        console.error("Error updating user:", updateError);
        throw updateError;
      }
      
      console.log("Updated existing user:", user.id);
    }

    console.log("Successfully set password and updated profile for user:", user.id);

    // Update waitlist entry to mark as completed
    try {
      const { data: waitlistUser } = await supabaseAdmin
        .from('waitlist')
        .select('id')
        .eq('email', email)
        .maybeSingle();

      if (waitlistUser) {
        await supabaseAdmin
          .from('waitlist')
          .update({ user_id: user.id })
          .eq('email', email);
        
        console.log("Updated waitlist entry with user_id");
      }
    } catch (waitlistError) {
      console.error("Error updating waitlist:", waitlistError);
      // Don't fail the signup if waitlist update fails
    }

    return new Response(
      JSON.stringify({ 
        success: true,
        passwordSet: true,
        message: "Account completed successfully" 
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
    console.error("Error in complete-waitlist-signup function:", error);
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
