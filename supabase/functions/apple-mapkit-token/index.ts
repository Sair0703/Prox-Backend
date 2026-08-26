import {
  importPKCS8,
  SignJWT,
} from "npm:jose@5.9.6";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
};

const TOKEN_LIFETIME_SECONDS = 6 * 24 * 60 * 60;

let privateKeyPromise: Promise<CryptoKey> | null = null;

function jsonResponse(
  body: Record<string, unknown>,
  status = 200,
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      ...corsHeaders,
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
    },
  });
}

function getRequiredSecret(name: string): string {
  const value = Deno.env.get(name);

  if (!value) {
    throw new Error(`Missing required secret: ${name}`);
  }

  return value;
}

function getPrivateKey(): Promise<CryptoKey> {
  if (!privateKeyPromise) {
    const rawPrivateKey = getRequiredSecret(
      "APPLE_MAPS_PRIVATE_KEY",
    );

    // Supports secrets entered either with real newlines
    // or with escaped "\n" characters.
    const formattedPrivateKey = rawPrivateKey.replace(
      /\\n/g,
      "\n",
    );

    privateKeyPromise = importPKCS8(
      formattedPrivateKey,
      "ES256",
    );
  }

  return privateKeyPromise;
}

Deno.serve(async (request: Request) => {
  if (request.method === "OPTIONS") {
    return new Response(null, {
      status: 204,
      headers: corsHeaders,
    });
  }

  if (
    request.method !== "GET" &&
    request.method !== "POST"
  ) {
    return jsonResponse(
      { error: "Method not allowed" },
      405,
    );
  }

  try {
    const teamId = getRequiredSecret(
      "APPLE_MAPS_TEAM_ID",
    );
    const keyId = getRequiredSecret(
      "APPLE_MAPS_KEY_ID",
    );
    const privateKey = await getPrivateKey();

    const issuedAt = Math.floor(Date.now() / 1000);
    const expiresAt =
      issuedAt + TOKEN_LIFETIME_SECONDS;

    const token = await new SignJWT({})
      .setProtectedHeader({
        alg: "ES256",
        kid: keyId,
        typ: "JWT",
      })
      .setIssuer(teamId)
      .setIssuedAt(issuedAt)
      .setExpirationTime(expiresAt)

      // Intentionally do not add an "origin" claim.
      // This makes the token unrestricted.
      .sign(privateKey);

    return jsonResponse({
      token,
      issuedAt,
      expiresAt,
    });
  } catch (error) {
    console.error(
      "[apple-mapkit-token] Token generation failed:",
      error,
    );

    return jsonResponse(
      {
        error: "Unable to generate Apple Maps token",
      },
      500,
    );
  }
});