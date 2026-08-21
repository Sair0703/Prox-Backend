# Store Location Acquisition

The Store Location Acquisition capability provides a common framework for collecting store-location data from official retailer sources. Each retailer keeps its acquisition logic in an independent strategy, while runners provide executable entry points and the shared service provides the staged acquisition interface and common output handling.

## Crawling Strategies

| Retailer | Strategy | Acquisition approach |
| --- | --- | --- |
| Albertsons | `albertsons_acquisition_strategy.py` | Official local directory hierarchy; state/city/store pages with detail-page enrichment |
| ALDI | `aldi_acquisition_strategy.py` | Official Uberall store-finder JSON dataset |
| Best Buy | `bestbuy_acquisition_strategy.py` | Official HTML store directory; state/city/store traversal |
| BJ's Wholesale Club | `bjs_acquisition_strategy.py` | Official locator source with retailer-specific store extraction |
| Costco | `costco_acquisition_strategy.py` | Official warehouse sitemap; state pages and warehouse detail pages |
| CVS Pharmacy | `cvs_pharmacy_acquisition_strategy.py` | Official CVS location source with retailer-specific parsing |
| Dollar Tree | `dollar_tree_acquisition_strategy.py` | Official locator/API source with geographic store discovery |
| Family Dollar | `family_dollar_acquisition_strategy.py` | Official store directory and store-page extraction |
| Fareway | `fareway_acquisition_strategy.py` | Official retailer location pages with store extraction |
| Food Lion | `food_lion_acquisition_strategy.py` | Official store directory hierarchy and detail-page acquisition |
| Gelson's | `gelsons_acquisition_strategy_commented.py` | Official Gelson's store source with retailer-specific parsing |
| The GIANT Company | `giant_company_acquisition_strategy.py` | Official GIANT/MARTIN'S location source |
| Giant Eagle | `giant_eagle_acquisition_strategy.py` | Official Giant Eagle store directory and detail pages |
| Giant Food | `giant_food_acquisition_strategy.py` | Official Giant Food store directory and detail pages |
| Hannaford | `hannaford_acquisition_strategy.py` | Official Hannaford store source with retailer-specific parsing |
| H-E-B | `heb_acquisition_strategy.py` | Next.js locator JSON; Texas ZIP enumeration, pagination, and global store-ID merge |
| Hy-Vee | `hyvee_acquisition_strategy.py` | ASP.NET store finder; sequential browser pagination and store-card parsing |
| Kroger | `kroger_acquisition_strategy.py` | Official directory JSON → city JSON → location IDs → Atlas locator API |
| Meijer | `meijer_acquisition_strategy.py` | Official geographic store-search endpoint with overlapping location seeds |
| Petco | `petco_acquisition_strategy.py` | Official state/city HTML directory and store detail/card parsing |
| PetSmart | `petsmart_acquisition_strategy.py` | Official state/city HTML directory and store-card parsing |
| Piggly Wiggly | `piggly_wiggly_acquisition_strategy.py` | Official state directory pages; store cards provide authoritative store IDs |
| Safeway | `safeway_acquisition_strategy.py` | Official local directory; state/city/detail traversal with rendered pages |
| Sam's Club | `samsclub_acquisition_strategy.py` | Official club directory; state pages → city/detail pages |
| ShopRite | `shoprite_acquisition_strategy.py` | Official geographic stores API with overlapping probes and store-ID deduplication |
| Smart & Final | `smart_final_acquisition_strategy.py` | Official nearby-store REST API with large-radius geographic probes |
| Sprouts Farmers Market | `sprouts_acquisition_strategy.py` | Official store index → state pages → store cards |
| Target | `target_acquisition_strategy.py` | Official directory; requests for static pages and Playwright for multi-store city expansion |
| Trader Joe's | `trader_joes_acquisition_strategy.py` | Official root → state → city directory; city pages expose store records directly |
| Ulta Beauty | `ulta_acquisition_strategy.py` | Rendered store directory → state sections → store detail pages |
| Walgreens | `walgreens_acquisitoin_strategy.py` | State/city directory with Playwright used to expand dynamic city store lists |
| Wegmans | `wegmans_acquisition_strategy.py` | Official store directory → direct store detail pages |
| Whole Foods Market | `whole_foods_acquisition_strategy.py` | Official locator HTML queried with geographic ZIP seeds and merged globally |

## Service Interface

`StoreLocationAcquisitionService` provides the shared staged workflow for strategies implementing `StoreLocationAcquisitionStrategy`:

| Interface | Purpose |
| --- | --- |
| `discover_source()` | Describe the official retailer source and acquisition mechanism |
| `fetch_raw_artifacts()` | Acquire raw HTML, JSON, API responses, or other source artifacts |
| `extract_store_payloads()` | Convert retailer-specific source data into normalized store payloads |
| `validate_store_payloads()` | Validate identifiers, duplicates, completeness, and acquisition quality |
| `build_run_notes()` | Provide source and execution notes for the run summary |
| `StoreLocationAcquisitionService.acquire()` | Execute the shared workflow and write CSV/summary output |

Retailer runners under `runners/` are the executable entry points and contain retailer-specific runtime configuration such as worker counts, retry settings, browser behavior, and output handling.

## Output

Acquisition runs write retailer-specific results under `_output/`, normally using a retailer and timestamp hierarchy:

```text
_output/
└── <retailer>/
    └── <timestamp>/
        ├── <timestamp>_<retailer>_us_locations.csv
        └── <timestamp>_<retailer>_summary.json
```

## Notes

Retailer acquisition depends on external websites and APIs, so strategies may require maintenance when the upstream source changes.

* HTML-based strategies can fail when page structure, selectors, or directory routes change.
* API-based strategies can fail when endpoints, response schemas, query parameters, or access behavior change.
* Playwright-based strategies require a working Playwright/browser installation and may need adjustment when client-side behavior changes.
* Geographic enumeration strategies depend on sufficient seed/probe coverage and should retain deduplication and coverage validation.
* H-E-B uses a Next.js data route whose build ID changes across deployments. If the fallback build ID becomes stale, obtain the current build ID from a successful `/_next/data/<BUILD_ID>/en/store-locations.json?...` request in the Network panel of `https://www.heb.com/store-locations`, then update `KNOWN_BUILD_ID` or set `HEB_BUILD_ID`.
* Store identifiers should come from authoritative retailer fields whenever available rather than being inferred from names or addresses.
* Validation and failure diagnostics should not be removed simply to make an incomplete acquisition appear successful.
