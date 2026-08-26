import { serve } from "https://deno.land/std@0.190.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.57.2";

const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
const supabaseServiceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type",
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
  appPreference?: string;
  householdSize?: number | null;
  phoneNumber?: string | null;
}

function jsonResponse(status: number, body: Record<string, unknown>) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json",
      ...corsHeaders,
    },
  });
}

function normalizeEmail(email: string): string {
  return email.trim().toLowerCase();
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function getErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message) return error.message;
  if (isRecord(error) && typeof error.message === "string") return error.message;
  return fallback;
}

function isAlreadyRegisteredError(error: unknown): boolean {
  const message = getErrorMessage(error, "").toLowerCase();
  return (
    message.includes("already registered") ||
    message.includes("already been registered") ||
    message.includes("already exists") ||
    message.includes("duplicate key") ||
    message.includes("user already registered")
  );
}

function sanitizeRetailers(retailers: unknown): string[] {
  if (!Array.isArray(retailers)) return [];
  return [
    ...new Set(
      retailers
        .filter((value): value is string => typeof value === "string")
        .map((value) => value.trim())
        .filter(Boolean)
    ),
  ].slice(0, 3);
}

function validateBody(body: Partial<CompleteSignupRequest>): string | null {
  if (!body.email?.trim()) return "Email is required.";
  if (!body.password?.trim()) return "Password is required.";
  if (!body.firstName?.trim()) return "First name is required.";
  if (!body.lastName?.trim()) return "Last name is required.";
  if (!body.dateOfBirth?.trim()) return "Date of birth is required.";
  if (!body.genderIdentity?.trim()) return "Gender identity is required.";
  if (!body.zipCode?.trim()) return "ZIP code is required.";
  if (sanitizeRetailers(body.preferredRetailers).length === 0) {
    return "At least one preferred retailer is required.";
  }
  return null;
}

async function findAuthUserByEmail(
  supabaseAdmin: ReturnType<typeof createClient>,
  normalizedEmail: string
) {
  let page = 1;
  const perPage = 1000;

  while (page <= 50) {
    const { data, error } = await supabaseAdmin.auth.admin.listUsers({
      page,
      perPage,
    });

    if (error) throw error;

    const users = data?.users ?? [];
    const match = users.find(
      (user) => normalizeEmail(user.email ?? "") === normalizedEmail
    );

    if (match) return match;
    if (users.length < perPage) return null;

    page += 1;
  }

  return null;
}

async function rollbackCreatedUser(
  supabaseAdmin: ReturnType<typeof createClient>,
  userId: string | null
) {
  if (!userId) return;

  const { error } = await supabaseAdmin.auth.admin.deleteUser(userId);
  if (error) {
    console.error("Failed to rollback created auth user:", error);
  }
}

serve(async (req: Request): Promise<Response> => {
  if (req.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders });
  }

  let createdUserId: string | null = null;

  try {
    const rawBody = (await req.json()) as Partial<CompleteSignupRequest>;
    const validationError = validateBody(rawBody);
    if (validationError) {
      return jsonResponse(400, { success: false, error: validationError });
    }

    const email = normalizeEmail(rawBody.email!);
    const password = rawBody.password!.trim();
    const firstName = rawBody.firstName!.trim();
    const lastName = rawBody.lastName!.trim();
    const dateOfBirth = rawBody.dateOfBirth!.trim();
    const genderIdentity = rawBody.genderIdentity!.trim();
    const zipCode = rawBody.zipCode!.trim();
    const preferredRetailers = sanitizeRetailers(rawBody.preferredRetailers);
    const appPreference = rawBody.appPreference?.trim() || "mobile";
    const householdSize = rawBody.householdSize ?? null;
    const phoneNumber = rawBody.phoneNumber?.trim() || null;

    const supabaseAdmin = createClient(supabaseUrl, supabaseServiceKey, {
      auth: {
        autoRefreshToken: false,
        persistSession: false,
      },
    });

    const { data: waitlistRow, error: waitlistLookupError } = await supabaseAdmin
      .from("waitlist")
      .select("id,user_id,email,metadata")
      .eq("email", email)
      .maybeSingle();

    if (waitlistLookupError) {
      throw waitlistLookupError;
    }

    if (!waitlistRow?.id) {
      return jsonResponse(409, {
        success: false,
        error: "No waitlist entry exists for this email.",
      });
    }

    const existingMetadata = isRecord(waitlistRow.metadata)
      ? waitlistRow.metadata
      : {};
    const { oauth_provider: _ignoreOauthProvider, ...restMetadata } =
      existingMetadata;

    const authMetadata = {
      email,
      first_name: firstName,
      last_name: lastName,
      phone_number: phoneNumber,
      date_of_birth: dateOfBirth,
      gender_identity: genderIdentity,
      zip_code: zipCode,
      preferred_retailers: preferredRetailers,
      app_preference: appPreference,
      display_name: `${firstName} ${lastName}`.trim(),
    };

    let userId = waitlistRow.user_id ?? null;

    if (userId) {
      const { error: updateUserError } =
        await supabaseAdmin.auth.admin.updateUserById(userId, {
          email,
          password,
          email_confirm: true,
          user_metadata: authMetadata,
        });

      if (updateUserError) {
        throw updateUserError;
      }
    } else {
      const { data: createdUser, error: createUserError } =
        await supabaseAdmin.auth.admin.createUser({
          email,
          password,
          email_confirm: true,
          user_metadata: authMetadata,
        });

      if (createUserError) {
        if (!isAlreadyRegisteredError(createUserError)) {
          throw createUserError;
        }

        const existingUser = await findAuthUserByEmail(supabaseAdmin, email);
        if (!existingUser?.id) {
          return jsonResponse(409, {
            success: false,
            error:
              "An auth account already exists for this email, but it could not be linked automatically.",
          });
        }

        userId = existingUser.id;

        const { error: updateExistingError } =
          await supabaseAdmin.auth.admin.updateUserById(userId, {
            email,
            password,
            email_confirm: true,
            user_metadata: authMetadata,
          });

        if (updateExistingError) {
          throw updateExistingError;
        }
      } else {
        userId = createdUser.user.id;
        createdUserId = userId;
      }
    }

    if (!userId) {
      throw new Error("Unable to resolve auth user for signup completion.");
    }

    const waitlistPayload = {
      user_id: userId,
      email,
      name: `${firstName} ${lastName}`.trim() || email,
      first_name: firstName,
      last_name: lastName,
      phone_number: phoneNumber,
      date_of_birth: dateOfBirth,
      zip_code: zipCode,
      preferred_retailers:
        preferredRetailers.length > 0 ? preferredRetailers : null,
      device_preference: appPreference === "both" ? null : appPreference,
      metadata: {
        ...restMetadata,
        source: "mobile-app",
        created_from: "mobile-signup",
        household_size: householdSize,
      },
    };

    const { error: waitlistWriteError } = await supabaseAdmin
      .from("waitlist")
      .update(waitlistPayload)
      .eq("id", waitlistRow.id);

    if (waitlistWriteError) {
      await rollbackCreatedUser(supabaseAdmin, createdUserId);
      throw waitlistWriteError;
    }

    const profilePayload = {
      id: userId,
      user_id: userId,
      first_name: firstName,
      last_name: lastName,
      display_name: `${firstName} ${lastName}`.trim(),
      phone_number: phoneNumber,
      date_of_birth: dateOfBirth,
      gender_identity: genderIdentity,
      zip_code: zipCode,
      household_size: householdSize,
      preferred_retailers:
        preferredRetailers.length > 0 ? preferredRetailers : null,
      app_preference: appPreference,
      email,
      updated_at: new Date().toISOString(),
    };

    const { error: profileWriteError } = await supabaseAdmin
      .from("profiles")
      .upsert(profilePayload, { onConflict: "user_id" });

    if (profileWriteError) {
      await rollbackCreatedUser(supabaseAdmin, createdUserId);
      throw profileWriteError;
    }

    return jsonResponse(200, {
      success: true,
      userId,
      message: "Account completed successfully.",
    });
  } catch (error) {
    console.error("complete-waitlist-signup-v2 failed:", error);
    return jsonResponse(500, {
      success: false,
      error: getErrorMessage(error, "Failed to complete signup."),
    });
  }
});
