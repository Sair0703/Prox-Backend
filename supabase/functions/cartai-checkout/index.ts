import { serve } from "https://deno.land/std@0.168.0/http/server.ts";

const CARTAI_BASE_URL = "https://api.cartai.ai";
const TEST_CUSTOMER = {
  contact: {
    firstName: "Carol",
    lastName: "Sturka",
    email: "carol.sturka@gmail.com",
    phone: "+16507083507",
  },
  shippingAddress: {
    addressLine1: "6104 Plano Parkway",
    addressLine2: "",
    city: "Plano",
    province: "Texas",
    postalCode: "75093",
    country: "US",
  },
  billingAddress: {
    addressLine1: "6104 Plano Parkway",
    addressLine2: "",
    city: "Plano",
    province: "Texas",
    postalCode: "75093",
    country: "US",
  },
  shippingMethod: { strategy: "cheapest" },
  payment: {
    provider: "test",
    data: {
      cardNumber: "4242424242424242",
      expiryMonth: "12",
      expiryYear: "2034",
      cvv: "444",
      name: "Carol Sturka",
    },
  },
};

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

type CartItem = { name?: unknown; quantity?: unknown; url?: unknown };
type SearchProduct = { merchant?: unknown; directUrl?: unknown };

function jsonResponse(status: number, body: Record<string, unknown>) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
}

function normalizeMerchant(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]/g, "");
}

async function cartAiFetch(apiKey: string, path: string, init: RequestInit) {
  return fetch(`${CARTAI_BASE_URL}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "x-api-key": apiKey,
      ...(init.headers ?? {}),
    },
  });
}

async function readCartAiError(response: Response): Promise<string> {
  const body = await response.text();
  return body.trim() || `Cart AI request failed (${response.status}).`;
}

function getValidItems(value: unknown): Array<{ name: string; quantity: number; url: string | null }> {
  if (!Array.isArray(value)) return [];

  return value.flatMap((item: CartItem) => {
    const name = typeof item?.name === "string" ? item.name.trim() : "";
    const quantity = Number(item?.quantity);
    const rawUrl = typeof item?.url === "string" ? item.url.trim() : "";
    let url: string | null = null;
    if (rawUrl) {
      try {
        const parsed = new URL(rawUrl);
        if (parsed.protocol === "https:") url = parsed.toString();
      } catch {
        url = null;
      }
    }
    return name && Number.isInteger(quantity) && quantity > 0 && (!rawUrl || url)
      ? [{ name, quantity, url }]
      : [];
  });
}

function isRetailerUrl(url: string, retailer: string): boolean {
  const host = new URL(url).hostname.toLowerCase().replace(/^www\./, "");
  const merchant = normalizeMerchant(retailer);
  if (merchant === "walmart") return host === "walmart.com" || host.endsWith(".walmart.com");
  return false;
}

async function searchRetailerProduct(apiKey: string, name: string, retailer: string): Promise<string | null> {
  const response = await cartAiFetch(apiKey, "/product/search", {
    method: "POST",
    body: JSON.stringify({ name, merchant: retailer }),
  });
  if (!response.ok) throw new Error(await readCartAiError(response));

  const payload = await response.json() as { data?: { products?: SearchProduct[] } };
  const desiredMerchant = normalizeMerchant(retailer);
  const product = payload.data?.products?.find((candidate) =>
    typeof candidate.merchant === "string" &&
    normalizeMerchant(candidate.merchant) === desiredMerchant &&
    typeof candidate.directUrl === "string" &&
    candidate.directUrl.startsWith("https://"),
  );

  return typeof product?.directUrl === "string" ? product.directUrl : null;
}

serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { status: 200, headers: corsHeaders });
  if (req.method !== "POST") return jsonResponse(405, { message: "Method not allowed." });

  const apiKey = Deno.env.get("CARTAI_API_KEY");
  if (!apiKey) return jsonResponse(500, { message: "CARTAI_API_KEY is not configured." });

  let payload: Record<string, unknown>;
  try {
    payload = await req.json();
  } catch {
    return jsonResponse(400, { message: "Request body must be valid JSON." });
  }

  const action = payload.action;
  if (action === "status") {
    const taskId = typeof payload.task_id === "string" ? payload.task_id.trim() : "";
    if (!taskId) return jsonResponse(400, { message: "task_id is required." });

    const response = await cartAiFetch(apiKey, `/checkout/${encodeURIComponent(taskId)}`, { method: "GET" });
    if (!response.ok) return jsonResponse(502, { message: await readCartAiError(response) });
    const task = await response.json() as { tid?: unknown; status?: unknown };
    if (typeof task.tid !== "string" || typeof task.status !== "string") {
      return jsonResponse(502, { message: "Cart AI returned an invalid checkout status." });
    }
    return jsonResponse(200, { task_id: task.tid, status: task.status });
  }

  if (action !== "create") return jsonResponse(400, { message: "action must be create or status." });

  const retailer = typeof payload.retailer === "string" ? payload.retailer.trim() : "";
  const items = getValidItems(payload.items);
  if (!retailer) return jsonResponse(400, { message: "retailer is required." });
  if (items.length === 0) return jsonResponse(400, { message: "At least one valid cart item is required." });

  try {
    if (items.some((item) => item.url && !isRetailerUrl(item.url, retailer))) {
      return jsonResponse(400, { message: `An item URL does not belong to ${retailer}.` });
    }
    const urls = await Promise.all(items.map((item) =>
      item.url ? Promise.resolve(item.url) : searchRetailerProduct(apiKey, item.name, retailer)
    ));
    if (urls.some((url) => !url)) {
      return jsonResponse(422, { message: `Cart AI could not find every item at ${retailer}.` });
    }

    const response = await cartAiFetch(apiKey, "/checkout", {
      method: "POST",
      body: JSON.stringify({
        customer: TEST_CUSTOMER,
        tasks: items.map((item, index) => ({ url: urls[index], quantity: item.quantity })),
      }),
    });
    if (!response.ok) return jsonResponse(502, { message: await readCartAiError(response) });

    const checkout = await response.json() as {
      taskId?: unknown;
      tid?: unknown;
      status?: unknown;
      data?: { taskId?: unknown; tid?: unknown; status?: unknown };
      tasks?: Array<{ taskId?: unknown; status?: unknown }>;
    };
    const taskId = checkout.tasks?.[0]?.taskId ?? checkout.data?.taskId ?? checkout.data?.tid ?? checkout.taskId ?? checkout.tid;
    const status = checkout.tasks?.[0]?.status ?? checkout.data?.status ?? checkout.status;
    if (typeof taskId !== "string") {
      console.error("[cartai-checkout] unrecognized create response", checkout);
      return jsonResponse(502, { message: "Cart AI did not return a checkout task." });
    }

    const tasks = checkout.tasks
      ?.filter((task): task is { taskId: string; status?: unknown } => typeof task.taskId === "string")
      .map((task) => ({
        task_id: task.taskId,
        status: typeof task.status === "string" ? task.status : "QUEUED",
      }));

    return jsonResponse(201, {
      task_id: taskId,
      status: typeof status === "string" ? status : "QUEUED",
      ...(tasks?.length ? { tasks } : {}),
    });
  } catch (error) {
    console.error("[cartai-checkout] request failed", error);
    return jsonResponse(502, { message: error instanceof Error ? error.message : "Cart AI is unavailable." });
  }
});
