/**
 * src/enrichmentCore.ts
 *
 * Single portable enrichment module — works in Node.js and Deno.
 * No Node-specific imports, no npm packages that don't work in Deno.
 * Only external import: @supabase/supabase-js (for loadEnrichmentContext).
 *
 * Exports:
 *   enrichRow()              — process one raw product row deterministically
 *   loadEnrichmentContext()  — fetch brand data from Supabase, build in-memory maps
 */

import type { SupabaseClient } from "https://esm.sh/@supabase/supabase-js@2";

// ============================================================================
// TYPES
// ============================================================================

/** The raw row as it comes from flyer_deals or test_flyer_deals_duplicate */
export interface RawRow {
  id: unknown;
  created_at?: string;
  retailer?: string | null;
  zip_code?: string | null;
  product_name: string | null;
  product_price?: number | null;
  image_link?: string | null;
  product_size?: string | null;
  product_description?: string | null;
  retailer_address?: string | null;
  coupon_detail?: string | null;
  distance_miles?: number | null;
  user_address?: string | null;
  instacart_retailer_key?: string | null;
  retailer_key?: string | null;
  store_name?: string | null;
}

/** Fully enriched row — column names match test_flyer_deals_duplicate exactly */
export interface EnrichedRow {
  // Pass-through raw columns
  id: unknown;
  product_name: string | null;
  product_price: number | null;
  product_size: string | null;
  retailer: string | null;

  // Enriched columns
  category: string;
  brand: string | null;
  is_store_brand: boolean;
  is_organic: boolean;

  // Size columns
  display_size: string | null;
  base_amount: number | null;
  base_unit: string | null;
  price_per_unit: number | null;
  size: number | null;      // per-item size (rawSize from ParsedSize)
  count: number | null;     // multiplier (rawCount from ParsedSize)
  unit: string | null;      // cleaned unit string (rawUnit from ParsedSize)

  // Confidence / source metadata
  brand_confidence: number | null;
  category_confidence: number | null;
  brand_source: string;
  category_source: string;
  size_source: string;
  size_confidence: number | null;

  // Brand flags
  is_brand: boolean | null;
  canonical_product_name: string | null;
  brand_category_match: boolean | null;

  // Timestamps & flags
  date_added: string;
  size_flag: boolean;
  category_flag: boolean;
  category_conflicts: boolean;

  // Signals for the caller — NOT stored in DB, strip before upsert
  _needsAIBrand: boolean;
  _needsAICategory: boolean;
  _needsAISize: boolean;
  _tentativeGenericMatch: { brand: string } | null;
}

/** Input entry for building the brand lookup */
export interface BrandEntry {
  brandName: string;
  isBrand: boolean;
}

/** In-memory brand lookup — built once, used for every row */
export interface BrandLookup {
  brandMap: Map<string, string>;
  isBrandFlags: Map<string, boolean>;
  sortedRealBrands: string[];
  sortedGenericNames: string[];
}

/** Everything enrichRow() needs — loaded once by loadEnrichmentContext() */
export interface EnrichmentContext {
  brandLookup: BrandLookup;
  brandCategoryMap: Map<string, string[] | null>;
}

/** Result from extractBrand() */
export interface BrandMatchResult {
  brand: string;
  isBrand: boolean;
  needsAI: boolean;
  tentativeGenericMatch: { brand: string } | null;
}

/** Parsed size from parseSize() */
export interface ParsedSize {
  amount: number;
  unit: string;
  type: MeasureType;
  rawSize: number;
  rawCount: number;
  rawUnit: string;
}

/** Normalized size from normalizeSize() */
export interface NormalizedSize {
  displaySize: string;
  baseAmount: number;
  baseUnit: string;
  pricePerUnit: number | null;
}

type MeasureType = "mass" | "volume" | "count";

// ============================================================================
// INLINE UNIT CONVERSIONS (replaces convert-units npm package)
// All volume values are relative to fl-oz; all mass values relative to oz.
// ============================================================================

const MASS_CONVERSIONS: Record<string, number> = {
  oz: 1,
  lb: 16,       // 1 lb = 16 oz
  g: 0.035274,  // 1 g  = 0.035274 oz
  kg: 35.274,   // 1 kg = 35.274 oz
  mg: 0.000035274,
};

const VOLUME_CONVERSIONS: Record<string, number> = {
  "fl-oz": 1,
  ml: 0.033814,   // 1 ml    = 0.033814 fl-oz
  l: 33.814,      // 1 l     = 33.814 fl-oz
  gal: 128,       // 1 gal   = 128 fl-oz
  pnt: 16,        // 1 pint  = 16 fl-oz  (convert-units uses "pnt")
  qt: 32,         // 1 qt    = 32 fl-oz
  cup: 8,         // 1 cup   = 8 fl-oz
  tsp: 0.166667,  // 1 tsp   = 1/6 fl-oz
  Tbs: 0.5,       // 1 Tbs   = 0.5 fl-oz
};

const MASS_UNITS = new Set(Object.keys(MASS_CONVERSIONS));
const VOLUME_UNITS = new Set(Object.keys(VOLUME_CONVERSIONS));

function convertUnit(value: number, fromUnit: string, toUnit: string): number {
  if (fromUnit === toUnit) return value;
  if (MASS_CONVERSIONS[fromUnit] !== undefined && MASS_CONVERSIONS[toUnit] !== undefined) {
    return value * (MASS_CONVERSIONS[fromUnit] / MASS_CONVERSIONS[toUnit]);
  }
  if (VOLUME_CONVERSIONS[fromUnit] !== undefined && VOLUME_CONVERSIONS[toUnit] !== undefined) {
    return value * (VOLUME_CONVERSIONS[fromUnit] / VOLUME_CONVERSIONS[toUnit]);
  }
  return value; // units not in same family — return as-is
}

/** Pick the most readable display unit for edge-case values (replaces toBest()). */
function pickDisplayUnit(baseAmount: number, baseUnit: string): { val: number; unit: string } {
  if (baseUnit === "oz" && baseAmount > 32) {
    return { val: round(baseAmount / 16, 2), unit: "lb" };
  }
  if (baseUnit === "lb" && baseAmount < 0.1) {
    return { val: round(baseAmount * 16, 2), unit: "oz" };
  }
  return { val: baseAmount, unit: baseUnit };
}

export function measureType(unit: string): MeasureType {
  if (MASS_UNITS.has(unit)) return "mass";
  if (VOLUME_UNITS.has(unit)) return "volume";
  return "count";
}

// ============================================================================
// TEXT UTILITIES (from brand-and-category/textUtils.ts)
// ============================================================================

function normalizeText(text: string): string {
  return text
    .normalize("NFKD")
    .toLowerCase()
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/&(?:amp|#38);/g, " and ")
    .replace(/&(?:nbsp|#160);/g, " ")
    .replace(/&(?:apos|#39);/g, "")
    .replace(/&/g, " and ")
    .replace(/['’]/g, "")
    .replace(/[^a-z0-9\s]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function tokenize(text: string): string[] {
  return normalizeText(text).split(" ").filter(Boolean);
}

function hasToken(tokens: string[], token: string): boolean {
  return tokens.includes(token);
}

function hasAnyToken(tokens: string[], candidates: string[]): boolean {
  return candidates.some((c) => tokens.includes(c));
}

const phraseRegexCache = new Map<string, RegExp>();

function getPhraseRegex(normalizedPhrase: string): RegExp {
  let re = phraseRegexCache.get(normalizedPhrase);
  if (!re) {
    const escaped = normalizedPhrase.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    re = new RegExp(`(?:^|\\s)${escaped}(?:\\s|$)`);
    phraseRegexCache.set(normalizedPhrase, re);
  }
  return re;
}

function hasPhrase(normalizedText: string, phrase: string): boolean {
  const np = normalizeText(phrase);
  return getPhraseRegex(np).test(normalizedText);
}

function hasPhraseNormalized(normalizedText: string, normalizedPhrase: string): boolean {
  return getPhraseRegex(normalizedPhrase).test(normalizedText);
}

function hasAnyPhrase(normalizedText: string, phrases: string[]): boolean {
  return phrases.some((p) => hasPhrase(normalizedText, p));
}

// ============================================================================
// PRODUCT CATEGORIZER (from brand-and-category/productCategorizer.ts)
// ============================================================================

export enum ProductCategory {
  PRODUCE = "PRODUCE",
  MEAT = "MEAT",
  SEAFOOD = "SEAFOOD",
  DAIRY = "DAIRY",
  EGGS = "EGGS",
  ALT_MILK = "ALT_MILK",
  DELI_PREPARED = "DELI_PREPARED",
  BAKERY = "BAKERY",
  SNACKS = "SNACKS",
  PANTRY = "PANTRY",
  DESSERT = "DESSERT",
  BEVERAGES = "BEVERAGES",
  FROZEN = "FROZEN",
  HOUSEHOLD = "HOUSEHOLD",
  HEALTH_BEAUTY = "HEALTH_BEAUTY",
  PET = "PET",
  BABY = "BABY",
  ALCOHOL = "ALCOHOL",
  FLORAL = "FLORAL",
  CATEGORY_UNCERTAIN = "CATEGORY_UNCERTAIN",
}

// --- Pre-check overrides ---

function precheckCannedPantry(tokens: string[], norm: string): boolean {
  // Explicit frozen products should continue to the FROZEN detector.
  if (hasToken(tokens, "frozen")) return false;
  const produceWords = [
    "spinach","carrots","carrot","potatoes","potato","corn","peas",
    "beans","beets","beet","tomatoes","tomato","vegetables","veggies",
    "mushrooms","mushroom","artichoke","artichokes","asparagus",
    "cucumbers","cucumber","peppers","pepper","onions","onion","pineapple","peach","peaches","pear","pears","apricot","apricots","mandarin","mandarins","orange","oranges","cherry","cherries","fruit"
  ];
  const producePhrasesArr = ["green beans","mixed vegetables"];
  const hasProduceWord = hasAnyToken(tokens, produceWords) || hasAnyPhrase(norm, producePhrasesArr);
  if (hasToken(tokens, "fresh")) return false;
  if (hasAnyToken(tokens, ["canned"]) || hasPhrase(norm, "shelf stable")) {
    if (hasProduceWord) return true;
  }
  if (
    hasAnyPhrase(norm, [
      "in juice",
      "in fruit juice",
      "in 100",
      "in syrup",
      "in light syrup",
      "in heavy syrup"
    ]) &&
    hasProduceWord
  ) return true;
  if (hasToken(tokens, "pickled")) return true;
  if (hasToken(tokens, "seasoned") && hasProduceWord) return true;
  if (hasPhrase(norm, "pieces and stems")) return true;
  if (/\(\d+\s*pack\)|\(pack\s+of\s+\d+\)/i.test(norm) && hasProduceWord) return true;
  if (hasToken(tokens, "chopped") && hasProduceWord && /\d+(\.\d+)?\s*oz/i.test(norm)) return true;
  if (hasAnyToken(tokens, ["cans"]) && hasProduceWord) return true;
  if (hasToken(tokens, "can") && hasProduceWord && /\d+\s*oz/i.test(norm)) return true;
  return false;
}

function precheckSpicePantry(tokens: string[], norm: string): boolean {
  // Explicit frozen products should continue to the FROZEN detector.
  if (hasToken(tokens, "frozen")) return false;
  // Match processed forms only when a known herb or spice is present.
  const processedForms = ["powder","granules","granulated","flakes","dehydrated","crystallized","seasoning","spice","ground","crushed"];
  const processedPhrases = ["garlic paste","minced garlic","garlic salt","onion powder","onion salt"];
  const produceWords = [
    "garlic","onion","onions","lime","lemon","pepper","peppers",
    "ginger","cilantro","parsley","basil","dill","rosemary","thyme",
    "sage","mint","chive","chives","turmeric","cumin","paprika",
    "oregano","cinnamon",
  ];
  if (!hasAnyToken(tokens, produceWords)) return false;
  if (hasToken(tokens, "dried") && !hasPhrase(norm, "freeze dried") && !hasPhrase(norm, "sun dried tomatoes")) return true;
  if (hasAnyToken(tokens, processedForms)) return true;
  if (hasAnyPhrase(norm, processedPhrases)) return true;
  return false;
}

function precheckNonFood(norm: string): boolean {
  return /\b(paperback|hardcover|kindle|isbn|board book)\b/.test(norm);
}

function precheckFrozenBrands(tokens: string[], norm: string): boolean {
  const exclusivelyFrozen = ["tai pei","ling ling","bibigo","crazy cuizine","smart ones"];
  if (hasAnyPhrase(norm, exclusivelyFrozen)) return true;
  if (/\bp\.?f\.?\s*chang/i.test(norm)) return true;
  const mealKeywords = ["bowl","entree","dinner","meal","pizza","pot pie","burrito"];
  if (hasPhrase(norm, "amys") || hasPhrase(norm, "amy s")) {
    if (hasAnyToken(tokens, mealKeywords) || hasAnyPhrase(norm, mealKeywords)) return true;
  }
  if (hasPhrase(norm, "marie callenders") || hasPhrase(norm, "marie callender s")) {
    if (hasAnyToken(tokens, mealKeywords) || hasAnyPhrase(norm, mealKeywords)) return true;
  }
  return false;
}

const COFFEE_TERMS = ["coffee","espresso","arabica","decaf","keurig","k cup","k cups","k pod","k pods","nespresso"];
const COFFEE_ORIGIN_TERMS = ["colombian","sumatra","guatemala","ethiopian","brazilian","costa rican","kenyan","jamaican","hawaiian kona"];

function precheckCoffee(tokens: string[], norm: string): boolean {
  if (hasAnyToken(tokens, ["creamer","topping"])) return false;
  if (hasAnyToken(tokens, COFFEE_TERMS)) return true;
  if (hasAnyPhrase(norm, COFFEE_TERMS)) return true;
  if (hasAnyPhrase(norm, COFFEE_ORIGIN_TERMS)) {
    if (hasAnyToken(tokens, ["roast","blend","ground"])) return true;
  }
  if (hasAnyPhrase(norm, ["ground coffee","roast coffee","coffee roast","coffee blend"])) return true;
  return false;
}

const BABY_BRANDS = ["little journey","happy baby","happy tot","gerber","beech nut","earths best","plum organics","ella s kitchen"];
const BABY_PRODUCT_TERMS = ["puree","purees","formula","rusk","rusks","teething","infant","toddler","puffs","mum mum","sippy","pacifier","diaper","diapers","onesie","bib"];

function precheckBabyGuard(tokens: string[], norm: string): boolean {
  if (!hasToken(tokens, "baby")) return false;
  if (hasAnyPhrase(norm, BABY_BRANDS)) return false;
  if (hasAnyToken(tokens, BABY_PRODUCT_TERMS)) return false;
  if (hasAnyPhrase(norm, ["baby food","baby formula","baby cereal","baby yogurt","baby puffs"])) return false;
  if (/\d+\+?\s*months/.test(norm)) return false;
  const descriptiveContext = [
    "carrots","carrot","spinach","kale","arugula","lettuce","peppers","corn","potatoes",
    "bella","bellas","mushrooms","mushroom","bananas","banana","tomatoes","tomato",
    "pickles","pickle","dill","whole","swiss","cheddar","back","ribs","shrimp","lobster",
  ];
  if (hasAnyToken(tokens, descriptiveContext)) return true;
  return false;
}

function precheckBabyYogurt(tokens: string[], norm: string): boolean {
  const yogurtTerms = hasAnyToken(tokens, ["yogurt","yoghurt","yogurt bites"]) || hasPhrase(norm, "yogurt bites");
  if (!yogurtTerms) return false;
  if (hasAnyPhrase(norm, BABY_BRANDS)) return true;
  if (hasAnyPhrase(norm, ["baby food","baby yogurt"])) return true;
  if (hasToken(tokens, "bites") && hasAnyToken(tokens, ["gerber","infant","toddler"])) return true;
  return false;
}

function precheckShrimp(tokens: string[], norm: string): boolean {
  if (!hasAnyToken(tokens, ["shrimp","prawns"])) return false;

  // Explicit frozen shrimp should continue to the FROZEN detector.
  if (hasToken(tokens, "frozen")) return false;

  if (hasAnyToken(tokens, ["sauce","butter","garlic","scampi","cocktail"])) return true;
  return false;
}

function precheckPreparedProduce(tokens: string[], norm: string): ProductCategory | null {
  if (hasToken(tokens, "quiche") || hasPhrase(norm, "quiche")) {
    if (hasToken(tokens, "frozen")) return ProductCategory.FROZEN;
    return ProductCategory.DELI_PREPARED;
  }
  if (hasPhrase(norm, "stuffed mushroom") || hasPhrase(norm, "stuffed mushrooms")) {
    if (hasToken(tokens, "frozen")) return ProductCategory.FROZEN;
    return ProductCategory.DELI_PREPARED;
  }
  if (hasToken(tokens, "florentine") && hasAnyToken(tokens, ["quiche","egg","spinach"])) {
    if (hasToken(tokens, "frozen")) return ProductCategory.FROZEN;
    return ProductCategory.DELI_PREPARED;
  }
  return null;
}

const SNACK_SHAPE_TERMS = ["loops","rings","puffs","puffed","crisps","chips","sticks","straws","curls"];

function precheckFlavoredSnack(tokens: string[], norm: string): boolean {
  const hasSnackShape = hasAnyToken(tokens, SNACK_SHAPE_TERMS);
  const hasSnackBase = hasAnyToken(tokens, ["lentil","chickpea","veggie","bean","rice","corn","potato"]);
  if (!hasSnackShape && !hasSnackBase) return false;
  const dairyFlavors = ["sour cream","cheddar","cheese","ranch","cream cheese","parmesan"];
  if (hasSnackShape && hasAnyPhrase(norm, dairyFlavors)) return true;
  if (hasSnackBase && hasSnackShape) return true;
  return false;
}

const SNACK_RING_BRANDS = ["funyuns","lesserevil","lesser evil","wise","herr s","utz","andy capp"];

function precheckOnionProduct(tokens: string[], norm: string): ProductCategory | null {
  const hasOnion = hasAnyToken(tokens, ["onion","onions"]);
  if (!hasOnion) return null;
  if (hasAnyPhrase(norm, ["fried onion","fried onions","french fried"])) return ProductCategory.PANTRY;
  if (hasPhrase(norm, "onion ring") || hasPhrase(norm, "onion rings")) {
    if (hasAnyToken(tokens, ["frozen","steamable"])) return ProductCategory.FROZEN;
    if (hasAnyPhrase(norm, SNACK_RING_BRANDS)) return ProductCategory.SNACKS;
    return ProductCategory.SNACKS;
  }
  return null;
}

// --- Category detectors ---

function isPet(tokens: string[], norm: string): boolean {
  const strongTokens = ["dog","cat","puppy","kitten","feline","canine","aquarium"];
  const strongPhrases = ["dog food","cat food","cat litter","dog treat","dog treats","cat treat","cat treats","pet food","flea collar","fish flakes","pet toy","pet toys","dog toy","cat toy","pet bed","dog bed","cat bed","dog bone","rawhide","catnip"];
  const brandTokens = ["purina","iams","pedigree","friskies","whiskas","beneful","alpo","kibbles","greenies","nylabone","kong"];
  const brandPhrases = ["meow mix","fancy feast","blue buffalo","rachael ray nutrish","hills science","royal canin","milk bone"];
  if (hasAnyToken(tokens, strongTokens)) return true;
  if (hasAnyPhrase(norm, strongPhrases)) return true;
  if (hasAnyToken(tokens, brandTokens)) return true;
  if (hasAnyPhrase(norm, brandPhrases)) return true;
  return false;
}

function isBaby(tokens: string[], norm: string): boolean {
  if (hasAnyToken(tokens, ["detergent","laundry","cleaner","dishwasher"])) return false;
  if (hasPhrase(norm, "dish soap")) return false;
  const strongTokens = ["diaper","diapers","pacifier","bib","teething","sippy","onesie"];
  const strongPhrases = ["baby food","baby formula","infant formula","baby wipes","baby lotion","baby shampoo","baby cereal","diaper cream","baby powder","nursing pads","baby bottle","baby oil","baby wash","toddler formula","baby puffs","baby yogurt","toddler snack","toddler snacks","stage 1","stage 2","stage 3"];
  const brandTokens = ["huggies","pampers","gerber","enfamil","similac","luvs"];
  const brandPhrases = ["honest company","beech nut","plum organics","happy baby","earths best"];
  if (hasAnyToken(tokens, strongTokens)) return true;
  if (hasAnyPhrase(norm, strongPhrases)) return true;
  if (hasAnyToken(tokens, brandTokens)) return true;
  if (hasAnyPhrase(norm, brandPhrases)) return true;
  if (/\d+\+?\s*months/.test(norm) && hasAnyToken(tokens, ["infant","toddler","baby"])) return true;
  if (hasToken(tokens, "baby")) {
    const produceExclusions = ["carrots","carrot","spinach","kale","arugula","lettuce","greens","tomatoes","tomato","peppers","corn","potatoes","bella","bellas","bananas","banana","mushrooms","mushroom"];
    const meatExclusions = ["back","ribs"];
    if (hasAnyToken(tokens, produceExclusions)) return false;
    if (hasAnyToken(tokens, meatExclusions)) return false;
    return true;
  }
  return false;
}

function isHealthBeauty(tokens: string[], norm: string): boolean {
  const strongTokens = ["shampoo","conditioner","deodorant","toothpaste","toothbrush","mouthwash","lotion","sunscreen","mascara","lipstick","eyeliner","concealer","moisturizer","serum","cleanser","toner","exfoliant","razor","razors","tampon","tampons","bandage","bandaid","ibuprofen","acetaminophen","aspirin","vitamin","vitamins","supplement","supplements","probiotic","antacid","melatonin","floss","swabs","soap","cosmetic","cosmetics","makeup","thermometer","syringe"];
  const strongPhrases = ["body wash","face wash","hand soap","body lotion","hand cream","eye cream","hair gel","hair spray","shaving cream","nail polish","makeup remover","contact lens","contact lenses","first aid","pain relief","cold medicine","allergy medicine","dental floss","cotton balls","cotton swabs","bath bomb","bubble bath","hand sanitizer","rubbing alcohol","hydrogen peroxide","petroleum jelly","lip balm","eye drops","nasal spray","cough drops","cough syrup","chest rub","epsom salt","liquid foundation","powder foundation","makeup foundation"];
  const brandTokens = ["dove","pantene","tresemme","loreal","maybelline","covergirl","neutrogena","aveeno","cetaphil","olay","gillette","tylenol","advil","colgate","listerine","benadryl","claritin","zyrtec","mucinex","nyquil","dayquil","vicks","neosporin","aquaphor","eucerin","cerave","nivea","garnier","suave","softsoap","method"];
  const brandPhrases = ["oral b","band aid","herbal essences","old spice","irish spring","secret deodorant","degree deodorant"];
  const crestOralCare =
    hasToken(tokens, "crest") &&
    (
      hasAnyToken(tokens, [
        "toothpaste","toothbrush","mouthwash","floss",
        "dental","oral","whitening","whitestrips"
      ]) ||
      hasAnyPhrase(norm, ["white strips","3d white"])
    );
  if (crestOralCare) return true;
  if (hasToken(tokens, "dove") && hasAnyToken(tokens, ["chocolate","candy","bar","promises","silk"])) return false;
  if (hasAnyPhrase(norm, ["dish soap","dish spray","dish liquid","dishwashing","dishwasher"])) return false;
  if (hasAnyToken(tokens, ["dawn","palmolive"]) && hasAnyToken(tokens, ["dish","clean","laundry"])) return false;
  if (hasPhrase(norm, "all purpose cleaner")) return false;
  if (hasAnyToken(tokens, ["vitamin","vitamins"])) {
    const foodContext = [
      "milk","juice","drink","water","cereal","smoothie","yogurt","tea"
    ];
    if (hasAnyToken(tokens, foodContext)) return false;
  }
  if (hasToken(tokens, "probiotic") || hasToken(tokens, "probiotics")) {
    const foodContext = ["yogurt","yoghurt","yoggies","snacks","fruit","water","kefir","kombucha","smoothie","shot","trail","drink","juice","bar"];
    if (hasAnyToken(tokens, foodContext)) return false;
  }
  if (hasToken(tokens, "soap") && hasAnyToken(tokens, ["dish","dishes","dishwashing"])) return false;
  if (hasAnyToken(tokens, strongTokens)) return true;
  if (hasAnyPhrase(norm, strongPhrases)) return true;
  if (hasAnyToken(tokens, brandTokens)) return true;
  if (hasAnyPhrase(norm, brandPhrases)) return true;
  return false;
}

function isHousehold(tokens: string[], norm: string): boolean {
  const strongTokens = ["detergent","bleach","sponge","sponges","mop","broom","dustpan","trash","garbage","napkins","napkin","tissues","tissue"];
  const strongPhrases = ["paper towels","paper towel","trash bags","garbage bags","laundry detergent","dish soap","dishwasher pods","dishwasher detergent","dryer sheets","fabric softener","toilet paper","paper plates","plastic wrap","aluminum foil","parchment paper","wax paper","freezer bags","storage bags","zip bags","cleaning spray","all purpose cleaner","glass cleaner","floor cleaner","disinfecting wipes","air freshener","toilet cleaner","oven cleaner","stain remover","plastic cups","plastic forks","plastic spoons","plastic knives","paper napkins","light bulb","light bulbs","trash can","garbage can"];
  const brandTokens = ["tide","gain","downy","bounty","charmin","scott","kleenex","clorox","lysol","windex","dawn","cascade","finish","swiffer","glad","hefty","ziploc","reynolds","oxiclean","febreze","glade","pledge"];
  const brandPhrases = ["mr clean","arm hammer","scrub daddy","seventh generation"];
  if (hasAnyToken(tokens, strongTokens)) return true;
  if (hasAnyPhrase(norm, strongPhrases)) return true;
  if (hasAnyToken(tokens, brandTokens)) return true;
  if (hasAnyPhrase(norm, brandPhrases)) return true;
  if (hasAnyPhrase(norm, ["dish spray","dish liquid","dishwashing liquid","surface cleaner"])) return true;
  if (hasToken(tokens, "palmolive") && hasToken(tokens, "dish")) return true;
  return false;
}

function isFloral(tokens: string[], norm: string): boolean {
  const strongTokens = ["bouquet","bouquets","roses","tulips","lilies","daisies","orchid","orchids","carnations","sunflowers","mums","poinsettia","succulent","succulents","arrangement"];
  const strongPhrases = ["flower arrangement","fresh flowers","potted plant","potted plants","floral arrangement","flower bouquet","dozen roses"];
  if (hasToken(tokens, "flowers")) return true;
  if (hasAnyToken(tokens, strongTokens)) return true;
  if (hasAnyPhrase(norm, strongPhrases)) return true;
  return false;
}

function isAlcohol(tokens: string[], norm: string): boolean {
  const strongTokens = ["beer","wine","champagne","vodka","rum","tequila","whiskey","whisky","bourbon","gin","brandy","cognac","mezcal","sake","soju","lager","ale","stout","ipa","merlot","chardonnay","cabernet","pinot","prosecco","malbec","moscato","sangria","seltzer"];
  const strongPhrases = ["hard seltzer","craft beer","red wine","white wine","sparkling wine","light beer","irish cream","wine cooler","hard cider","hard lemonade","rose wine","rosé wine"];
  const brandTokens = ["budweiser","coors","corona","modelo","heineken","stella","guinness","michelob","lagunitas","truly","barefoot","yellowtail","smirnoff","absolut","bacardi","fireball","kahlua","jagermeister","tanqueray","hendricks"];
  const brandPhrases = ["bud light","blue moon","sierra nevada","sam adams","white claw","josh cellars","robert mondavi","grey goose","captain morgan","jack daniels","crown royal","don julio","jose cuervo","makers mark","johnnie walker","bombay sapphire","titos vodka","patron tequila"];
  if (hasToken(tokens, "beer") && hasAnyToken(tokens, ["root","ginger","birch","near"])) return false;
  if (hasAnyPhrase(norm, ["non alcoholic beer","na beer","alcohol free beer","0 0 beer","non alcoholic"])) return false;
  if (hasToken(tokens, "ale") && hasToken(tokens, "ginger")) return false;
  if (hasPhrase(norm, "ginger ale")) return false;
  if (hasToken(tokens, "cider") && !hasToken(tokens, "hard")) {
    // fall to BEVERAGES
  } else if (hasToken(tokens, "cider") && hasToken(tokens, "hard")) {
    return true;
  }
  if (hasToken(tokens, "seltzer")) {
    if (hasToken(tokens, "hard") || hasAnyToken(tokens, brandTokens) || hasAnyPhrase(norm, brandPhrases)) return true;
    return false;
  }
  if (hasAnyToken(tokens, strongTokens.filter(t => t !== "seltzer"))) return true;
  if (hasAnyPhrase(norm, strongPhrases)) return true;
  if (hasAnyToken(tokens, brandTokens)) return true;
  if (hasAnyPhrase(norm, brandPhrases)) return true;
  return false;
}

function isFrozen(tokens: string[], norm: string): boolean {
  if (hasToken(tokens, "frozen")) return true;
  const strongPhrases = ["frozen pizza","frozen dinner","frozen meal","frozen vegetables","frozen veggies","frozen fruit","frozen entree","frozen appetizer","frozen breakfast","frozen burrito","frozen waffle","frozen fries","tv dinner","pizza rolls","fish sticks","frozen nuggets","frozen patties","frozen shrimp","ice pops","pot pie","popcorn shrimp","breaded shrimp","coconut shrimp","chicken nugget","chicken nuggets","breaded chicken nugget","breaded chicken nuggets","corn dog","corn dogs","egg roll","egg rolls","breakfast sandwich","breakfast sandwiches"];
  const brandPhrases = ["lean cuisine","hot pocket","hot pockets","smart ones","healthy choice","marie callenders","birds eye"];
  const brandTokens = ["stouffers","banquet","totinos","digiorno","tombstone","eggo"];
  if (hasToken(tokens, "eggo") && hasAnyToken(tokens, ["waffles","waffle","pancakes"])) return true;
  if (hasToken(tokens, "digiorno") && hasAnyToken(tokens, ["pizza","rising"])) return true;
  if (hasAnyPhrase(norm, ["dinosaur shaped","dino shaped","dino buddies"])) return true;
  if (hasAnyToken(tokens, ["yangs","innovasian"]) && hasAnyToken(tokens, ["chicken","orange","mandarin","beef","shrimp"])) return true;
  if (hasPhrase(norm, "p f changs") || hasPhrase(norm, "pf changs")) return true;
  if (hasAnyPhrase(norm, strongPhrases)) return true;
  if (hasAnyPhrase(norm, brandPhrases)) return true;
  if (hasAnyToken(tokens, brandTokens)) return true;
  return false;
}

function isDeliPrepared(tokens: string[], norm: string): boolean {
  const strongTokens = ["rotisserie","deli","hummus","sushi"];
  const strongPhrases = ["deli meat","deli sliced","cold cuts","lunch meat","lunchmeat","prepared meal","ready to eat","hot bar","salad bar","deli cheese","deli sandwich","fried chicken","potato salad","macaroni salad","coleslaw","cole slaw","chicken tenders","chicken strips","meatloaf","meal kit","rotisserie chicken","charcuterie","california roll","dragon roll","summer roll","cooked sushi","spicy shrimp roll","crunchy roll","with white rice","chicken salad"];
  const brandTokens = ["hillshire","buddig","columbus"];
  const brandPhrases = ["boars head","oscar mayer","land o frost"];
  if (hasToken(tokens, "deli") && hasAnyToken(tokens, ["cheese"]) &&
    !hasAnyToken(tokens, ["turkey","ham","roast","beef","salami","bologna","sandwich","platter","rotisserie","meat"])) {
    return false;
  }
  if (hasPhrase(norm, "sushi rice")) return false;
  const deliMeatTokens = [
    "ham","turkey","chicken","beef","salami","bologna"
  ];

  if (
    hasAnyToken(tokens, ["sliced","slices"]) &&
    (hasPhrase(norm, "fully cooked") || hasToken(tokens, "refrigerated")) &&
    hasAnyToken(tokens, deliMeatTokens)
  ) return true;
  if (
    hasAnyToken(tokens, ["fajita","fajitas"]) &&
    hasToken(tokens, "strips") &&
    hasAnyToken(tokens, ["refrigerated","cooked","prepared"])
  ) return true;
  if (hasAnyToken(tokens, strongTokens)) return true;
  if (hasAnyPhrase(norm, strongPhrases)) return true;
  if (hasAnyToken(tokens, brandTokens)) return true;
  if (hasAnyPhrase(norm, brandPhrases)) return true;
  if (hasAnyToken(tokens, ["sub","subs"]) && hasAnyToken(tokens, ["sandwich","italian","turkey","ham","club","hoagie"])) return true;
  return false;
}

function isDessert(tokens: string[], norm: string): boolean {
  const strongTokens = ["cake","cupcake","cupcakes","pie","brownie","brownies","cookie","cookies","donut","donuts","doughnut","doughnuts","pastry","pastries","muffin","muffins","cheesecake","tiramisu","gelato","fudge","truffle","truffles","pudding","cobbler","tart","tarts","eclair"];
  const strongPhrases = ["ice cream","ice pop","ice pops","popsicle","fudge bar","frozen yogurt","pudding cup","snack cake","layer cake","bundt cake","pound cake","sheet cake","cookie dough"];
  const brandTokens = ["talenti","magnum","klondike","drumstick","outshine","hostess","entenmanns"];
  const brandPhrases = ["haagen dazs","ben jerrys","ben jerry s","ben and jerrys","good humor","little debbie","sara lee","pepperidge farm","chips ahoy","nutter butter"];
  if (hasToken(tokens, "mix")) return false;
  if (hasPhrase(norm, "pot pie")) return false;
  if (hasToken(tokens, "pie") && hasAnyToken(tokens, ["crust","shell","filling"])) return false;
  const savoryBakeryWords = ["bread","bun","buns","hamburger","sandwich","bagel","bagels","hot dog","toast"];
  if (hasAnyToken(tokens, savoryBakeryWords)) return false;
  if (hasToken(tokens, "muffin") && hasToken(tokens, "english")) return false;
  if (hasAnyToken(tokens, ["rolls","roll"]) && hasAnyToken(tokens, ["dinner","kaiser","slider","hoagie"])) return false;
  if (hasToken(tokens, "zbar")) return false;
  if (
    hasAnyToken(tokens, ["bar","bars"]) &&
    hasAnyToken(tokens, [
      "granola","protein","energy","snack","nutrition","cereal"
    ])
  ) return false;
  if (hasAnyToken(tokens, strongTokens)) return true;
  if (hasAnyPhrase(norm, strongPhrases)) return true;
  if (hasAnyToken(tokens, brandTokens)) return true;
  if (hasAnyPhrase(norm, brandPhrases)) return true;
  return false;
}

function isBakery(tokens: string[], norm: string): boolean {
  if (hasAnyPhrase(norm, [
    "bread flour",
    "bread mix",
    "quick bread mix"
  ])) return false;
  const strongTokens = ["bread","loaf","bagel","bagels","bun","buns","croissant","croissants","tortilla","tortillas","pita","naan","baguette","sourdough","flatbread","brioche","ciabatta","focaccia","cornbread","biscuit","biscuits","croutons"];
  const strongPhrases = ["hamburger buns","hot dog buns","dinner rolls","sandwich bread","whole wheat bread","white bread","french bread","italian bread","texas toast","garlic bread","banana bread","slider buns","english muffin","english muffins","hawaiian rolls"];
  const brandTokens = ["bimbo","guerrero"];
  const brandPhrases = ["daves killer bread","natures own","oroweat","kings hawaiian","la banderita","sara lee bread","wonder bread","thomas english","thomas bagels","thomas muffins"];
  if (hasAnyToken(tokens, ["tortilla","tortillas"]) && hasAnyToken(tokens, ["chip","chips"])) return false;
  if (hasAnyToken(tokens, ["pretzel","pretzels"]) && !hasAnyToken(tokens, ["bun","buns","roll","rolls","bread"])) return false;
  if (hasAnyToken(tokens, ["roll","rolls"]) && hasAnyToken(tokens, ["sushi","california","dragon","spicy","crunchy","cooked","summer"])) return false;
  if (hasAnyToken(tokens, ["roll","rolls"]) && hasPhrase(norm, "white rice")) return false;
  if (hasAnyToken(tokens, strongTokens)) return true;
  if (hasAnyPhrase(norm, strongPhrases)) return true;
  if (hasAnyToken(tokens, brandTokens)) return true;
  if (hasAnyPhrase(norm, brandPhrases)) return true;
  return false;
}

function isSnacks(tokens: string[], norm: string): boolean {
  if (hasAnyToken(tokens, ["yogurt","yoghurt","gogurt"]) || hasPhrase(norm, "go gurt")) {
    const yogurtAsModifier = hasAnyPhrase(norm, ["yogurt covered","yogurt dipped","yogurt coated"]);
    const fruitSnackContext = hasAnyPhrase(norm, ["fruit snacks","fruit snack","fruit n yogurt"]);
    if (!yogurtAsModifier && !fruitSnackContext) return false;
  }
  const strongTokens = ["chips","crisps","pretzels","pretzel","popcorn","crackers","cracker","jerky","nuts","peanuts","almonds","cashews","pistachios","pecans","walnuts","macadamia","seeds"];
  const strongPhrases = ["potato chips","tortilla chips","corn chips","pita chips","veggie chips","snack mix","trail mix","granola bar","granola bars","protein bar","protein bars","energy bar","energy bars","fruit snacks","fruit snack","cheese puffs","pork rinds","rice cakes","rice cake","fruit leather","snack pack","cheese crackers","animal crackers","graham crackers","goldfish crackers","mixed nuts","sunflower seeds","pumpkin seeds","gummy bears","gummy worms","kettle corn","cereal bar","cereal bars","nutrition bar","nutrition bars","fruit roll up","fruit roll ups","fruit rollup","fruit rollups"];
  const brandTokens = ["doritos","lays","ruffles","tostitos","cheetos","pringles","fritos","takis","goldfish","triscuit","ritz","kashi","rxbar","planters","boomchickapop","zbar"];
  const brandPhrases = ["kettle brand","cape cod","skinny pop","smartfood","cheez it","wheat thins","kind bar","clif bar","larabar","nature valley","wonderful pistachios","pop chips","popchips"];
  if (hasAnyToken(tokens, strongTokens)) return true;
  if (hasAnyPhrase(norm, strongPhrases)) return true;
  if (hasAnyToken(tokens, brandTokens)) return true;
  if (hasAnyPhrase(norm, brandPhrases)) return true;
  return false;
}

function isPantry(tokens: string[], norm: string): boolean {
  if (
    hasAnyPhrase(norm, ["bubble tea topping","boba topping"]) ||
    (hasToken(tokens, "tea") && hasAnyToken(tokens, ["topping","jelly"]))
  ) return true;
  if (hasPhrase(norm, "coffee topping")) return true;
  if (hasToken(tokens, "creamer")) return false;
  if (hasAnyToken(tokens, ["yogurt","yoghurt"]) && !hasPhrase(norm, "frozen yogurt")) return false;
  if (hasAnyToken(tokens, ["gogurt"]) || hasPhrase(norm, "go gurt")) return false;
  if (hasToken(tokens, "tea") || hasAnyPhrase(norm, ["tea bags","herbal tea","green tea","black tea","chai"])) return false;
  const strongTokens = ["soup","soups","broth","stock","bouillon","canned","ramen","noodle","noodles","pasta","spaghetti","penne","rigatoni","linguine","fettuccine","macaroni","orzo","lasagna","rice","quinoa","couscous","lentils","beans","chickpeas","cereal","oatmeal","oats","flour","sugar","oil","vinegar","syrup","honey","jam","jelly","preserves","ketchup","mustard","mayonnaise","mayo","relish","salsa","dressing","marinade","seasoning","spice","spices","baking","yeast","cornstarch","extract","vanilla","cocoa","molasses","breadcrumbs","panko","stuffing","gravy","tahini","miso","sriracha","hoisin","worcestershire"];
  const strongPhrases = ["chicken soup","chicken noodle soup","chicken broth","chicken stock","tomato soup","cream of mushroom","cream of chicken","noodle soup","cup noodle","cup noodles","instant noodle","instant noodles","ramen noodle","beef broth","vegetable broth","bone broth","canned chicken","canned tuna","canned salmon","canned beans","baked beans","refried beans","black beans","kidney beans","pinto beans","chili beans","mac and cheese","macaroni and cheese","pasta sauce","spaghetti sauce","marinara sauce","alfredo sauce","pizza sauce","enchilada sauce","taco sauce","taco seasoning","salad dressing","olive oil","vegetable oil","canola oil","coconut oil","avocado oil","sesame oil","cooking spray","apple cider vinegar","balsamic vinegar","rice vinegar","pancake mix","waffle mix","brownie mix","cake mix","muffin mix","cornbread mix","biscuit mix","bread mix","pie crust","pizza dough","taco shells","taco kit","instant rice","minute rice","jasmine rice","basmati rice","brown rice","wild rice","fried rice","boxed rice","spanish rice","white rice","peanut butter","almond butter","sunflower butter","fruit spread","maple syrup","agave nectar","tomato paste","tomato sauce","diced tomatoes","crushed tomatoes","coconut cream","condensed milk","evaporated milk","soy sauce","teriyaki sauce","barbecue sauce","bbq sauce","hot sauce","ranch dressing","stewed tomatoes","whole tomatoes","san marzano","green beans","canned corn","canned peas","popping corn","corn kernels","simmer sauce","cooking sauce","butter chicken sauce","wing sauce","cheese sauce","whole peeled tomato","whole peeled tomatoes","instant mashed potato","instant mashed potatoes","pieces and stems"];
  const brandTokens = ["campbells","progresso","swanson","knorr","maruchan","nissin","barilla","ronzoni","buitoni","muellers","ragu","prego","classico","bertolli","hunts","contadina","rosarita","goya","iberia","heinz","hellmanns","jif","skippy","smuckers","welchs","cheerios","chex","kelloggs"];
  const brandPhrases = ["del monte","muir glen","old el paso","taco bell","bush beans","college inn","top ramen","cup noodles","newmans own","best foods","hidden valley","peter pan","aunt jemima","pearl milling","mrs butterworth","log cabin","general mills","betty crocker","duncan hines","gold medal","king arthur","domino sugar","c and h","cinnamon toast","cocoa puffs","cap n crunch","honey nut","frosted flakes","fruit loops","raisin bran","grape nuts","honey bunches","lucky charms","special k"];
  if (hasAnyToken(tokens, strongTokens)) return true;
  if (hasAnyPhrase(norm, strongPhrases)) return true;
  if (hasAnyToken(tokens, brandTokens)) return true;
  if (hasAnyPhrase(norm, brandPhrases)) return true;
  return false;
}

function isBeverages(tokens: string[], norm: string): boolean {
  if (hasAnyToken(tokens, ["creamer","topping"])) return false;
  const strongMeatTokens = ["beef","steak","steaks","pork","chicken","sirloin","ribeye","strip","brisket","roast","lamb","veal","sausage","bacon"];
  const isCoffeeProduct = hasAnyToken(tokens, COFFEE_TERMS) || hasAnyPhrase(norm, COFFEE_TERMS);
  if (hasAnyToken(tokens, strongMeatTokens) && !hasAnyToken(tokens, ["jerky"]) && !isCoffeeProduct) return false;
  const strongTokens = ["soda","cola","juice","lemonade","tea","coffee","kombucha","gatorade","powerade","pedialyte"];
  const strongPhrases = ["bottled water","spring water","sparkling water","mineral water","coconut water","drinking water","purified water","distilled water","alkaline water","energy drink","energy drinks","sports drink","sports drinks","iced tea","iced coffee","cold brew","orange juice","apple juice","cranberry juice","grape juice","fruit punch","lemon lime","root beer","cream soda","ginger ale","tonic water","club soda","seltzer water","hydration drink","hydration drinks","hydration beverage","hydration beverages","electrolyte drink","electrolyte drinks","electrolyte beverage","electrolyte beverages"];
  const brandTokens = ["pepsi","sprite","fanta","snapple","arizona","lipton","brisk","starbucks","dunkin","nescafe","folgers","maxwell","dasani","aquafina","evian","fiji","voss","lacroix","perrier","pellegrino","essentia","smartwater","vitaminwater","bodyarmor","celsius","monster","rockstar","prime"];
  const brandPhrases = ["coca cola","dr pepper","mountain dew","sierra mist","canada dry","poland spring","crystal geyser","deer park","red bull","liquid iv","ocean spray","minute maid","tropicana","simply orange","simply lemonade","capri sun","kool aid","hawaiian punch","nesquik","yoo hoo","dole juice","welchs juice","v8 juice"];
  if (hasToken(tokens, "water")) {
    const foodContextTokens = ["tuna","chicken","salmon","sardines","sardine","garlic","beans","vegetables","mushrooms","mushroom","ham","anchovies","clams","crab","shrimp","artichoke","artichokes","hearts"];
    if ((hasPhrase(norm, "in water") || hasPhrase(norm, "packed in water") || hasPhrase(norm, "water added")) && hasAnyToken(tokens, foodContextTokens)) return false;
    return true;
  }
  if (hasToken(tokens, "juice")) {
    const fruitInJuice = hasAnyPhrase(norm, ["in juice","in fruit juice","in 100"]);
    const containerWords = hasAnyToken(tokens, ["jar","jars","can","cans","cup","cups"]);
    const fruitWords = hasAnyToken(tokens, ["mandarin","oranges","peaches","pears","pineapple","fruit","cocktail","cherries","mixed"]);
    if (fruitInJuice && (containerWords || fruitWords)) return false;
    if (hasAnyToken(tokens, ["packets","packet"]) && hasAnyToken(tokens, ["lemon","lime"])) return false;
  }
  if (hasToken(tokens, "cider") && !hasToken(tokens, "hard")) return true;
  if (hasAnyToken(tokens, strongTokens)) return true;
  if (hasAnyPhrase(norm, strongPhrases)) return true;
  if (hasAnyToken(tokens, brandTokens)) return true;
  if (hasAnyPhrase(norm, brandPhrases)) return true;
  return false;
}

function isAltMilk(tokens: string[], norm: string): boolean {
  const strongPhrases = ["oat milk","almond milk","soy milk","coconut milk","cashew milk","rice milk","hemp milk","flax milk","pea milk","macadamia milk","plant milk","plant based milk","oat creamer","almond creamer","coconut creamer","soy creamer"];
  const brandTokens = ["oatly","silk","califia","ripple","elmhurst","malk","milkadamia","nutpods","forager"];
  const brandPhrases = ["planet oat","so delicious","minor figures","good karma"];
  if (hasAnyPhrase(norm, strongPhrases)) return true;
  if (hasAnyToken(tokens, brandTokens)) return true;
  if (hasAnyPhrase(norm, brandPhrases)) return true;
  return false;
}

function isEggs(tokens: string[], norm: string): boolean {
  const strongTokens = ["eggs","egg"];
  const strongPhrases = ["egg whites","liquid eggs","cage free eggs","free range eggs","dozen eggs","large eggs","organic eggs","brown eggs"];
  const brandPhrases = ["egg lands best","vital farms","happy egg","pete and gerrys"];
  if (hasToken(tokens, "eggnog")) return false;
  if (hasToken(tokens, "eggplant")) return false;
  if (hasAnyPhrase(norm, [
    "egg roll",
    "egg rolls",
    "breakfast sandwich",
    "breakfast sandwiches",
    "easter egg",
    "easter eggs"
  ])) return false;

  if (hasAnyToken(tokens, [
    "sandwich","sandwiches","candy","chocolate"
  ])) return false;
  if (hasAnyToken(tokens, strongTokens)) return true;
  if (hasAnyPhrase(norm, strongPhrases)) return true;
  if (hasAnyPhrase(norm, brandPhrases)) return true;
  return false;
}

function isDairy(tokens: string[], norm: string): boolean {
  const strongTokens = ["milk","cheese","yogurt","butter","cream","creamer","cottage","ricotta","mozzarella","cheddar","parmesan","provolone","swiss","gouda","brie","camembert","feta","gruyere","manchego","colby","monterey","havarti","muenster","velveeta","queso","custard","eggnog","kefir","ghee"];
  const strongPhrases = ["cream cheese","sour cream","heavy cream","whipping cream","half and half","cottage cheese","shredded cheese","string cheese","cheese slices","sliced cheese","cheese block","greek yogurt","whole milk","skim milk","2 percent milk","1 percent milk","chocolate milk","strawberry milk","buttermilk","whipped cream","cool whip","coffee creamer","french vanilla creamer","hazelnut creamer","american cheese"];
  const brandTokens = ["horizon","fairlife","lactaid","kerrygold","tillamook","cabot","sargento","philadelphia","borden","daisy","fage","dannon","yoplait","stonyfield","oikos","activia","noosa","breakstone"];
  const brandPhrases = ["organic valley","land o lakes","chobani yogurt","international delight","coffee mate","reddi wip","siggi s"];
  if (hasToken(tokens, "creamer") && hasAnyToken(tokens, ["potato","potatoes","fingerling","gold","yellow","red"])) return false;
  if (hasToken(tokens, "creamer") && hasAnyPhrase(norm, ["nondairy","non dairy"])) return false;
  if (hasToken(tokens, "creamer") && hasAnyToken(tokens, ["packets","packet","powder"])) return false;
  if (hasToken(tokens, "cream") && hasToken(tokens, "coconut") && !hasToken(tokens, "ice")) return false;
  if (
    hasAnyToken(tokens, ["waffle","waffles","pancake","pancakes"]) &&
    hasAnyToken(tokens, ["butter","buttermilk"])
  ) return false;
  if (hasToken(tokens, "butter")) {
    if (hasAnyPhrase(norm, [
      "peanut butter",
      "almond butter",
      "sunflower butter",
      "cashew butter",
      "cookie butter",
      "butter lettuce",
      "butter leaf",
      "butter chicken",
      "simmer sauce",
      "cooking sauce"
    ])) return false;

    if (hasAnyToken(tokens, [
      "butterbread",
      "butterbeer",
      "butternut",
      "butterfinger",
      "waffle",
      "waffles",
      "pancake",
      "pancakes"
    ])) return false;

    if (hasAnyToken(tokens, ["herb","homestyle","mashed"])) return false;
  }
  if (hasToken(tokens, "cream")) {
    if (hasPhrase(norm, "ice cream")) return false;
    if (hasAnyPhrase(norm, ["cream of mushroom","cream of chicken","cream of celery","cream of broccoli"])) return false;
    if (hasPhrase(norm, "cream soda")) return false;
  }
  if (hasToken(tokens, "milk") && hasToken(tokens, "chocolate") && !hasAnyToken(tokens, ["whole","skim","2"])) {
    if (hasPhrase(norm, "milk chocolate")) return false;
  }
  if (hasToken(tokens, "milk") && hasAnyToken(tokens, ["oat","almond","soy","coconut","cashew","rice","hemp","flax","pea"])) return false;
  if (hasAnyToken(tokens, ["egg","eggs"])) {
    if (hasToken(tokens, "eggplant")) return false;
    if (hasAnyPhrase(norm, ["easter egg","easter eggs","egg roll","egg rolls"])) return false;
    if (hasAnyToken(tokens, ["cadbury","hershey","hersheys","candy","chocolate"]) && !hasAnyPhrase(norm, ["egg whites","liquid eggs"])) return false;
  }
  if (hasAnyToken(tokens, ["cheese","cheddar","parmesan","gouda"])) {
    if (hasToken(tokens, "sauce")) return false;
    if (hasAnyPhrase(norm, ["cheese crackers","cheese chips","cheese puffs","cheese tortilla","nacho cheese"])) return false;
    if (hasAnyToken(tokens, ["crackers","chips","puffs","crisps","popcorn"])) return false;
    if (hasAnyToken(tokens, ["sausage","sausages","bratwurst","burger","patty","patties"])) return false;
  }
  if (hasAnyToken(tokens, strongTokens)) return true;
  if (hasAnyPhrase(norm, strongPhrases)) return true;
  if (hasAnyToken(tokens, brandTokens)) return true;
  if (hasAnyPhrase(norm, brandPhrases)) return true;
  return false;
}

function isSeafood(tokens: string[], norm: string): boolean {
  if (hasToken(tokens, "canned")) return false;
  if (hasToken(tokens, "frozen")) return false;
  if (hasAnyToken(tokens, ["soup","chowder","bisque","stew"])) return false;
  if (hasToken(tokens, "clam") && (hasPhrase(norm, "clam shell") || hasToken(tokens, "clamshell"))) return false;
  const strongTokens = ["salmon","tuna","shrimp","prawns","crab","lobster","tilapia","cod","halibut","mahi","swordfish","catfish","trout","snapper","bass","perch","clam","clams","oyster","oysters","mussel","mussels","scallop","scallops","calamari","squid","octopus","sardines","anchovies","crawfish","crayfish"];
  const strongPhrases = ["fish fillet","fish filet","fish steak","raw shrimp","wild caught","farm raised","king crab","snow crab","crab legs","crab meat","lobster tail","lobster tails","smoked salmon","ahi tuna","yellowfin tuna","albacore tuna","sushi grade","seafood mix","seafood medley"];
  if (hasAnyToken(tokens, strongTokens)) return true;
  if (hasAnyPhrase(norm, strongPhrases)) return true;
  return false;
}

function isMeat(tokens: string[], norm: string): boolean {
  const exclusionTokens = ["soup","broth","stock","bouillon","ramen","noodle","noodles","canned","flavored","flavor","seasoning","sauce","gravy","spread","dip","pizza","burrito","enchilada","taco","wrap","sandwich","bowl","dinner","meal","entree","helper","mix","ravioli","lasagna","pot pie","salad","salads"];
  const exclusionPhrases = ["pot pie","mac and cheese","cup noodle"];
  if (hasAnyToken(tokens, exclusionTokens)) return false;
  if (hasAnyPhrase(norm, exclusionPhrases)) return false;
  const strongTokens = ["chicken","beef","pork","turkey","lamb","veal","bison","venison","duck","goose","quail","steak","roast","ribs","chops","chop","ground","sausage","sausages","bacon","ham","salami","pepperoni","bratwurst","brat","brats","kielbasa","chorizo","andouille","frank","franks","patty","patties","meatball","meatballs","prosciutto","pancetta"];
  const strongPhrases = ["chicken breast","chicken thigh","chicken thighs","chicken drumstick","chicken wing","chicken wings","chicken leg","chicken legs","chicken tender","chicken tenders","whole chicken","bone in chicken","boneless skinless","split breast","ground beef","ground turkey","ground pork","ground chicken","ground lamb","beef steak","pork chop","pork chops","pork loin","pork tenderloin","pork shoulder","pork butt","pork belly","baby back ribs","spare ribs","st louis ribs","beef roast","chuck roast","pot roast","eye round","top round","bottom round","flank steak","skirt steak","sirloin steak","ny strip","new york strip","ribeye steak","filet mignon","t bone","porterhouse","tri tip","brisket","beef short ribs","london broil","beef tenderloin","stew meat","beef stew","cubed steak","corned beef","rack of lamb","lamb chop","lamb chops","leg of lamb","lamb shank","turkey breast","turkey thigh","turkey leg","turkey drumstick","whole turkey","italian sausage","pork sausage","chicken sausage","turkey sausage","andouille sausage","smoked sausage","polish sausage","link sausage","sausage link","sausage patty","breakfast sausage","center cut bacon","thick cut bacon","turkey bacon","canadian bacon","applewood bacon","smoked ham","honey ham","spiral ham","bone in ham","deli ham","hot dog","hot dogs"];
  const brandTokens = ["tyson","perdue","butterball","johnsonville","eckrich","hormel","smithfield","farmland","swift","applegate"];
  const brandPhrases = ["foster farms","bell evans","jennie o","jimmy dean","bar s","hebrew national","nathan s","oscar mayer","certified angus","organic prairie","pilgrim s"];
  if (hasAnyToken(tokens, strongTokens)) return true;
  if (hasAnyPhrase(norm, strongPhrases)) return true;
  if (hasAnyToken(tokens, brandTokens)) return true;
  if (hasAnyPhrase(norm, brandPhrases)) return true;
  return false;
}

function isProduce(tokens: string[], norm: string): boolean {
  if (hasAnyToken(tokens, ["juice","soda","drink","sauce","soup","canned","dried","chip","chips","seltzer","wine","beer"])) return false;
  if (hasAnyPhrase(norm, ["freeze dried"])) return false;
  if (hasToken(tokens, "corn") && hasAnyToken(tokens, ["meal","flour","starch","syrup","oil"])) return false;
  if (hasToken(tokens, "corn") && hasAnyToken(tokens, ["popping","kettle","candy"])) return false;
  if (hasAnyToken(tokens, ["powder","granules","flakes","dehydrated","crystallized","paste"])) return false;
  if (hasAnyPhrase(norm, ["garlic salt","onion salt","garlic paste","minced garlic"])) return false;
  const strongTokens = ["apple","apples","banana","bananas","orange","oranges","tomato","tomatoes","grape","grapes","berry","berries","strawberry","strawberries","blueberry","blueberries","raspberry","raspberries","blackberry","blackberries","lemon","lemons","lime","limes","mango","mangoes","mangos","pineapple","watermelon","cantaloupe","honeydew","peach","peaches","pear","pears","plum","plums","nectarine","nectarines","cherry","cherries","pomegranate","papaya","guava","kiwi","tangerine","tangerines","clementine","clementines","mandarin","mandarins","grapefruit","fig","figs","persimmon","lychee","plantain","plantains","coconut","lettuce","spinach","kale","arugula","broccoli","carrot","carrots","celery","onion","onions","potato","potatoes","pepper","peppers","cucumber","cucumbers","avocado","avocados","mushroom","mushrooms","zucchini","squash","corn","asparagus","cabbage","cauliflower","garlic","cilantro","parsley","basil","ginger","beet","beets","radish","radishes","artichoke","artichokes","fennel","turnip","turnips","rutabaga","yam","yams","scallion","scallions","leek","leeks","shallot","shallots","chive","chives","dill","mint","rosemary","thyme","sage","jalapeno","serrano","habanero","poblano","eggplant"];
  const strongPhrases = ["fresh fruit","fresh vegetables","fresh produce","salad mix","salad kit","salad blend","spring mix","romaine hearts","iceberg lettuce","baby spinach","baby carrots","baby kale","green beans","snap peas","snow peas","sugar snap","brussels sprouts","bok choy","bean sprouts","water chestnuts","bamboo shoots","jicama","tomatillo","napa cabbage","savoy cabbage","red cabbage","collard greens","mustard greens","turnip greens","dandelion greens","swiss chard","endive","radicchio","watercress","microgreens","herb bunch","fresh herbs","organic produce","bell pepper","bell peppers","sweet potato","sweet potatoes","green onion","green onions","passion fruit","dragon fruit"];
  if (hasAnyToken(tokens, strongTokens)) return true;
  if (hasAnyPhrase(norm, strongPhrases)) return true;
  return false;
}

// --- Main categorizer entry point ---

export interface CategoryConflictResult {
  category: ProductCategory;
  conflicts: boolean;
  flagged: boolean;
}

export function categorizeWithConflicts(productName: string): CategoryConflictResult {
  const fallback: CategoryConflictResult = {
    category: ProductCategory.CATEGORY_UNCERTAIN,
    conflicts: false,
    flagged: false,
  };

  if (!productName || typeof productName !== "string") return fallback;
  const norm = normalizeText(productName);
  if (norm.length === 0) return fallback;
  const tokens = tokenize(productName);
  if (tokens.length === 0) return fallback;

  if (precheckNonFood(norm)) return fallback;
  if (precheckCannedPantry(tokens, norm)) return { category: ProductCategory.PANTRY, conflicts: false, flagged: false };
  if (precheckSpicePantry(tokens, norm)) return { category: ProductCategory.PANTRY, conflicts: false, flagged: false };
  if (precheckFrozenBrands(tokens, norm)) return { category: ProductCategory.FROZEN, conflicts: false, flagged: false };
  if (precheckCoffee(tokens, norm)) return { category: ProductCategory.BEVERAGES, conflicts: false, flagged: false };
  if (precheckBabyYogurt(tokens, norm)) return { category: ProductCategory.BABY, conflicts: false, flagged: false };
  if (precheckShrimp(tokens, norm)) return { category: ProductCategory.SEAFOOD, conflicts: false, flagged: false };
  if (precheckFlavoredSnack(tokens, norm)) return { category: ProductCategory.SNACKS, conflicts: false, flagged: false };
  const onionOverride = precheckOnionProduct(tokens, norm);
  if (onionOverride) return { category: onionOverride, conflicts: false, flagged: false };
  const preparedOverride = precheckPreparedProduce(tokens, norm);
  if (preparedOverride) return { category: preparedOverride, conflicts: false, flagged: false };
  const suppressBaby = precheckBabyGuard(tokens, norm);

  const detectors: Array<[ProductCategory, (t: string[], n: string) => boolean]> = [
    [ProductCategory.PET, isPet],
    [ProductCategory.BABY, suppressBaby ? () => false : isBaby],
    [ProductCategory.HEALTH_BEAUTY, isHealthBeauty],
    [ProductCategory.HOUSEHOLD, isHousehold],
    [ProductCategory.FLORAL, isFloral],
    [ProductCategory.ALCOHOL, isAlcohol],
    [ProductCategory.FROZEN, isFrozen],
    [ProductCategory.DELI_PREPARED, isDeliPrepared],
    [ProductCategory.DESSERT, isDessert],
    [ProductCategory.BAKERY, isBakery],
    [ProductCategory.ALT_MILK, isAltMilk],
    [ProductCategory.SNACKS, isSnacks],
    [ProductCategory.PANTRY, isPantry],
    [ProductCategory.BEVERAGES, isBeverages],
    [ProductCategory.EGGS, isEggs],
    [ProductCategory.DAIRY, isDairy],
    [ProductCategory.SEAFOOD, isSeafood],
    [ProductCategory.MEAT, isMeat],
    [ProductCategory.PRODUCE, isProduce],
  ];

  let winner: ProductCategory | null = null;
  const allMatches: ProductCategory[] = [];

  for (const [cat, detector] of detectors) {
    if (detector(tokens, norm)) {
      if (!winner) winner = cat;
      allMatches.push(cat);
    }
  }

  if (!winner) return fallback;

  const hasConflicts = allMatches.length > 1;
  return {
    category: winner,
    conflicts: hasConflicts,
    flagged: hasConflicts,
  };
}

// ============================================================================
// BRAND MATCHER (from brand-and-category/brandMatcher.ts)
// ============================================================================

export const DESCRIPTOR_SET: Set<string> = new Set([
  "fresh","premium","quality","select","choice","fancy","natural","classic",
  "original","traditional","authentic","artisan","artisanal","gourmet",
  "homestyle","homemade","wholesome","hearty","finest","superior","deluxe",
  "boneless","skinless","whole","sliced","diced","chopped","minced","ground",
  "shredded","cubed","crushed","peeled","deveined","trimmed","filleted",
  "split","halved","quartered","grated","butterflied","portioned",
  "large","small","medium","mini","jumbo","giant","family","value","bulk",
  "single","double","triple","extra","mega","super","king","snack","party",
  "fun","bite","individual","big","little","petite","regular","xl","xxl",
  "multipack","variety",
  "frozen","chilled","refrigerated","raw","cooked","dried","dehydrated",
  "smoked","cured","roasted","grilled","baked","fried","steamed","braised",
  "poached","sauteed","marinated","seasoned","breaded","battered",
  "organic","nongmo","usda","certified","gluten","free","sugar","low",
  "nonfat","reduced","unsalted","lightly","lite","light","diet","zero",
  "keto","vegan","vegetarian","plant","based","dairy","lactose","nut",
  "kosher","halal","non","no","added","fat",
  "bag","box","can","jar","bottle","tub","pouch","tray","container",
  "carton","pack","count","ct","case","wrapped","resealable","squeeze",
  "spray","tube","sleeve","wrapper","packet",
  "the","and","or","with","in","on","of","for","by","from",
  "new","great","best","real","pure","true","simply","just","only",
  "all","our","style","type","kind","flavor","flavored","inspired",
  "made","crafted","handcrafted",
  "red","green","yellow","white","black","golden","brown","dark","pink",
  "orange","blue","purple","sweet","hot","mild","spicy","savory","tangy",
  "zesty","smoky","bold","rich","buttery","velvety",
  "walmart","target","costco","kroger","safeway","wegmans","aldi","trader",
  "joes","marketside","freshness","guaranteed","kirkland",
  "iceberg","romaine","russet","yukon","fuji","gala","anjou","bartlett",
  "heirloom","heritage","seedless","pitted",
  "grass","grain","corn","cage","pasture","raised","fed","caught",
  "wild","farm","local","locally","domestic","imported",
  "baby","toddler","senior","adult","kids","junior","infant",
  "grade","stage","prime","angus",
  "ready","eat","cook","heat","microwave","instant","quick","easy",
  "seasonal","limited","special","edition","everyday","daily",
  "assorted","mixed","blend","enriched","fortified","pasteurized",
  "homogenized","antibiotic","hormone","preservative","artificial",
  "thick","thin","fine","coarse","crispy","crunchy","tender","juicy",
  "creamy","chunky","smooth","fluffy","flaky",
  "net","wt","weight","oz","lb","lbs","fl","gal","gallon","ml",
  "liter","litre","kg","qt","pt",
  "salted","unsweetened","sweetened","unfiltered","unpasteurized",
  "sale","deal","offer","promo","approved","inspected",
  "hand","picked","harvested","sun",
]);

function escapeRegex(str: string): string {
  return str.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function hasSuspiciousLeftover(
  productName: string,
  genericMatch: string,
  descriptorSet: Set<string>,
): { suspicious: boolean; leftoverWords: string[] } {
  let remaining = productName;
  remaining = remaining.replace(new RegExp(escapeRegex(genericMatch), "i"), "");
  remaining = remaining.replace(/\d+\.?\d*\s*(oz|lb|lbs|fl\s*oz|gal|gallon|ml|l|g|kg|ct|count|pack|pk|qt|pt|each|liter|litre)\b/gi, "");
  remaining = remaining.replace(/\d+\s*[-x×]\s*\d+/gi, "");
  remaining = remaining.replace(/\b\d+\s*(\/\s*\d+)?\b/g, "");
  remaining = remaining.replace(/\$\d+\.?\d*/g, "");
  remaining = remaining.replace(/[,\-\/\(\)™®©.!:;|]+/g, " ");
  const tokens = remaining.trim().split(/\s+/).filter((t) => t.length > 1);
  const unknownTokens = tokens.filter((t) => !descriptorSet.has(t.toLowerCase()));
  const brandLikeTokens = unknownTokens.filter((t) => {
    if (t === t.toUpperCase() && t.length >= 2) return true;
    if (t[0] === t[0].toUpperCase() && t[0] !== t[0].toLowerCase()) return true;
    if (t.includes("'")) return true;
    return false;
  });
  return { suspicious: brandLikeTokens.length > 0, leftoverWords: brandLikeTokens };
}

function sortByTokenCountDesc(keys: string[]): string[] {
  return [...keys].sort((a, b) => {
    const aTokens = a.split(" ").length;
    const bTokens = b.split(" ").length;
    if (bTokens !== aTokens) return bTokens - aTokens;
    return b.length - a.length;
  });
}

export function buildBrandLookup(entries: BrandEntry[]): BrandLookup {
  const brandMap = new Map<string, string>();
  const isBrandFlags = new Map<string, boolean>();

  for (const { brandName, isBrand } of entries) {
    const normalized = normalizeText(brandName);
    if (normalized.length > 0 && !brandMap.has(normalized)) {
      brandMap.set(normalized, brandName);
      isBrandFlags.set(normalized, isBrand);
    }
  }

  const allKeys = [...brandMap.keys()];
  const sortedRealBrands = sortByTokenCountDesc(allKeys.filter(k => isBrandFlags.get(k) === true));
  const sortedGenericNames = sortByTokenCountDesc(allKeys.filter(k => isBrandFlags.get(k) !== true));

  return { brandMap, isBrandFlags, sortedRealBrands, sortedGenericNames };
}

export function addBrandToLookup(lookup: BrandLookup, brandName: string, isBrand = true): BrandLookup {
  const normalized = normalizeText(brandName);
  if (normalized.length === 0) return lookup;
  if (lookup.brandMap.has(normalized)) return lookup;

  lookup.brandMap.set(normalized, brandName);
  lookup.isBrandFlags.set(normalized, isBrand);

  const targetList = isBrand ? lookup.sortedRealBrands : lookup.sortedGenericNames;
  const newTokenCount = normalized.split(" ").length;
  let insertIdx = 0;
  for (let i = 0; i < targetList.length; i++) {
    const existingTokenCount = targetList[i].split(" ").length;
    if (existingTokenCount < newTokenCount) { insertIdx = i; break; }
    if (existingTokenCount === newTokenCount && targetList[i].length < normalized.length) { insertIdx = i; break; }
    insertIdx = i + 1;
  }
  targetList.splice(insertIdx, 0, normalized);
  return lookup;
}

export function extractBrand(productName: string, lookup: BrandLookup): BrandMatchResult | null {
  if (!productName || typeof productName !== "string") return null;
  const norm = normalizeText(productName);
  if (norm.length === 0) return null;
  const tokens = norm.split(" ").filter(Boolean);
  if (tokens.length === 0) return null;

  const { sortedRealBrands, sortedGenericNames, brandMap } = lookup;

  // Pass 1A: real brand at start
  for (const normalizedBrand of sortedRealBrands) {
    if (norm.startsWith(normalizedBrand + " ") || norm === normalizedBrand) {
      return { brand: brandMap.get(normalizedBrand)!, isBrand: true, needsAI: false, tentativeGenericMatch: null };
    }
  }
  // Pass 1B: real brand anywhere
  for (const normalizedBrand of sortedRealBrands) {
    if (normalizedBrand.length <= 3) continue;
    const brandTokens = normalizedBrand.split(" ");
    if (brandTokens.length > 1) {
      if (hasPhrase(norm, normalizedBrand)) {
        return { brand: brandMap.get(normalizedBrand)!, isBrand: true, needsAI: false, tentativeGenericMatch: null };
      }
    } else {
      if (hasToken(tokens, normalizedBrand)) {
        return { brand: brandMap.get(normalizedBrand)!, isBrand: true, needsAI: false, tentativeGenericMatch: null };
      }
    }
  }

  // Pass 2: generic/canonical names
  let tentativeGeneric: { brand: string } | null = null;
  for (const normalizedGeneric of sortedGenericNames) {
    if (hasPhraseNormalized(norm, normalizedGeneric)) {
      const genericBrandName = brandMap.get(normalizedGeneric)!;
      const { suspicious } = hasSuspiciousLeftover(productName, genericBrandName, DESCRIPTOR_SET);
      if (!suspicious) {
        return { brand: genericBrandName, isBrand: false, needsAI: false, tentativeGenericMatch: null };
      } else {
        tentativeGeneric = { brand: genericBrandName };
        break;
      }
    }
  }

  // Pass 3: signal AI needed
  if (tentativeGeneric) {
    return { brand: tentativeGeneric.brand, isBrand: false, needsAI: true, tentativeGenericMatch: tentativeGeneric };
  }
  return null;
}

const HEURISTIC_NONSTARTER = new Set([
  "red","green","blue","yellow","white","black","orange","purple","brown","pink","golden","silver","dark","light",
  "large","small","medium","mini","jumbo","giant","big","little","petite","value",
  "organic","natural","fresh","whole","pure","wild","free","raw","local","artisan","premium","gourmet","ultra","super","extra",
  "frozen","canned","dried","roasted","grilled","baked","smoked","sliced","diced","chopped","minced","shredded","grated","ground","boneless","skinless","seedless",
  "store","classic","original","traditional","unsalted","salted","sweetened","unsweetened","unfiltered",
  "iceberg","romaine","russet","yukon","fuji","gala","anjou","bartlett","heirloom","heritage",
  "angus","kosher","halal","grass","grain",
  "baby","toddler","senior",
  "grade","stage","select","choice","prime","no",
]);

const HEURISTIC_SECOND_WORD_STOP = new Set([
  "beauty","care","herbal","antibacterial","deodorant","moisturizing","whitening","clarifying","exfoliating",
  "chunk","chunks","chunky","creamy","smooth","crunchy","crispy","tender","juicy","fruity","berry",
  "cage","grass","grain","corn","hormone","antibiotic","pasture","raised","fed",
  "best","finest","plus","pro","max","advanced",
  "honey","vanilla","chocolate","strawberry","cinnamon","lemon","lime","cherry","blueberry","peach","mango","coconut",
  "original","classic","traditional","signature",
  "bar","bars","bag","bags","box","boxes","can","cans","bottle","bottles","jar","jars","spray","wipes","rolls","pods","stick","sticks","sheet","sheets","pack","packs","pouch","pouches",
  "soap","lotion","cream","gel","oil","balm","serum","mask",
  "milk","juice","water","soda","ale","lager","cider",
  "bread","roll","bun","bagel","muffin","cake","cookie","cookies","cracker","crackers","chip","chips",
  "cereal","oats","granola","pasta","noodle","noodles","rice",
  "soup","broth","stock","stew","chili","bisque",
  "sauce","dressing","condiment","ketchup","mustard","mayo","vinegar",
  "beef","chicken","turkey","pork","lamb","fish","shrimp","salmon","tuna","crab","lobster",
  "cheese","yogurt","egg","eggs","butter",
  "apple","banana","grape","orange","lemon","strawberry","blueberry",
  "lettuce","spinach","kale","arugula","tomato","potato","onion","garlic","carrot","celery","broccoli","cauliflower","corn","pepper","peppers","cucumber","zucchini","squash","mushroom","mushrooms","bean","beans","pea","peas",
  "vitamin","supplement","capsule","tablet","pill","gummies","gummy",
  "detergent","cleaner","disinfectant","bleach","wipe",
]);

export function extractBrandHeuristic(productName: string): string | null {
  if (!productName?.trim()) return null;
  const words = productName.trim().split(/\s+/);
  if (words.length === 0) return null;
  const firstRaw = words[0].replace(/[®™©]/g, "").replace(/[,.]$/, "");
  const firstLower = firstRaw.toLowerCase();
  if (HEURISTIC_NONSTARTER.has(firstLower)) return null;
  if (!firstRaw[0] || firstRaw[0] === firstRaw[0].toLowerCase()) return null;
  if (/^\d/.test(firstRaw)) return null;
  if (firstRaw.length <= 1) return null;
  const brandParts = [firstRaw];
  if (words.length > 1) {
    const secondRaw = words[1].replace(/[®™©]/g, "").replace(/[,.]$/, "");
    const secondLower = secondRaw.toLowerCase();
    const secondIsCapitalized = secondRaw[0] && secondRaw[0] !== secondRaw[0].toLowerCase();
    const secondIsStop = HEURISTIC_SECOND_WORD_STOP.has(secondLower);
    const secondIsDescriptor = HEURISTIC_NONSTARTER.has(secondLower);
    const secondIsNumber = /^\d/.test(secondRaw);
    if (secondIsCapitalized && !secondIsStop && !secondIsDescriptor && !secondIsNumber) {
      brandParts.push(secondRaw);
    }
  }
  return brandParts.join(" ");
}

// ============================================================================
// STORE BRAND MATCHER (from brand-and-category/storeBrandMatcher.ts)
// ============================================================================

const RAW_STORE_BRANDS: string[] = [
  "Simply Nature","Specially Selected","liveGfree","Little Journey","Fremont Fish Market",
  "Happy Harvest","Season's Choice","Earth Grown","Belmont","Benton's","Clancy's","Priano",
  "Southern Grove","Elevation","Fit & Active","Deutsche Küche","Chef's Cupboard","Millville",
  "Mama Cozzi's","Nature's Nectar","Friendly Farms",
  "Kirkland Signature","Kirkland",
  "Trader Joe's","Trader José's","Trader Ming's","Trader Giotto's",
  "Great Value","Sam's Choice","Bettergoods","Marketside","Freshness Guaranteed","Equate",
  "Good & Gather","Market Pantry","Favorite Day","Up & Up",
  "Simple Truth Organic","Simple Truth","Private Selection","Kroger","Smart Way","Home Chef","Big K",
  "Signature Select","O Organics","Open Nature","Primo Taglio","Waterfront Bistro",
  "Debi Lilly Design","Ready Meals","Lucerne","Soleil","Value Corner",
  "365 Everyday Value","365 by Whole Foods Market","365 by Whole Foods","Whole Foods Market",
  "Meijer Gold","Meijer","True Goodness","Frederik's","Purple Cow",
  "Clover Valley",
  "Member's Mark",
  "Wellsley Farms",
  "Publix Premium","GreenWise","Publix",
  "Sprouts Organic","Sprouts",
  "Natural Grocers",
  "Lidl",
  "H-E-B Select Ingredients","H-E-B Organics","Hill Country Fare","Central Market","Mi Tienda",
  "Café Olé","Creamy Creations","Meal Simple","H-E-B",
  "Bowl & Basket","Wholesome Pantry",
  "Wegmans Organic","Wegmans",
  "Giant Eagle","Market District","Nature's Basket",
  "Harris Teeter Organics","Harris Teeter","HT Traders",
  "First Street","Sun Harvest",
  "Food 4 Less",
  "Ralphs",
  "Stater Bros.",
  "WinCo Foods",
  "Superior",
  "Northgate González Market",
  "Vallarta",
  "El Super",
  "Cardenas",
];

const STORE_BRAND_MAP = new Map<string, string>();
for (const brand of RAW_STORE_BRANDS) {
  const norm = normalizeText(brand);
  if (norm && !STORE_BRAND_MAP.has(norm)) STORE_BRAND_MAP.set(norm, brand);
}

const STORE_BRAND_LIST: string[] = [...STORE_BRAND_MAP.keys()].sort((a, b) => {
  const tokenDiff = b.split(" ").length - a.split(" ").length;
  return tokenDiff !== 0 ? tokenDiff : b.length - a.length;
});

export function matchStoreBrand(productName: string): string | null {
  if (!productName || typeof productName !== "string") return null;
  const norm = normalizeText(productName);
  if (!norm) return null;
  for (const brand of STORE_BRAND_LIST) {
    if (norm.startsWith(brand + " ") || norm === brand) return STORE_BRAND_MAP.get(brand)!;
  }
  for (const brand of STORE_BRAND_LIST) {
    if (brand.length <= 3) continue;
    if (hasPhraseNormalized(norm, brand)) return STORE_BRAND_MAP.get(brand)!;
  }
  return null;
}

export function isStoreBrand(productName: string): boolean {
  return matchStoreBrand(productName) !== null;
}

// ============================================================================
// SIZE PARSING AND NORMALIZATION (from normalize.ts)
// ============================================================================

const UNIT_ALIASES: Record<string, string> = {
  lb:"lb", lbs:"lb", pound:"lb", pounds:"lb",
  oz:"oz", ounce:"oz", ounces:"oz",
  g:"g", gram:"g", grams:"g",
  kg:"kg", kilogram:"kg", kilograms:"kg",
  mg:"mg", milligram:"mg", milligrams:"mg",
  "fl oz":"fl-oz","fl. oz":"fl-oz","fl. oz.":"fl-oz","fl-oz":"fl-oz",
  "fluid ounce":"fl-oz","fluid ounces":"fl-oz","floz":"fl-oz",
  gal:"gal", gallon:"gal", gallons:"gal",
  qt:"qt", quart:"qt", quarts:"qt",
  pt:"pnt", pnt:"pnt", pint:"pnt", pints:"pnt",
  l:"l", liter:"l", litre:"l", liters:"l", litres:"l",
  ml:"ml", milliliter:"ml", millilitre:"ml", milliliters:"ml", millilitres:"ml",
  cup:"cup", cups:"cup",
  tsp:"tsp", teaspoon:"tsp", teaspoons:"tsp",
  tbsp:"Tbs", tbs:"Tbs", tablespoon:"Tbs", tablespoons:"Tbs",
  "oz.":"oz","lbs.":"lb","lb.":"lb","gal.":"gal","qt.":"qt","pt.":"pnt",
};

const COUNT_UNITS = new Set([
  "ea","each","ct","count","pk","pack","packs","bag","bags","box","boxes",
  "roll","rolls","sheet","sheets","piece","pieces","pc","pcs","bundle","dozen","doz",
  "serving","servings","load","loads","capsule","capsules","tablet","tablets",
  "can","cans","bottle","bottles","jar","jars","pouch","pouches","stick","sticks",
  "bar","bars","sachet","sachets","tube","tubes","packet","packets","sleeve","sleeves",
  "bunch","bouquet","set","loaf","loaves","rack","slab","tray","trays",
  "head","ear","ears","stem","stems","case","cases",
]);

const CANONICAL_COUNT: Record<string, string> = {
  ea:"ea", each:"ea", ct:"ct", count:"ct", pk:"pk", pack:"pk", packs:"pk",
  bag:"bag", bags:"bag", box:"box", boxes:"box", roll:"roll", rolls:"roll",
  sheet:"sheet", sheets:"sheet", piece:"pc", pieces:"pc", pc:"pc", pcs:"pc",
  bundle:"bundle", dozen:"dozen", doz:"dozen", serving:"serving", servings:"serving",
  load:"load", loads:"load", capsule:"capsule", capsules:"capsule",
  tablet:"tablet", tablets:"tablet", can:"can", cans:"can", bottle:"bottle", bottles:"bottle",
  jar:"jar", jars:"jar", pouch:"pouch", pouches:"pouch", stick:"stick", sticks:"stick",
  bar:"bar", bars:"bar", sachet:"sachet", sachets:"sachet", tube:"tube", tubes:"tube",
  packet:"packet", packets:"packet", sleeve:"sleeve", sleeves:"sleeve",
  bunch:"bunch", bouquet:"bouquet", set:"set", loaf:"loaf", loaves:"loaf",
  rack:"rack", slab:"slab", tray:"tray", trays:"tray", head:"head",
  ear:"ear", ears:"ear", stem:"stem", stems:"stem", case:"case", cases:"case",
};

const CATEGORY_STANDARD_UNITS: Record<string, { mass: string; volume: string }> = {
  PRODUCE:            { mass: "lb",  volume: "fl-oz" },
  MEAT:               { mass: "lb",  volume: "fl-oz" },
  SEAFOOD:            { mass: "lb",  volume: "fl-oz" },
  DAIRY:              { mass: "oz",  volume: "fl-oz" },
  EGGS:               { mass: "oz",  volume: "fl-oz" },
  ALT_MILK:           { mass: "oz",  volume: "fl-oz" },
  DELI_PREPARED:      { mass: "lb",  volume: "fl-oz" },
  BAKERY:             { mass: "oz",  volume: "fl-oz" },
  SNACKS:             { mass: "oz",  volume: "fl-oz" },
  PANTRY:             { mass: "oz",  volume: "fl-oz" },
  DESSERT:            { mass: "oz",  volume: "fl-oz" },
  BEVERAGES:          { mass: "oz",  volume: "fl-oz" },
  FROZEN:             { mass: "oz",  volume: "fl-oz" },
  HOUSEHOLD:          { mass: "oz",  volume: "fl-oz" },
  HEALTH_BEAUTY:      { mass: "oz",  volume: "fl-oz" },
  PET:                { mass: "lb",  volume: "fl-oz" },
  BABY:               { mass: "oz",  volume: "fl-oz" },
  ALCOHOL:            { mass: "oz",  volume: "fl-oz" },
  FLORAL:             { mass: "oz",  volume: "fl-oz" },
  CATEGORY_UNCERTAIN: { mass: "oz",  volume: "fl-oz" },
};

const DESCRIPTIVE_SIZES: Record<string, ParsedSize> = {
  "family size":  { amount:20, unit:"oz", type:"mass", rawSize:20, rawCount:1, rawUnit:"oz" },
  "family pack":  { amount:20, unit:"oz", type:"mass", rawSize:20, rawCount:1, rawUnit:"oz" },
  "large":        { amount:16, unit:"oz", type:"mass", rawSize:16, rawCount:1, rawUnit:"oz" },
  "lg":           { amount:16, unit:"oz", type:"mass", rawSize:16, rawCount:1, rawUnit:"oz" },
  "medium":       { amount:12, unit:"oz", type:"mass", rawSize:12, rawCount:1, rawUnit:"oz" },
  "med":          { amount:12, unit:"oz", type:"mass", rawSize:12, rawCount:1, rawUnit:"oz" },
  "small":        { amount:8,  unit:"oz", type:"mass", rawSize:8,  rawCount:1, rawUnit:"oz" },
  "sm":           { amount:8,  unit:"oz", type:"mass", rawSize:8,  rawCount:1, rawUnit:"oz" },
  "snack size":   { amount:6,  unit:"oz", type:"mass", rawSize:6,  rawCount:1, rawUnit:"oz" },
  "king size":    { amount:24, unit:"oz", type:"mass", rawSize:24, rawCount:1, rawUnit:"oz" },
  "party size":   { amount:28, unit:"oz", type:"mass", rawSize:28, rawCount:1, rawUnit:"oz" },
  "jumbo":        { amount:24, unit:"oz", type:"mass", rawSize:24, rawCount:1, rawUnit:"oz" },
  "mini":         { amount:4,  unit:"oz", type:"mass", rawSize:4,  rawCount:1, rawUnit:"oz" },
  "single":       { amount:1,  unit:"ea", type:"count", rawSize:1, rawCount:1, rawUnit:"ea" },
  "individual":   { amount:1,  unit:"ea", type:"count", rawSize:1, rawCount:1, rawUnit:"ea" },
  "each":         { amount:1,  unit:"ea", type:"count", rawSize:1, rawCount:1, rawUnit:"ea" },
};

function isDescriptiveSize(raw: string): boolean {
  return Object.prototype.hasOwnProperty.call(DESCRIPTIVE_SIZES, raw.toLowerCase());
}

function resolveUnit(unitStr: string): { unit: string; type: MeasureType } | null {
  const cuUnit = UNIT_ALIASES[unitStr];
  if (cuUnit) return { unit: cuUnit, type: measureType(cuUnit) };
  if (COUNT_UNITS.has(unitStr)) return { unit: CANONICAL_COUNT[unitStr] ?? unitStr, type: "count" };
  return null;
}

function parseSize(raw: string): ParsedSize | null {
  if (!raw || !raw.trim()) return null;
  let s = raw.trim();

  // Price-as-size: "$16.99 / lbs"
  const priceMatch = s.match(/^\$[\d.,]+\s*\/\s*(.+)$/i);
  if (priceMatch) {
    const resolved = resolveUnit(priceMatch[1].trim().toLowerCase());
    if (resolved) return { amount:1, ...resolved, rawSize:1, rawCount:1, rawUnit:resolved.unit };
    return null;
  }

  s = s.replace(/^(about|approx\.?|approximately|~)\s*/i, "");
  s = s.replace(/[.,]+$/, "");

  // Strip trailing junk (only when not a multi-pack pattern)
  if (!/^\d[\d.,]*\s*[xX×*]\s*\d/.test(s)) {
    const cleaned = s.replace(/\s*[x.,/|\-]+\s*$/i, "");
    if (cleaned !== s && cleaned.length > 0) s = cleaned.trim();
  }

  // Sold by patterns
  const soldMatch = s.match(/^sold\s+(?:individually|per\s+(\w+)|by\s+(?:the\s+)?(\w+)|by\s+weight)$/i);
  if (soldMatch) {
    const unitWord = (soldMatch[1] || soldMatch[2] || "").toLowerCase();
    if (!unitWord || unitWord === "individually") return { amount:1, unit:"ea", type:"count", rawSize:1, rawCount:1, rawUnit:"ea" };
    if (unitWord === "weight" || unitWord === "pound" || unitWord === "lb") return { amount:1, unit:"lb", type:"mass", rawSize:1, rawCount:1, rawUnit:"lb" };
    if (unitWord === "dozen") return { amount:12, unit:"ea", type:"count", rawSize:12, rawCount:12, rawUnit:"ea" };
    const resolved = resolveUnit(unitWord);
    if (resolved) return { amount:1, ...resolved, rawSize:1, rawCount:1, rawUnit:resolved.unit };
    return { amount:1, unit:"ea", type:"count", rawSize:1, rawCount:1, rawUnit:"ea" };
  }
  if (/^sold\s+by\s+weight$/i.test(s)) return { amount:1, unit:"lb", type:"mass", rawSize:1, rawCount:1, rawUnit:"lb" };

  // Per X / by the X
  const perUnitMatch = s.match(/^(?:per|by\s+the)\s+(.+)$/i);
  if (perUnitMatch) {
    const unitWord = perUnitMatch[1].trim().toLowerCase();
    if (unitWord === "dozen" || unitWord === "doz") return { amount:12, unit:"ea", type:"count", rawSize:12, rawCount:12, rawUnit:"ea" };
    if (["unit","item","serving"].includes(unitWord)) return { amount:1, unit:"ea", type:"count", rawSize:1, rawCount:1, rawUnit:"ea" };
    const resolved = resolveUnit(unitWord);
    if (resolved) return { amount:1, ...resolved, rawSize:1, rawCount:1, rawUnit:resolved.unit };
    return { amount:1, unit:"ea", type:"count", rawSize:1, rawCount:1, rawUnit:"ea" };
  }

  if (/^half\s+dozen$/i.test(s)) return { amount:6, unit:"ea", type:"count", rawSize:6, rawCount:6, rawUnit:"ea" };
  if (/^half\s+gal(?:lon)?$/i.test(s)) return { amount:0.5, unit:"gal", type:"volume", rawSize:0.5, rawCount:1, rawUnit:"gal" };

  const dozenMatch = s.match(/^([\d.,]+)\s+dozen$/i);
  if (dozenMatch) {
    const n = parseFloat(dozenMatch[1].replace(/,/g, ""));
    if (!isNaN(n) && n > 0) { const total = n*12; return { amount:total, unit:"ea", type:"count", rawSize:total, rawCount:total, rawUnit:"ea" }; }
  }

  const fracDozenMatch = s.match(/^(\d+)\s*\/\s*(\d+)\s+dozen$/i);
  if (fracDozenMatch) {
    const num = parseInt(fracDozenMatch[1],10), den = parseInt(fracDozenMatch[2],10);
    if (den !== 0) { const total = (num/den)*12; return { amount:total, unit:"ea", type:"count", rawSize:total, rawCount:total, rawUnit:"ea" }; }
  }

  const halfGalMatch = s.match(/^([\d.,]+)\s+half\s+gal(?:lon)?$/i);
  if (halfGalMatch) {
    const n = parseFloat(halfGalMatch[1].replace(/,/g, ""));
    if (!isNaN(n) && n > 0) return { amount:n*0.5, unit:"gal", type:"volume", rawSize:0.5, rawCount:n, rawUnit:"gal" };
  }

  // Weight + container: "5 lb bag"
  const weightContainerMatch = s.match(
    /^([\d.,]+)\s*(lbs?\.?|oz\.?|kg|g|fl\.?\s*oz\.?|ml|l|gal\.?|qt\.?|pt\.?|pints?|quarts?|gallons?|pounds?|ounces?|kilograms?|grams?|milligrams?|liters?|litres?|milliliters?|millilitres?)\s+(?:bag|bags|box|boxes|tray|trays|case|cases|container|containers|pack|packs|jar|jars|can|cans|bottle|bottles|bunch|loaf|loaves|slab|rack)$/i
  );
  if (weightContainerMatch) {
    const amount = parseFloat(weightContainerMatch[1].replace(/,/g, ""));
    if (!isNaN(amount) && amount > 0) {
      const resolved = resolveUnit(weightContainerMatch[2].trim().toLowerCase());
      if (resolved) return { amount, ...resolved, rawSize:amount, rawCount:1, rawUnit:resolved.unit };
    }
  }

  // Count filler: "24 ct bag", "1 count bunch"
  const PACKAGING_WORDS = new Set(["bag","bags","box","boxes","tray","trays","can","cans","bottle","bottles","jar","jars","container","containers","carton","cartons","pouch","pouches","sleeve","sleeves","wrapper","wrappers","package","packages","pkg"]);
  const countFillerMatch = s.match(/^([\d.,]+)\s*(?:count|ct|no\.?|number)\s+(.+)$/i);
  if (countFillerMatch) {
    const amount = parseFloat(countFillerMatch[1].replace(/,/g, ""));
    const unitWord = countFillerMatch[2].trim().toLowerCase();
    if (!isNaN(amount) && amount > 0) {
      if (PACKAGING_WORDS.has(unitWord)) return { amount, unit:"ea", type:"count", rawSize:amount, rawCount:amount, rawUnit:"ea" };
      const resolved = resolveUnit(unitWord);
      if (resolved) return { amount, ...resolved, rawSize:amount, rawCount:amount, rawUnit:resolved.unit };
      return { amount, unit:"ea", type:"count", rawSize:amount, rawCount:amount, rawUnit:"ea" };
    }
  }

  // Multi-pack: "6 x 250 ml"
  const multiMatch = s.match(/^([\d.,]+)\s*[xX×*]\s*([\d.,]+)\s*(.+)$/i);
  if (multiMatch) {
    const count = parseFloat(multiMatch[1].replace(/,/g, "")), perUnit = parseFloat(multiMatch[2].replace(/,/g, ""));
    if (!isNaN(count) && !isNaN(perUnit)) {
      const resolved = resolveUnit(multiMatch[3].trim().toLowerCase());
      if (resolved) return { amount:count*perUnit, ...resolved, rawSize:perUnit, rawCount:count, rawUnit:resolved.unit };
    }
  }

  // N-pack N unit: "4-pack 4 oz"
  const packMatch = s.match(/^([\d.,]+)\s*[-\s]?\s*(?:pack|pk)\s*[,\s]\s*([\d.,]+)\s*(.+)$/i);
  if (packMatch) {
    const count = parseFloat(packMatch[1].replace(/,/g, "")), perUnit = parseFloat(packMatch[2].replace(/,/g, ""));
    if (!isNaN(count) && !isNaN(perUnit)) {
      const resolved = resolveUnit(packMatch[3].trim().toLowerCase());
      if (resolved) return { amount:count*perUnit, ...resolved, rawSize:perUnit, rawCount:count, rawUnit:resolved.unit };
    }
  }

  // Slash multi-pack: "4/4 oz"
  const slashMultiMatch = s.match(/^([\d.,]+)\s*\/\s*([\d.,]+)\s+(.+)$/);
  if (slashMultiMatch) {
    const count = parseFloat(slashMultiMatch[1].replace(/,/g, "")), perUnit = parseFloat(slashMultiMatch[2].replace(/,/g, ""));
    if (!isNaN(count) && !isNaN(perUnit) && count !== 1) {
      const resolved = resolveUnit(slashMultiMatch[3].trim().toLowerCase());
      if (resolved) return { amount:count*perUnit, ...resolved, rawSize:perUnit, rawCount:count, rawUnit:resolved.unit };
    }
  }

  // Fraction: "1/2 gallon"
  const fracMatch = s.match(/^(\d+)\s*\/\s*(\d+)\s+(.+)$/);
  if (fracMatch) {
    const num = parseInt(fracMatch[1],10), den = parseInt(fracMatch[2],10);
    if (den !== 0) {
      const resolved = resolveUnit(fracMatch[3].trim().toLowerCase());
      if (resolved) return { amount:num/den, ...resolved, rawSize:num/den, rawCount:1, rawUnit:resolved.unit };
    }
  }

  // Standard: "12oz" or "12 oz"
  const stdMatch = s.match(/^([\d.,]+)\s*(.+)$/);
  if (stdMatch) {
    const amount = parseFloat(stdMatch[1].replace(/,/g, ""));
    if (isNaN(amount) || amount <= 0) return null;
    const resolved = resolveUnit(stdMatch[2].trim().toLowerCase());
    if (resolved) return { amount, ...resolved, rawSize:amount, rawCount:1, rawUnit:resolved.unit };
  }

  return null;
}

const UNIT_PATTERN_PARTS = [
  "fl\\.?\\s*oz\\.?","fluid\\s+ounces?",
  "oz\\.?","ounces?","lbs?\\.?","pounds?","gal\\.?","gallons?","qt\\.?","quarts?",
  "pt\\.?","pints?","liters?","litres?","ml","milliliters?","millilitres?","l",
  "kg","kilograms?","g","grams?","mg","milligrams?","cups?",
  "ct","count","ea","each","pk","packs?","packets?",
];

const EXTRACT_SIZE_RE = new RegExp(
  `(\\d+\\.?\\d*)\\s*(${UNIT_PATTERN_PARTS.join("|")})(?:\\b|$)`, "i"
);

function extractSizeFromName(name: string): string | null {
  const match = name.match(EXTRACT_SIZE_RE);
  if (match) return `${match[1]} ${match[2]}`;
  const lower = name.toLowerCase();
  for (const key of Object.keys(DESCRIPTIVE_SIZES)) {
    if (lower.includes(key)) return key;
  }
  return null;
}

function round(n: number, decimals = 4): number {
  const f = Math.pow(10, decimals);
  return Math.round(n * f) / f;
}

function formatNum(n: number): string {
  return parseFloat(n.toFixed(4)).toString();
}

export function normalizeSize(parsed: ParsedSize, price: number | null, category?: string): NormalizedSize {
  if (parsed.type === "count") {
    const pricePerUnit = price != null ? round(price / parsed.amount, 2) : null;
    return { displaySize:`${formatNum(parsed.amount)} ${parsed.unit}`, baseAmount:parsed.amount, baseUnit:parsed.unit, pricePerUnit };
  }

  const catUnits = CATEGORY_STANDARD_UNITS[category ?? "CATEGORY_UNCERTAIN"] ?? CATEGORY_STANDARD_UNITS["CATEGORY_UNCERTAIN"];
  const targetUnit = parsed.type === "mass" ? catUnits.mass : catUnits.volume;
  const baseAmount = parsed.unit === targetUnit ? parsed.amount : convertUnit(parsed.amount, parsed.unit, targetUnit);
  const roundedBase = round(baseAmount, 2);

  let displaySize: string;
  if (roundedBase < 0.1 || roundedBase > 1000) {
    const best = pickDisplayUnit(roundedBase, targetUnit);
    displaySize = `${formatNum(best.val)} ${best.unit}`;
  } else {
    displaySize = `${formatNum(roundedBase)} ${targetUnit}`;
  }

  const pricePerUnit = price != null ? round(price / roundedBase, 2) : null;
  return { displaySize, baseAmount:roundedBase, baseUnit:targetUnit, pricePerUnit };
}

// ============================================================================
// SIZE FLAGGER (from src/sizeFlagger.ts)
// ============================================================================

const SIZE_BOUNDS: Record<string, { mass:{ min:number; max:number; unit:string }; volume:{ min:number; max:number; unit:string } }> = {
  PRODUCE:       { mass:{min:0.1, max:50,  unit:"lb"},  volume:{min:1, max:128,  unit:"fl-oz"} },
  MEAT:          { mass:{min:0.25,max:25,  unit:"lb"},  volume:{min:1, max:128,  unit:"fl-oz"} },
  SEAFOOD:       { mass:{min:0.25,max:15,  unit:"lb"},  volume:{min:1, max:64,   unit:"fl-oz"} },
  DAIRY:         { mass:{min:1,   max:160, unit:"oz"},  volume:{min:1, max:128,  unit:"fl-oz"} },
  EGGS:          { mass:{min:1,   max:160, unit:"oz"},  volume:{min:1, max:128,  unit:"fl-oz"} },
  ALT_MILK:      { mass:{min:1,   max:64,  unit:"oz"},  volume:{min:4, max:128,  unit:"fl-oz"} },
  DELI_PREPARED: { mass:{min:0.25,max:10,  unit:"lb"},  volume:{min:1, max:128,  unit:"fl-oz"} },
  BAKERY:        { mass:{min:1,   max:80,  unit:"oz"},  volume:{min:1, max:64,   unit:"fl-oz"} },
  SNACKS:        { mass:{min:0.5, max:64,  unit:"oz"},  volume:{min:1, max:64,   unit:"fl-oz"} },
  PANTRY:        { mass:{min:0.5, max:160, unit:"oz"},  volume:{min:1, max:128,  unit:"fl-oz"} },
  DESSERT:       { mass:{min:1,   max:96,  unit:"oz"},  volume:{min:1, max:128,  unit:"fl-oz"} },
  BEVERAGES:     { mass:{min:1,   max:64,  unit:"oz"},  volume:{min:4, max:384,  unit:"fl-oz"} },
  FROZEN:        { mass:{min:1,   max:160, unit:"oz"},  volume:{min:1, max:128,  unit:"fl-oz"} },
  HOUSEHOLD:     { mass:{min:1,   max:320, unit:"oz"},  volume:{min:1, max:256,  unit:"fl-oz"} },
  HEALTH_BEAUTY: { mass:{min:0.5, max:64,  unit:"oz"},  volume:{min:0.5,max:128, unit:"fl-oz"} },
  PET:           { mass:{min:0.5, max:50,  unit:"lb"},  volume:{min:1, max:128,  unit:"fl-oz"} },
  BABY:          { mass:{min:0.5, max:80,  unit:"oz"},  volume:{min:1, max:64,   unit:"fl-oz"} },
  ALCOHOL:       { mass:{min:1,   max:64,  unit:"oz"},  volume:{min:4, max:384,  unit:"fl-oz"} },
  FLORAL:        { mass:{min:0.5, max:20,  unit:"oz"},  volume:{min:1, max:64,   unit:"fl-oz"} },
};

const SUSPICIOUS_UNIT_TYPES: Record<string, string[]> = {
  MEAT:["volume"], SEAFOOD:["volume"], PRODUCE:["volume"], FLORAL:["mass","volume"], BEVERAGES:["mass"],
};

const DESCRIPTIVE_LABELS_FLAG = new Set([
  "family size","family pack","large","lg","medium","med",
  "small","sm","snack size","king size","party size","jumbo","mini",
  "single","individual","each",
]);

const PRICE_AS_SIZE_RE = /^\$[\d.,]+\s*\/\s*.+$/i;
const SOLD_BY_RE = /^sold\s+/i;
const PER_UNIT_RE = /^(?:per|by\s+the)\s+/i;

export interface SizeFlagResult { flagged: boolean; reasons: string[]; }

export interface SizeFlagParams {
  rawSizeString: string | null;
  baseAmount: number | null;
  baseUnit: string | null;
  measureType: "mass" | "volume" | "count" | null;
  sizeSource: string | null;
  sizeConfidence: number | null;
  category: string;
  trailingCleanupFired: boolean;
}

export function shouldFlagSize(params: SizeFlagParams): SizeFlagResult {
  const { rawSizeString, baseAmount, baseUnit, measureType: mType, sizeSource, sizeConfidence, category, trailingCleanupFired } = params;
  const reasons: string[] = [];

  if (baseAmount == null || baseUnit == null || mType == null) {
    reasons.push("null_size");
    return { flagged: true, reasons };
  }

  if (sizeSource === "ai" && sizeConfidence != null && sizeConfidence < 0.7) reasons.push("ai_low_confidence");

  const bounds = SIZE_BOUNDS[category];
  if (bounds && mType !== "count") {
    const typeBounds = mType === "mass" ? bounds.mass : bounds.volume;
    if (baseAmount < typeBounds.min || baseAmount > typeBounds.max) reasons.push("out_of_bounds");
  }
  if (mType === "count" && (baseAmount > 100 || baseAmount < 1)) reasons.push("out_of_bounds");

  const suspicious = SUSPICIOUS_UNIT_TYPES[category];
  if (suspicious && mType !== "count" && suspicious.includes(mType)) reasons.push("suspicious_unit_for_category");

  const raw = rawSizeString?.trim().toLowerCase() ?? "";
  if (raw && DESCRIPTIVE_LABELS_FLAG.has(raw)) reasons.push("vague_descriptor");
  if (raw && PRICE_AS_SIZE_RE.test(raw)) reasons.push("price_as_size");
  if (raw && SOLD_BY_RE.test(raw)) reasons.push("sold_by_pattern");
  if (raw && PER_UNIT_RE.test(raw)) reasons.push("per_unit_pattern");
  if (trailingCleanupFired) reasons.push("trailing_artifact_cleanup");

  return { flagged: reasons.length > 0, reasons };
}

// ============================================================================
// BRAND-CATEGORY VALIDATOR (from src/brandCategoryValidator.ts)
// ============================================================================

export interface BrandCategoryValidation {
  brandCategoryMatch: boolean | null;
  shouldFlag: boolean;
  suggestedCategory: string | null;
}

const CATEGORY_KEYWORDS_VALIDATOR: Record<string, string[]> = {
  PRODUCE:["apple","apples","banana","bananas","lettuce","tomato","tomatoes","potato","potatoes","onion","onions","carrot","carrots","celery","broccoli","cauliflower","spinach","kale","avocado","avocados","pepper","peppers","cucumber","cucumbers","mushroom","mushrooms","zucchini","squash","corn","berries","strawberry","strawberries","blueberry","blueberries","raspberry","raspberries","grape","grapes","orange","oranges","lemon","lemons","lime","limes","mango","mangoes","peach","peaches","pear","pears","watermelon","cantaloupe","melon","pineapple","cherry","cherries","plum","plums","salad","greens","herbs","cilantro","parsley","basil","garlic","ginger","organic","fresh","fruit","vegetable","vegetables","veggies"],
  MEAT:["chicken","beef","pork","steak","steaks","ground","turkey","lamb","sausage","sausages","bacon","ham","brisket","ribs","roast","tenderloin","chop","chops","patty","patties","breast","thigh","thighs","drumstick","wing","wings","loin","sirloin","ribeye","filet","mignon","veal","bison","venison","jerky","hot dog","hot dogs","meatball","meatballs","boneless","skinless"],
  SEAFOOD:["salmon","shrimp","tuna","cod","tilapia","crab","lobster","fish","catfish","halibut","mahi","trout","scallop","scallops","clam","clams","oyster","oysters","mussel","mussels","squid","calamari","anchovy","anchovies","sardine","sardines","seafood","fillet"],
  DAIRY:["milk","cheese","yogurt","butter","cream","sour cream","cottage cheese","cream cheese","whipped cream","half and half","creamer","cheddar","mozzarella","parmesan","swiss","provolone","gouda","brie","feta","ricotta","greek yogurt","whole milk","skim milk","2% milk","heavy cream","whipping cream"],
  EGGS:["eggs","egg","egg whites","dozen eggs","cage free eggs","organic eggs","large eggs","extra large eggs"],
  ALT_MILK:["oat milk","almond milk","soy milk","coconut milk","cashew milk","oat","almond","soy","plant based","plant-based","dairy free","non-dairy","vegan milk"],
  DELI_PREPARED:["deli","rotisserie","hummus","prepared","ready to eat","cold cuts","lunch meat","salad","potato salad","coleslaw","fried chicken","chicken tenders","chicken strips","meatloaf","meal kit","charcuterie","sandwich"],
  BAKERY:["bread","bagel","bagels","bun","buns","roll","rolls","croissant","tortilla","tortillas","pita","naan","baguette","sourdough","flatbread","brioche","ciabatta","biscuit","biscuits","croutons","english muffin","loaf"],
  SNACKS:["chip","chips","crackers","cracker","popcorn","pretzel","pretzels","nuts","trail mix","granola bar","protein bar","snack","snacks","tortilla chips","cheese puffs","pork rinds","beef jerky","dried fruit","rice cakes","fruit snacks"],
  PANTRY:["soup","broth","stock","sauce","pasta","noodle","noodles","rice","beans","canned","cereal","oatmeal","oats","granola","flour","sugar","oil","vinegar","spice","spices","seasoning","condiment","ketchup","mustard","mayo","mayonnaise","dressing","salsa","peanut butter","jelly","jam","honey","syrup","coffee","tea","mix","baking","crust"],
  DESSERT:["cake","cupcake","pie","brownie","brownies","cookie","cookies","donut","donuts","pastry","muffin","muffins","ice cream","cheesecake","gelato","fudge","pudding","tart","eclair"],
  BEVERAGES:["water","juice","soda","pop","cola","lemonade","tea","coffee","energy drink","sports drink","sparkling water","coconut water","kombucha","smoothie","drink","beverage"],
  FROZEN:["frozen","pizza","ice","waffles","waffle","fries","nuggets","frozen dinner","frozen meal","tv dinner","pot pie","burrito","frozen vegetables","frozen fruit"],
  HOUSEHOLD:["detergent","bleach","cleaner","sponge","trash bags","paper towels","toilet paper","napkins","tissues","dish soap","laundry","dryer sheets","fabric softener","aluminum foil","plastic wrap","storage bags","air freshener","disinfectant"],
  HEALTH_BEAUTY:["shampoo","conditioner","deodorant","toothpaste","toothbrush","lotion","sunscreen","soap","body wash","face wash","razor","vitamin","vitamins","supplement","medicine","bandage","floss"],
  PET:["dog food","cat food","dog treat","cat treat","pet food","cat litter","dog","cat","puppy","kitten","pet"],
  BABY:["diaper","diapers","baby food","baby formula","infant formula","baby wipes","baby","pacifier","sippy"],
  ALCOHOL:["beer","wine","vodka","whiskey","bourbon","tequila","rum","gin","champagne","seltzer","hard seltzer","lager","ale","stout","ipa","merlot","chardonnay","cabernet","prosecco"],
  FLORAL:["bouquet","flowers","roses","tulips","lilies","orchid","arrangement","plant","succulent"],
};

function scoreCategory(productNameLower: string, category: string): number {
  const keywords = CATEGORY_KEYWORDS_VALIDATOR[category];
  if (!keywords) return 0;
  let count = 0;
  for (const kw of keywords) { if (productNameLower.includes(kw)) count++; }
  return count;
}

function pickBestCategory(productNameLower: string, candidates: string[]): string | null {
  if (candidates.length === 0) return null;
  if (candidates.length === 1) return candidates[0];
  let best: string | null = null, bestScore = -1;
  for (const cat of candidates) {
    const score = scoreCategory(productNameLower, cat);
    if (score > bestScore) { bestScore = score; best = cat; }
  }
  if (bestScore <= 0) return candidates[0];
  return best;
}

export function validateBrandCategory(
  brand: string | null,
  isBrand: boolean,
  currentCategory: string,
  brandCategoryMap: Map<string, string[] | null>,
  productName?: string,
): BrandCategoryValidation {
  if (!brand || !isBrand) return { brandCategoryMatch: null, shouldFlag: false, suggestedCategory: null };
  const brandLower = brand.toLowerCase();
  if (!brandCategoryMap.has(brandLower)) return { brandCategoryMatch: null, shouldFlag: false, suggestedCategory: null };
  const commonCategories = brandCategoryMap.get(brandLower);
  if (commonCategories == null || commonCategories.length === 0) return { brandCategoryMatch: null, shouldFlag: false, suggestedCategory: null };
  if (commonCategories.includes(currentCategory)) return { brandCategoryMatch: true, shouldFlag: false, suggestedCategory: null };
  const nameLower = (productName ?? "").toLowerCase();
  const suggested = pickBestCategory(nameLower, commonCategories as string[]);
  return { brandCategoryMatch: false, shouldFlag: true, suggestedCategory: suggested };
}

// ============================================================================
// ORGANIC DETECTION (from normalize.ts)
// ============================================================================

const ORGANIC_RE = /\borganic\b/i;

function detectOrganic(...fields: (string | null | undefined)[]): boolean {
  for (const field of fields) {
    if (!field) continue;
    if (ORGANIC_RE.test(field)) return true;
  }
  return false;
}

// ============================================================================
// MAIN EXPORTED FUNCTIONS
// ============================================================================

/**
 * Process a single raw product row through the full deterministic pipeline.
 * Pure function — no DB calls, no API calls.
 * Everything it needs comes from the context parameter.
 *
 * The underscore-prefixed fields (_needsAIBrand etc.) are signals for the
 * caller. Strip them before upserting to the database.
 */
export function enrichRow(row: RawRow, context: EnrichmentContext): EnrichedRow {
  const productName: string | null = (row.product_name ?? null) as string | null;
  const price: number | null = (() => {
    if (row.product_price == null || String(row.product_price).trim() === "") return null;
    const p = parseFloat(String(row.product_price));
    return isNaN(p) ? null : p;
  })();

  // ---- Determine raw size string ----
  let rawSize: string | null = (row.product_size ?? null) as string | null;
  if (!rawSize || !rawSize.trim()) {
    rawSize = productName ? extractSizeFromName(productName) : null;
  }

  // ---- 1. Categorize ----
  let category: string;
  let categoryFlag = false;
  if (productName) {
    const catResult = categorizeWithConflicts(productName);
    category = catResult.category;
    categoryFlag = catResult.flagged;
  } else {
    category = ProductCategory.CATEGORY_UNCERTAIN;
  }

  // ---- 2. Brand matching (three-pass) ----
  let brand: string | null = null;
  let is_brand: boolean | null = null;
  let brandSrc: "lookup" | "none" = "none";
  let _needsAIBrand = false;
  let _tentativeGenericMatch: { brand: string } | null = null;

  if (productName) {
    const tableMatch = extractBrand(productName, context.brandLookup);
    if (tableMatch) {
      if (!tableMatch.needsAI) {
        brand = tableMatch.brand;
        is_brand = tableMatch.isBrand;
        brandSrc = "lookup";
      } else {
        _needsAIBrand = true;
        _tentativeGenericMatch = tableMatch.tentativeGenericMatch;
      }
    } else {
      _needsAIBrand = true;
    }
  }

  // ---- 3. Store brand ----
  const is_store_brand: boolean = productName ? isStoreBrand(productName) : false;

  // ---- 4. Organic ----
  const is_organic: boolean = detectOrganic(productName, brand, row.product_description);

  // ---- 5. Brand-category cross-validation ----
  const validation = validateBrandCategory(brand, is_brand === true, category, context.brandCategoryMap, productName ?? undefined);
  const brand_category_match = validation.brandCategoryMatch;
  let brandCategoryFlag = false;
  let categorySource: string = category !== ProductCategory.CATEGORY_UNCERTAIN ? "deterministic" : "none";
  let categoryConfidence: number | null = category !== ProductCategory.CATEGORY_UNCERTAIN ? 1.0 : null;

  if (validation.shouldFlag) {
    brandCategoryFlag = true;
    if (validation.suggestedCategory) {
      category = validation.suggestedCategory;
      categorySource = "brand";
      categoryConfidence = 1.0;
    }
  }

  const finalCategoryFlag = categoryFlag || brandCategoryFlag;

  // ---- 6. Parse and normalize size ----
  let display_size: string | null = null;
  let base_amount: number | null = null;
  let base_unit: string | null = null;
  let price_per_unit: number | null = null;
  let size: number | null = null;
  let count: number | null = null;
  let unit: string | null = null;
  let size_source = "deterministic";
  let size_confidence: number | null = null;
  let size_flag = true; // default: flagged (will be overwritten below)

  // Detect trailing cleanup for size flagging
  const trimmedRaw = rawSize?.trim() ?? "";
  const trailingCleanupFired = !!rawSize &&
    !/^\d[\d.,]*\s*[xX×*]\s*\d/.test(trimmedRaw) &&
    /\s*[x.,/|\-]+\s*$/.test(trimmedRaw);

  if (rawSize) {
    const parsed = parseSize(rawSize);
    if (parsed) {
      // Beverage oz → fl-oz assumption
      if ((category === ProductCategory.BEVERAGES || category === ProductCategory.ALT_MILK) &&
          parsed.unit === "oz" && parsed.type === "mass") {
        const rawLower = rawSize.toLowerCase();
        const hasWeightIndicator = /\b(wt\s*oz|net\s*wt|weight)\b/.test(rawLower);
        if (!hasWeightIndicator) {
          parsed.unit = "fl-oz";
          parsed.type = "volume";
          parsed.rawUnit = "fl-oz";
        }
      }

      try {
        const norm = normalizeSize(parsed, price, category);
        display_size = norm.displaySize;
        base_amount = norm.baseAmount;
        base_unit = norm.baseUnit;
        price_per_unit = norm.pricePerUnit;
        size = parsed.rawSize;
        count = parsed.rawCount;
        unit = parsed.rawUnit;
        size_source = "deterministic";
        size_confidence = 1.0;

        const flagResult = shouldFlagSize({
          rawSizeString: rawSize,
          baseAmount: norm.baseAmount,
          baseUnit: norm.baseUnit,
          measureType: parsed.type,
          sizeSource: "deterministic",
          sizeConfidence: 1.0,
          category,
          trailingCleanupFired,
        });
        size_flag = flagResult.flagged;
      } catch {
        // parse succeeded but normalization failed — fall through to null size flag
        const flagResult = shouldFlagSize({ rawSizeString: rawSize, baseAmount: null, baseUnit: null, measureType: null, sizeSource: "deterministic", sizeConfidence: null, category, trailingCleanupFired });
        size_flag = flagResult.flagged;
      }
    } else if (isDescriptiveSize(rawSize)) {
      display_size = rawSize;
      size_source = "deterministic";
      size_confidence = 0.5;
      // size_flag determined below (null size path)
      const flagResult = shouldFlagSize({ rawSizeString: rawSize, baseAmount: null, baseUnit: null, measureType: null, sizeSource: "deterministic", sizeConfidence: null, category, trailingCleanupFired });
      size_flag = flagResult.flagged;
    } else {
      const flagResult = shouldFlagSize({ rawSizeString: rawSize, baseAmount: null, baseUnit: null, measureType: null, sizeSource: "deterministic", sizeConfidence: null, category, trailingCleanupFired });
      size_flag = flagResult.flagged;
    }
  } else {
    const flagResult = shouldFlagSize({ rawSizeString: null, baseAmount: null, baseUnit: null, measureType: null, sizeSource: "deterministic", sizeConfidence: null, category, trailingCleanupFired });
    size_flag = flagResult.flagged;
  }

  // ---- 7. AI signals ----
  const _needsAICategory = category === ProductCategory.CATEGORY_UNCERTAIN;
  const _needsAISize = size_confidence == null || display_size == null;

  return {
    id: row.id,
    product_name: row.product_name ?? null,
    product_price: price,
    product_size: (row.product_size ?? null) as string | null,
    retailer: (row.retailer ?? null) as string | null,

    category,
    brand,
    is_store_brand,
    is_organic,

    display_size,
    base_amount,
    base_unit,
    price_per_unit,
    size,
    count,
    unit,

    brand_confidence: brandSrc === "lookup" ? 1.0 : null,
    category_confidence: categoryConfidence,
    brand_source: brandSrc === "lookup" ? "lookup" : "none",
    category_source: categorySource,
    size_source,
    size_confidence,

    is_brand,
    canonical_product_name: null,
    brand_category_match,

    date_added: new Date().toISOString(),
    size_flag,
    category_flag: finalCategoryFlag,
    category_conflicts: finalCategoryFlag,

    _needsAIBrand,
    _needsAICategory,
    _needsAISize,
    _tentativeGenericMatch,
  };
}

/**
 * Strip internal signal fields before writing to the database.
 * The _needsAI* and _tentativeGenericMatch fields are pipeline-only;
 * they don't exist as columns in the DB schema.
 */
export function toDbRow(enriched: EnrichedRow): Record<string, unknown> {
  const {
    _needsAIBrand: _a,
    _needsAICategory: _b,
    _needsAISize: _c,
    _tentativeGenericMatch: _d,
    ...rest
  } = enriched;
  return rest as Record<string, unknown>;
}

/**
 * Fetch all brand data from Supabase and build the in-memory EnrichmentContext.
 * Call once at startup; pass the returned context to every enrichRow() call.
 */
export async function loadEnrichmentContext(supabase: SupabaseClient): Promise<EnrichmentContext> {
  const entries: BrandEntry[] = [];
  const brandCategoryMap = new Map<string, string[] | null>();
  let offset = 0;
  const PAGE = 1000;

  while (true) {
    const { data, error } = await supabase
      .from("brands_test")
      .select("brand_name, is_brand, common_categories")
      .range(offset, offset + PAGE - 1);

    if (error) { console.warn("Could not load brands:", error.message); break; }
    if (!data || data.length === 0) break;

    for (const row of data) {
      if (!row.brand_name) continue;
      const brandName = row.brand_name as string;
      const isBrand = row.is_brand === true;
      entries.push({ brandName, isBrand });

      const catsStr = row.common_categories as string | null;
      if (catsStr === null || catsStr === "") {
        brandCategoryMap.set(brandName.toLowerCase(), null);
      } else {
        const categories = catsStr.split(",").map((c: string) => c.trim()).filter(Boolean);
        brandCategoryMap.set(brandName.toLowerCase(), categories);
      }
    }

    if (data.length < PAGE) break;
    offset += PAGE;
  }

  return {
    brandLookup: buildBrandLookup(entries),
    brandCategoryMap,
  };
}
