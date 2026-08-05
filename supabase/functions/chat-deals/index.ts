import "https://deno.land/x/xhr@0.1.0/mod.ts";
import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { checkRateLimit, createRateLimitResponse } from "../_shared/rate-limiter.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

// Rate limit: 20 requests per minute per IP (chatbot can be chatty)
const RATE_LIMIT_CONFIG = {
  maxRequests: 20,
  windowMs: 60 * 1000, // 1 minute
};

// Helper function to parse date range and check if today is within it
function isDateInRange(dateRangeStr: string): boolean {
  try {
    const today = new Date();
    
    // Parse formats like "12/03-12/09/2025" or "December 3-9, 2025"
    // Try numeric format first (12/03-12/09/2025)
    let match = dateRangeStr.match(/(\d{1,2})\/(\d{1,2})-(\d{1,2})\/(\d{1,2})\/(\d{4})/);
    if (match) {
      const [, startMonth, startDay, endMonth, endDay, year] = match;
      const startDate = new Date(parseInt(year), parseInt(startMonth) - 1, parseInt(startDay));
      const endDate = new Date(parseInt(year), parseInt(endMonth) - 1, parseInt(endDay));
      return today >= startDate && today <= endDate;
    }
    
    // Try text format (December 3-9, 2025)
    match = dateRangeStr.match(/(\w+)\s+(\d+)-(\d+),\s+(\d+)/);
    if (match) {
      const [, month, startDay, endDay, year] = match;
      const monthIndex = new Date(`${month} 1, ${year}`).getMonth();
      const startDate = new Date(parseInt(year), monthIndex, parseInt(startDay));
      const endDate = new Date(parseInt(year), monthIndex, parseInt(endDay));
      return today >= startDate && today <= endDate;
    }
    
    return false;
  } catch (error) {
    console.error('Error parsing date range:', error);
    return false;
  }
}

serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders });
  }

  // Check rate limit
  const rateLimitResult = checkRateLimit(req, RATE_LIMIT_CONFIG);
  if (!rateLimitResult.allowed) {
    console.warn("Rate limit exceeded for chat-deals");
    return createRateLimitResponse(rateLimitResult, corsHeaders);
  }

  try {
    const { messages } = await req.json();
    const OPENAI_API_KEY = Deno.env.get("OPENAI_API_KEY");
    
    if (!OPENAI_API_KEY) {
      throw new Error("OPENAI_API_KEY is not configured");
    }

    // Current week deals data (December 3-9, 2025)
    // NOTE: Update this section whenever weekly deals are updated in src/data/dealsData.ts
    const currentDeals = `🛒 VONS
Valid 12/03-12/09/2025
🏆 Boneless Skinless Chicken Breast - $1.77/lb (Cheaper than Costco)
Tri-Tip Roast - $5.99/lb
🏆 Coke, Topo Chico, and Dasani - Buy 2 Get 3 Free
Signature Select Blueberries (18 oz) - $3.99 (Cheapest I've ever seen anywhere)
Large Honeycrisp - $1.99/lb (Member price)
🏆 Lay's/Doritos/Tostitos/Kettle Chips - Buy 2 Get 2 Free
Signature Select Pasta - $0.77
Medium Ripe Hass Avocados - 2/$3 (Member price)
Fresh 90% Lean Ground Beef - $6.99/lb (Member price, sold by the package)
Tide Laundry Detergent and Pods - $9.99
$5 Fridays: Shrimp $5/lb, Medium Ripe Hass avocados 5 for $5, Rana filled pasta and sauce $5, Fresh sushi $5, Flower Bouquet $5 and more

🛒 SMART & FINAL
Valid 12/03-12/09/2025
🏆 Small Hass Avocados - 2 for $1
Tri-Tip - $5.39/lb
🏆 Boneless Skinless Chicken Breasts - $1.99/lb
Mangos - $0.99 ea
First Street Ground Beef 73% Lean - $3.99/lb
🏆 Cucumbers - 2 for $0.98
Assorted Pork Loin Chops Bone-In - $2.99/lb
Blueberries 6 oz - $1.99 ea
Fritos or Cheetos Chips 7-9.25 oz - $2.49 ea (Must buy 4)
Fresh Chicken Wings - $2.69/lb
Tide Pods and Bounce or Gain Dryer Sheets - $4.99

🛒 RALPHS
Valid 12/03-12/09/2025
🏆 All Foster Farms Fresh Chicken - BOGO Free
🏆 Pork Chops, Roasts or Ribs - 50% OFF
Coke and Pepsi 24 Packs - $9.99 ($4.99/12 pack cheaper than Costco)
Red, Green, or Black Seedless Grapes - $1.99/lb
Boneless Beef Chuck Roasts - $5.99/lb
🏆 Doritos/Tostitos/Ruffles - Buy 2 Get 2 Free
Dave's Killer Bread - $4.99
🏆 Tillamook Cheese - BOGO Free
Carbone Pasta Sauce (24 oz) - $5.99
Fresh Atlantic Salmon Full Fillets - $8.99/lb (Sat-Monday only)
Haagen Dazs Ice Cream - $2.99
Cuties Mandarins 5 lb bag - $4.99
Arm & Hammer Laundry Detergent - Buy 1 Get 1 Free

🛒 SPROUTS
Valid 12/03-12/09/2025
🏆 Sprouts Boneless Chicken Breasts - BOGO 50% OFF (Value Pack)
🏆 Organic Black, Blue, or Raspberries - BOGO 50% OFF (5.6-11oz containers)
Rao's Sauce, Soup or Pasta - Buy 1 Get 1 50% Off
🏆 Organic Medium Hass Avocados 5ct Bag - $5.98/ea
🏆 Premium Angus Boneless NY Strip Steak - $12.99/lb (Choice Beef)
Sprouts Marinated Chicken Wings - $4.99/lb
Taylor Farms Chopped Salad Kits - BOGO 50% OFF (10.65-13.2oz)
Ready-Made Sandwiches - $4.99
Panera Bread Soup - Buy 1 Get 1 50% Off
Siete Cookies, Tortilla or Kettle Chips - 2 for $7

🛒 ALDI
Valid 12/03-12/09/2025
🏆 Mangos - $0.79 ea
🏆 Organic Grass-Fed 93/7 Ground Beef - $5.75/lb
🏆 Cabbage - $0.69/lb
Blueberries pint - $2.49
Whole White Mushrooms 8oz - $1.49
Fresh Atlantic Salmon - $9.99/lb
Granny Smith Apples and Bartlett Pears - $2.99 for a 3lb bag
Granulated Sugar 4lb bag - $2.89

🛒 GROCERY OUTLET
Valid 12/03-12/09/2025 (Burbank location)
Boneless Chicken Thighs - $2.99/lb
12 Ct Large Eggs - $1.99
73/27 Ground Beef - $4.99/lb
🏆 Cosmic Crisp Apples 5lb - $3.99
🏆 Navel Oranges 8lb - $3.99
Blueberries 18oz - $3.99
Poppi 4 pack (80% Off) - $1.99
Momofuku Noodles - 57% Off at $5.99
Pilgrims Korean BBQ Wings 3lb - 68% Off at $8.99
Therabreath Mouth Wash - 62% off at $4.99

🛒 WHOLE FOODS
Valid 12/03-12/09/2025 - All deals require Prime membership
🏆 Spiral-Cut Ham - 30% OFF with Prime
Rao's Pasta Sauces 24oz - $5.99 ea with Prime ($6.66 ea without, reg $9.69)
🏆 St. Louis-Style Pork Spareribs - 30% OFF with Prime
4ct Hass Avocados - $4.99
🏆 Wild Caught Alaskan Sockeye Salmon Fillets - 20% OFF with Prime
BBQ, Buffalo, or Sweet Chili Chicken Wings - 20% Off
Chicken Soup - 20% off

🛒 AMAZON FRESH
Valid 12/03-12/09/2025
🏆 Fresh 80/20 Ground Beef - $4.99/lb
🏆 Pomegranates - $2.99 each
Just Bare Chicken Breast Fillets - $3.99/lb
Wonderful Halos Mandarins 3lb Bag - $3.99 per bag
Russet Potatoes 5lb Bag - $2.79 per bag
Tyson Boneless Chicken Thighs 2.5lb Pack - $6.39
Holiday Wines including Josh - $11.29
Glad Cling'n Seal Wrap 200 sq ft roll - $3.49

🛒 EL SUPER
Valid 12/03-12/09/2025
Small Avocados - 3 for $0.99 (every day)
🍇 FRUIT WEDNESDAY SPECIAL:
Large Mangoes - $0.79 ea
White Onions - 3lb for $0.99
Bananas - 2lb for $0.99
🏆 Fresh White Corn - 2 for $1

🥩 MEAT THURSDAY SPECIAL:
🏆 Chicken Drumsticks - $0.57/lb (Incredible price!)
Boneless Beef Stir Fry - $4.99/lb
🏆 Boneless Skinless Chicken Thighs - $2.47/lb (Regular or Marinated)
Fresh Chicken Wings - $2.47/lb
Fresh Pork Belly - $4.49/lb

🛒 STATER BROS
Valid 12/03-12/09/2025
🏆 Boneless Skinless Chicken Breasts - $1.99/lb
USDA Choice Bone-In New York Steak - $8.99/lb
USDA Choice Boneless Beef Chuck Shoulder Roast or Steak - $4.99/lb
Pepsi 12 packs - $4.99
Coke 12-packs - $5.99
Kellogg's Cereal - $2.99
5lb bag of Red or Gold Potatoes - $3.99
Mix & Match Cheetos, Fritos or Smartfood - $2.49 ea (6.25 lb, When you buy 4)

🛒 VALLARTA
Valid 12/03-12/09/2025
🏆 Medium Avocado - 5 for $2 (40¢ each!)
Fresh Diced and Marinated Beef Carne Asada Taco Meat - $5.99/lb
Guerrero Corn Tortillas 80 ct - $2.99 ea
Fresh Boneless Skinless Chicken Breast - $2.99/lb
🏆 Fresh White Corn - 2 for $1
18 ct Grade AA White Eggs - $3.99

🛒 SUPERIOR GROCERS
Valid 12/03-12/09/2025
Large Avocados - 4 for $5
Boneless Chicken Breast & Thighs - $2.99/lb
18 Ct Large Eggs - $2.99
🏆 Large Mangoes - $0.79 ea (RED TAG SPECIAL!)
Beef Taco Meat - $5.49/lb (RED TAG SPECIAL!)
Pepsi 12-packs - $4.99
🏆 Pork Loin Chops - $2.99/lb (RED TAG SPECIAL!)
Cacique Queso Fresco or Crema - 2 for $5
🏆 Peruvian Beans Bulk - $0.79/lb (RED TAG SPECIAL!)`;

    // Check if today falls within the current deals date range
    const dateRange = "12/03-12/09/2025";
    const isCurrentWeek = isDateInRange(dateRange);
    
    if (!isCurrentWeek) {
      console.warn(`Today's date is not within the current deals range (${dateRange}). Using available deals data anyway.`);
    }

    const dealsContext = `You are a helpful shopping assistant for a grocery deals website. You help users find the best grocery deals and answer questions about weekly grocery offers.

Current Weekly Deals (Valid ${dateRange}):

${currentDeals}

CRITICAL FORMATTING RULES - You MUST follow these:
- Always format store name in **bold** followed by ":"
- Include price and unit clearly (e.g., "$3.99/lb")
- Keep ALL answers under 100 words
- Limit responses to the 3 LOWEST priced stores only
- If the user asks for "the lowest price store" or "cheapest", provide ALL stores that have the lowest price (multiple if they are tied at the same price)
- When a deal is marked as 🏆, mention it as a highlighted offer

Help users by:
1. Comparing prices across stores and showing the top 3 lowest
2. Identifying the absolute cheapest option when asked
3. Highlighting top deals (marked with 🏆)
4. Mentioning special days (like $5 Fridays at Vons, Fruit Wednesday at El Super, Meat Thursday at El Super, RED TAG SPECIALS at Superior Grocers)

Keep responses friendly, concise, and UNDER 100 WORDS.`;

    const response = await fetch("https://api.openai.com/v1/chat/completions", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${OPENAI_API_KEY}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model: "gpt-5-mini-2025-08-07",
        messages: [
          { role: "system", content: dealsContext },
          ...messages,
        ],
      }),
    });

    if (!response.ok) {
      if (response.status === 429) {
        return new Response(
          JSON.stringify({ error: "Rate limit exceeded. Please try again later." }),
          { status: 429, headers: { ...corsHeaders, "Content-Type": "application/json" } }
        );
      }
      if (response.status === 402) {
        return new Response(
          JSON.stringify({ error: "Payment required. Please add credits to your workspace." }),
          { status: 402, headers: { ...corsHeaders, "Content-Type": "application/json" } }
        );
      }
      
      const errorText = await response.text();
      console.error("AI gateway error:", response.status, errorText);
      return new Response(
        JSON.stringify({ error: "AI service error" }),
        { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    const data = await response.json();
    const assistantMessage = data.choices[0].message.content;

    return new Response(
      JSON.stringify({ message: assistantMessage }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  } catch (error) {
    console.error("Error in chat-deals function:", error);
    return new Response(
      JSON.stringify({ error: error instanceof Error ? error.message : "Unknown error" }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  }
});
