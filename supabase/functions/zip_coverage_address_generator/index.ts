// index.ts for Edge Function: generate-zip-coverage-addresses

import { serve } from "https://deno.land/std@0.177.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const googleMapsApiKey = Deno.env.get("GOOGLE_MAPS_API_KEY") ?? "";
const supabaseUrl = Deno.env.get("SUPABASE_URL") ?? "";
const supabaseServiceRoleKey =
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";

console.error("GOOGLE_MAPS_API_KEY loaded:", !!googleMapsApiKey);

if (!supabaseUrl || !supabaseServiceRoleKey || !googleMapsApiKey) {
  console.error("Missing required environment variables.");
  console.error("SUPABASE_URL:", !!supabaseUrl);
  console.error("SUPABASE_SERVICE_ROLE_KEY:", !!supabaseServiceRoleKey);
  console.error("GOOGLE_MAPS_API_KEY:", !!googleMapsApiKey);
}

const supabase = createClient(supabaseUrl, supabaseServiceRoleKey, {
  auth: {
    autoRefreshToken: false,
    persistSession: false,
  },
});

type ZipCoverageRow = {
  id: string | number;
  zip_code: string | null;
  estimated_address: string | null;
};

async function geocodeZip(zip: string) {
  const url = new URL(
    "https://maps.googleapis.com/maps/api/geocode/json",
  );

  url.searchParams.set(
    "components",
    `postal_code:${zip}|country:US`,
  );
  url.searchParams.set("key", googleMapsApiKey);

  const res = await fetch(url.toString());

  if (!res.ok) {
    throw new Error(`Geocode HTTP error: ${res.status}`);
  }

  const json = await res.json();

  if (json.status !== "OK") {
    throw new Error(
      `Geocode error for ZIP ${zip}: status=${json.status}, error=${
        json.error_message ?? "n/a"
      }`,
    );
  }

  if (!json.results?.length) {
    throw new Error(`Geocode returned no results for ZIP ${zip}`);
  }

  const location = json.results[0].geometry.location;

  return {
    lat: location.lat as number,
    lng: location.lng as number,
  };
}

async function getFormattedAddressFromPlace(placeId: string) {
  const url = new URL(
    "https://maps.googleapis.com/maps/api/geocode/json",
  );

  url.searchParams.set("place_id", placeId);
  url.searchParams.set("key", googleMapsApiKey);

  const res = await fetch(url.toString());

  if (!res.ok) {
    throw new Error(
      `Geocode place HTTP error: ${res.status}`,
    );
  }

  const json = await res.json();

  if (json.status !== "OK") {
    throw new Error(
      `Geocode place error: status=${json.status}, error=${
        json.error_message ?? "n/a"
      }`,
    );
  }

  if (!json.results?.length) {
    throw new Error("Geocode place returned no results");
  }

  return json.results[0].formatted_address as string;
}

async function getRandomAddressNear(
  lat: number,
  lng: number,
  zip: string,
) {
  const url = new URL(
    "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
  );

  url.searchParams.set("location", `${lat},${lng}`);
  url.searchParams.set("radius", "3000");
  url.searchParams.set("key", googleMapsApiKey);

  const res = await fetch(url.toString());

  if (!res.ok) {
    throw new Error(`Places API HTTP error: ${res.status}`);
  }

  const json = await res.json();

  if (json.status !== "OK" && json.status !== "ZERO_RESULTS") {
    throw new Error(
      `Places API error: status=${json.status}, error=${
        json.error_message ?? "n/a"
      }`,
    );
  }

  if (!json.results?.length) {
    throw new Error(`No nearby places found for ZIP ${zip}`);
  }

  const places = json.results as Array<{
    place_id?: string;
  }>;

  /*
   * Shuffle the results so the function does not always choose
   * the first Google Places result.
   */
  const shuffledPlaces = [...places].sort(
    () => Math.random() - 0.5,
  );

  const maxTries = Math.min(shuffledPlaces.length, 20);

  for (let i = 0; i < maxTries; i++) {
    const place = shuffledPlaces[i];

    if (!place.place_id) {
      continue;
    }

    const fullAddress =
      await getFormattedAddressFromPlace(place.place_id);

    /*
     * Require an actual street number and an exact ZIP match.
     * This prevents an address from a neighboring ZIP from
     * being saved.
     */
    const containsTargetZip = new RegExp(
      `\\b${zip}\\b`,
    ).test(fullAddress);

    const containsStreetNumber = /\d/.test(fullAddress);

    if (!containsTargetZip || !containsStreetNumber) {
      continue;
    }

    return fullAddress.replace(
      /,?\s*(USA|United States)$/i,
      "",
    );
  }

  throw new Error(
    `No suitable exact-ZIP address found for ${zip}`,
  );
}

serve(async (_req: Request) => {
  try {
    /*
     * Get up to 25 zip_coverage_plan rows that do not yet
     * have an estimated address.
     */
    const { data: rows, error: fetchError } = await supabase
      .from("zip_coverage_plan")
      .select("id, zip_code, estimated_address")
      .is("estimated_address", null)
      .not("zip_code", "is", null)
      .limit(25);

    if (fetchError) {
      console.error(
        "Error fetching zip_coverage_plan rows:",
        fetchError,
      );

      return new Response(
        JSON.stringify({
          error: fetchError.message,
        }),
        {
          status: 500,
          headers: {
            "Content-Type": "application/json",
          },
        },
      );
    }

    const typedRows = rows as ZipCoverageRow[] | null;

    if (!typedRows?.length) {
      return new Response(
        JSON.stringify({
          message:
            "No zip_coverage_plan rows have a NULL estimated_address",
        }),
        {
          status: 200,
          headers: {
            "Content-Type": "application/json",
          },
        },
      );
    }

    const results: Array<{
      id: string | number;
      zip_code: string | null;
      success: boolean;
      message: string;
    }> = [];

    for (const row of typedRows) {
      const zip = row.zip_code?.trim();

      if (!zip) {
        results.push({
          id: row.id,
          zip_code: row.zip_code,
          success: false,
          message: "Missing zip_code",
        });

        continue;
      }

      /*
       * Preserve leading zeros and reject malformed ZIP values.
       */
      if (!/^\d{5}$/.test(zip)) {
        results.push({
          id: row.id,
          zip_code: row.zip_code,
          success: false,
          message: `Invalid ZIP format: ${zip}`,
        });

        continue;
      }

      try {
        const { lat, lng } = await geocodeZip(zip);

        const address = await getRandomAddressNear(
          lat,
          lng,
          zip,
        );

        const { error: updateError } = await supabase
          .from("zip_coverage_plan")
          .update({
            estimated_address: address,
          })
          .eq("id", row.id);

        if (updateError) {
          console.error(
            `Update error for row ${row.id}:`,
            updateError,
          );

          results.push({
            id: row.id,
            zip_code: zip,
            success: false,
            message: updateError.message,
          });

          continue;
        }

        results.push({
          id: row.id,
          zip_code: zip,
          success: true,
          message: `Updated with address: ${address}`,
        });
      } catch (err) {
        const message =
          err instanceof Error
            ? err.message
            : "Unknown error";

        console.error(
          `Error processing row ${row.id}:`,
          err,
        );

        results.push({
          id: row.id,
          zip_code: zip,
          success: false,
          message,
        });
      }
    }

    return new Response(
      JSON.stringify(
        {
          rows_selected: typedRows.length,
          successful: results.filter(
            (result) => result.success,
          ).length,
          failed: results.filter(
            (result) => !result.success,
          ).length,
          processed: results,
        },
        null,
        2,
      ),
      {
        status: 200,
        headers: {
          "Content-Type": "application/json",
        },
      },
    );
  } catch (err) {
    const message =
      err instanceof Error
        ? err.message
        : "Unexpected error";

    console.error("Unexpected error:", err);

    return new Response(
      JSON.stringify({
        error: message,
      }),
      {
        status: 500,
        headers: {
          "Content-Type": "application/json",
        },
      },
    );
  }
});