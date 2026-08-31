# Data integrity baseline

This directory contains a read-only production baseline pack. It is designed
to establish a trusted baseline across source rows, the canonical v2.7
projection, and Search/Deals/Cart visibility without modifying production.

## Check-in

### Meaningful deliverables completed

1. **A reproducible, read-only audit pack is complete.** The SQL measures
   canonical/brand/category/size/price quality, retailer freshness, store
   health, and search fallbacks.
2. **The canonical projection boundary is covered.** The audit compares
   `flyer_deals` with `best_deals_comprehensive` and distinguishes source defects
   from projection mismatches and downstream visibility failures.
3. **The QA workflow is ready to produce evidence.** The exporter creates a
   deterministic 250-row sample across Kroger, Whole Foods, Harris Teeter, and
   Aldi v2, plus retailer scorecard and ranked issue-register outputs.

### Working result

The local read-only smoke test successfully authenticated to Supabase and read
`flyer_deals`. The complete REST export is currently blocked by Supabase
statement timeouts on broad production scans, so no production scorecard,
labeled sample, or top-10 findings are being presented as completed. The SQL
pack is ready to run server-side in the Supabase SQL Editor, which avoids that
transfer/scan limitation.

### Blockers

- **Supabase REST statement timeout:** broad reads time out before the exporter
  can finish.
- **Required input:** approval to run the SQL pack in the Supabase SQL Editor,
  or a read-only database connection with a higher statement timeout.
- **Manual review dependency:** a reviewer must label the generated 250 rows as
  `correct`, `incorrect`, or `uncertain`.
- **Projection schema dependency:** confirm that
  `best_deals_comprehensive` is the production v2.7 projection view.

No production data has been modified.

## Run

1. Provide a read-only Supabase key in the environment:
   `SUPABASE_URL=... SUPABASE_KEY=...`
2. Run `python data_quality/run_baseline.py`.
3. Review and label `data_quality/qa_sample_250.csv`.
4. Re-run with the labeled sample to produce the issue register:
   `python data_quality/run_baseline.py --qa-csv data_quality/qa_sample_250.csv`.

The script writes `scorecard_by_retailer.csv`, `issue_register.csv`, and
`qa_sample_250.csv`. It never calls an insert, update, delete, upsert, RPC, or
SQL write endpoint.

The SQL pack additionally checks v2.7 projection parity against
`best_deals_comprehensive`, and records Search, Deals, and Cart eligibility in
the QA sample. Projection/view availability is confirmed by the schema
introspection query before running those checks.

Labels must be `correct`, `incorrect`, or `uncertain`. Issue types should use
the controlled vocabulary in the sample header. Rank issue classes by
`user_impact * affected_rows * recurrence`, not by implementation effort.
