// index.ts for Edge Function: generate-estimated-addresses

import { serve } from "https://deno.land/std@0.177.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";


const googleMapsApiKey = Deno.env.get("GOOGLE_MAPS_API_KEY") ?? "";
console.error("GOOGLE_MAPS_API_KEY loaded:", !!googleMapsApiKey);
console.error("GOOGLE_MAPS_API_KEY prefix:", googleMapsApiKey.slice(0, 8));

const supabaseUrl = Deno.env.get("SUPABASE_URL") ?? "";
const supabaseServiceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";

if (!supabaseUrl || !supabaseServiceRoleKey || !googleMapsApiKey) {
  console.error("Missing required env vars.");
  console.error("SUPABASE_URL:", !!supabaseUrl);
  console.error("SUPABASE_SERVICE_ROLE_KEY:", !!supabaseServiceRoleKey);
  console.error("GOOGLE_MAPS_API_KEY:", !!googleMapsApiKey);
}

// Supabase client with service role key (server-side only!)
const supabase = createClient(supabaseUrl, supabaseServiceRoleKey, {
  auth: {
    autoRefreshToken: false,
    persistSession: false,
  },
});

type WaitlistRow = {
  id: string | number;
  zip_code: string | null;
  estimated_address: string | null;
};

async function geocodeZip(zip: string) {
  const url = new URL("https://maps.googleapis.com/maps/api/geocode/json");

  // Force Google to interpret this as a US postal code
  url.searchParams.set("components", `postal_code:${zip}|country:US`);
  url.searchParams.set("key", googleMapsApiKey);

  const res = await fetch(url.toString());
  if (!res.ok) throw new Error(`Geocode HTTP error: ${res.status}`);

  const json = await res.json();

  if (json.status !== "OK") {
    throw new Error(
      `Geocode error for ZIP ${zip}: status=${json.status}, error=${
        json.error_message ?? "n/a"
      }`,
    );
  }

  if (!json.results || json.results.length === 0) {
    throw new Error(`Geocode returned no results for ZIP: ${zip}`);
  }

  const location = json.results[0].geometry.location;
  return { lat: location.lat as number, lng: location.lng as number };
}

async function getFormattedAddressFromPlace(placeId: string) {
  const url = new URL("https://maps.googleapis.com/maps/api/geocode/json");
  url.searchParams.set("place_id", placeId);
  url.searchParams.set("key", googleMapsApiKey);

  const res = await fetch(url.toString());
  if (!res.ok) throw new Error(`Geocode (place) HTTP error: ${res.status}`);

  const json = await res.json();

  if (json.status !== "OK") {
    throw new Error(
      `Geocode (place) error: status=${json.status}, error=${
        json.error_message ?? "n/a"
      }`,
    );
  }

  if (!json.results || json.results.length === 0) {
    throw new Error("Geocode (place) returned no results");
  }

  // This should look like "123 Main St, City, ST 12345, USA"
  const formatted: string = json.results[0].formatted_address;

  return formatted;
}

async function getRandomAddressNear(lat: number, lng: number, zip: string) {
  const url = new URL(
    "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
  );
  url.searchParams.set("location", `${lat},${lng}`);
  url.searchParams.set("radius", "3000"); // meters (~3km)
  url.searchParams.set("key", googleMapsApiKey);

  const res = await fetch(url.toString());
  if (!res.ok) throw new Error(`Places API error: ${res.status}`);

  const json = await res.json();

  if (!json.results || json.results.length === 0) {
    throw new Error("No nearby places found");
  }

  const places = json.results as Array<any>;

  // Try up to N places to get a nice, full US-style address
  const maxTries = Math.min(places.length, 100);

  for (let i = 0; i < maxTries; i++) {
    const idx = Math.floor(Math.random() * places.length);
    const place = places[idx];

    if (!place.place_id) {
      continue;
    }

    const fullAddress = await getFormattedAddressFromPlace(place.place_id);

    // Basic sanity checks:
    // - contains the ZIP we’re targeting
    // - contains at least one digit (street number)
    if (!fullAddress.includes(zip)) continue;
    if (!/[0-9]/.test(fullAddress)) continue;

    // Optionally strip trailing ", USA"
    const cleaned = fullAddress.replace(/,?\s*(USA|United States)$/i, "");

    return cleaned;
  }

  throw new Error("No suitable full address found near this ZIP");
}

serve(async (req: Request) => {
  try {
    // OPTIONAL: simple auth using a header
    // if (internalJobSecret) {
    //   const incomingSecret = req.headers.get("x-internal-token") ?? "";
    //   if (incomingSecret !== internalJobSecret) {
    //     return new Response(JSON.stringify({ error: "Unauthorized" }), {
    //       status: 401,
    //       headers: { "Content-Type": "application/json" },
    //     });
    //   }
    // }

    // 1. Get up to 25 waitlist rows that need estimated_address
    const { data: rows, error: fetchError } = await supabase
      .from<WaitlistRow>("waitlist")
      .select("id, zip_code, estimated_address")
      .is("estimated_address", null)
      .not("zip_code", "is", null)
      .limit(25);

    if (fetchError) {
      console.error("Error fetching waitlist rows:", fetchError);
      return new Response(JSON.stringify({ error: fetchError.message }), {
        status: 500,
        headers: { "Content-Type": "application/json" },
      });
    }

    if (!rows || rows.length === 0) {
      return new Response(
        JSON.stringify({ message: "No rows with NULL estimated_address" }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }

    const results: Array<{
      id: string | number;
      success: boolean;
      message: string;
    }> = [];

    // 2. Process each row sequentially to avoid hammering Google APIs
    for (const row of rows) {
      if (!row.zip_code) {
        results.push({
          id: row.id,
          success: false,
          message: "Missing zip_code",
        });
        continue;
      }

      try {
        // a) Geocode ZIP -> lat/lng
        const { lat, lng } = await geocodeZip(row.zip_code);

        // b) Get random nearby place address
        const address = await getRandomAddressNear(lat, lng, row.zip_code);

        // c) Update waitlist row
        const { error: updateError } = await supabase
          .from("waitlist")
          .update({ estimated_address: address })
          .eq("id", row.id);

        if (updateError) {
          console.error("Update error for row", row.id, updateError);
          results.push({
            id: row.id,
            success: false,
            message: updateError.message,
          });
          continue;
        }

        results.push({
          id: row.id,
          success: true,
          message: `Updated with address: ${address}`,
        });
      } catch (err: any) {
        console.error("Error processing row", row.id, err);
        results.push({
          id: row.id,
          success: false,
          message: err?.message ?? "Unknown error",
        });
      }
    }

    return new Response(JSON.stringify({ processed: results }, null, 2), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  } catch (err: any) {
    console.error("Unexpected error:", err);
    return new Response(
      JSON.stringify({ error: err?.message ?? "Unexpected error" }),
      {
        status: 500,
        headers: { "Content-Type": "application/json" },
      },
    );
  }
});